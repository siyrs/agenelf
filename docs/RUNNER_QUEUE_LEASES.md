# Agenelf Runner 队列租约与崩溃恢复

Agenelf 的运维、审批、自我升级、软件验证和代码修复都通过独立 Runner 消费文件队列。每个请求在执行期间会创建一个 `.lock` 文件，防止同一请求被重复执行。

过去，如果 Runner 在写入结果前异常退出，`.lock` 可能永久残留。容器重新启动后，新 Runner 会持续返回 `locked`，请求既没有结果，也不会再次执行。`/doctor` 只能看到队列积压，却无法判断是业务失败还是崩溃遗留锁。

## 新的恢复模型

所有长期 Runner 仍由：

```text
scripts/runner_supervisor.py
```

启动。Supervisor 现在负责两层互斥：

1. **Supervisor 租约**：同一 Runner 名称在共享数据目录中只能有一个活动 Supervisor；
2. **请求锁恢复**：只有取得 Supervisor 独占租约后，才允许回收该 Runner 固定锁目录中的旧 `.lock` 文件。

受管 Runner 与锁目录是固定映射：

| Runner | 锁目录 |
|---|---|
| `ops-runner` | `data/ops-locks` |
| `approval-runner` | `data/approval-locks` |
| `self-upgrade-runner` | `data/self-upgrade-locks` |
| `validation-runner` | `data/validation-locks` |
| `repair-runner` | `data/repair-locks` |

模型不能提供锁目录，也不能要求 Supervisor 清理其他路径。

## Supervisor 租约

租约保存在：

```text
data/runner-health/<runner>.supervisor/owner.json
```

租约包含：

- Runner 名称；
- 随机实例 ID；
- Supervisor PID；
- 子进程 PID；
- PID 命名空间标识；
- 启动与最近心跳时间；
- 租约陈旧阈值；
- 当前状态。

租约不包含：

- 子进程 argv；
- 环境变量；
- 请求参数；
- SSH、Token 或审批密钥；
- 模型提示词或 reasoning；
- stdout/stderr。

目录创建使用原子 `mkdir`。如果已存在租约：

- PID 命名空间相同且 Supervisor PID 仍存活：拒绝启动第二个 Supervisor，即使心跳暂时延迟；
- PID 命名空间不同但共享卷心跳仍新鲜：视为另一个容器可能仍在运行，拒绝接管；
- 不同命名空间的心跳已经超时：允许原子隔离旧租约后接管；
- 同命名空间 PID 已退出，或租约内容损坏：允许接管。

因此，意外把同一 Compose 服务扩成两份时，第二个容器不会因 PID 命名空间不同而抢走活动 Runner 的租约。容器异常退出后，新容器会等旧共享心跳过期，再安全接管。

## 崩溃遗留锁回收

取得独占租约后，Supervisor 会在启动子进程之前扫描该 Runner 的固定锁目录。

只会删除：

```text
*.lock
```

且必须是普通文件。以下内容不会自动删除：

- 目录；
- 符号链接；
- 设备文件；
- Socket；
- 不在固定锁目录中的任何文件。

非普通条目会计入 `skipped`，`/doctor` 会把运行时标记为 `degraded`，要求人工检查。

为什么可以回收普通 `.lock`：

- Supervisor 已证明同名 Runner 没有其他活动实例；
- Runner 子进程尚未启动；
- 旧锁只能来自已经退出的上一 Runner 实例；
- 请求和审批文件没有被删除；
- 新 Runner 会重新校验请求指纹、授权状态、目标策略和幂等结果文件。

回收锁不会等同于批准请求，也不会绕过任何审批。

## 心跳与 `/doctor`

Runner 心跳增加：

```json
{
  "lock_recovery": {
    "lock_dir": "data/ops-locks",
    "reclaimed": 1,
    "skipped": 0
  },
  "reclaimed_previous_lease": true
}
```

`/doctor` 汇总：

- 自动回收锁总数；
- 未自动处理的异常锁条目；
- 每个 Runner 的具体恢复结果；
- 需要核对的请求结果。

示例：

```text
Runner 5/5 健康；路径异常 0；技能错误 0；自动回收锁 1；锁异常 0
```

当发生自动回收时，Doctor 会建议检查对应的请求结果，避免把“重新进入执行”误认为“任务已经完成”。

## 部署

本轮只修改了 Supervisor 和 Doctor 代码，没有改变 Compose 服务名或挂载拓扑。更新后重新创建长期 Runner 即可：

```powershell
git switch main
git pull --ff-only origin main

docker compose up -d --build --force-recreate `
  ops-runner `
  approval-runner `
  self-upgrade-runner `
  validation-runner `
  repair-runner
```

然后进入 CLI：

```powershell
docker compose exec agenelf python /agenelf/app-fork/cli.py
```

执行：

```text
/doctor
```

## 可调参数

默认跨容器租约陈旧阈值为 15 秒。需要调整时，可在对应 Runner 服务中设置：

```text
AGENELF_SUPERVISOR_LEASE_STALE_SECONDS=20
```

允许范围为 2–300 秒。阈值越短，异常容器后的接管越快；阈值越长，对存储抖动和调度延迟越保守。默认值优先保证不会出现两个 Runner 并发消费同一队列。

## 安全与幂等边界

- Supervisor 始终使用固定 argv 和 `shell=False`；
- 不开放 Docker Socket 或任意宿主机 Shell；
- 不删除请求、审批、结果、备份或审计文件；
- 不清理固定目录之外的路径；
- 结果文件仍是幂等终态：存在结果时 Runner 不会重复执行；
- change/privileged 操作仍需原有精确指纹审批；
- 自我升级候选仍需两阶段主人授权；
- 普通自我迭代不能修改 Supervisor、Runtime Doctor 或租约解释逻辑；
- 修改这些受保护代码仍需主人两阶段授权升级。
