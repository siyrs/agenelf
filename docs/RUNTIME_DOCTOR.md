# Agenelf Runtime Doctor 与 Runner 心跳

`/doctor` 用于在执行真实任务前快速判断 Agenelf 的控制面是否可用。它不连接服务器、不读取 SSH 私钥，也不执行 Docker 命令；它只读取本机的确定性心跳、队列元数据、挂载状态和技能注册错误。

## 解决的问题

过去常见的故障只有在调用具体能力后才暴露，例如：

- Agent 已加载新服务器配置，但 `ops-runner` 没有运行或仍使用旧代码；
- `/approve` 已提交，但 `approval-runner` 没有消费审批命令；
- 自我升级候选已批准，但 `self-upgrade-runner` 未启动；
- `app-tmp`、`data` 或本地配置挂载缺失；
- 技能导入失败，但启动页只显示技能数量；
- 任务停在队列中，Agent 反复重试却没有真实进展。

现在每个长期运行的确定性 Runner 都由 `scripts/runner_supervisor.py` 启动。Supervisor 使用固定 argv、`shell=False`，并周期性写入：

```text
data/runner-health/<runner-name>.json
```

## 当前受监控 Runner

默认监控：

```text
ops-runner
approval-runner
self-upgrade-runner
validation-runner
repair-runner
```

Supervisor 心跳只包含：

- Runner 名称；
- `starting`、`running`、`stopping`、`stopped` 或 `failed` 状态；
- UTC 启动时间和最近心跳时间；
- 有界失效窗口；
- Supervisor/子进程 PID；
- 序列号；
- 退出码或有界启动错误；
- `AGENELF_RUNTIME_SOURCE` 的非秘密标记。

心跳不会记录：

- 子进程完整 argv；
- 环境变量内容；
- 请求参数；
- SSH、Token 或审批密钥；
- 模型提示词、思考内容；
- Runner stdout/stderr。

## CLI 使用

进入交互终端：

```powershell
docker compose exec agenelf python /agenelf/app-fork/cli.py
```

输入：

```text
/doctor
```

也可以直接用自然语言要求 Agenelf 调用只读工具：

```text
先检查 Agenelf 自身运行时和所有 Runner 是否健康，再继续处理服务器任务。
```

返回内容包括：

```text
status
summary
runtime_source
runners
paths
registry_errors
queues
recommendations
```

### Runner 状态含义

| 状态 | 含义 |
|---|---|
| `healthy` | 最近心跳在有效窗口内，子进程处于 starting/running |
| `missing` | 没有找到该 Runner 的心跳文件，通常是容器未创建或 Supervisor 未启动 |
| `invalid` | 心跳不是有效 JSON 或缺少合法时间 |
| `stale` | 曾经有心跳，但已经超过有效窗口，通常是进程卡死或容器停止 |
| `failed` | 子进程已非零退出 |
| `stopped` | 子进程正常退出；对长期 Runner 仍属于不健康 |
| `stopping` | 容器正在停止 |

## 部署与升级

本功能修改了所有长期 Runner 的 Compose `command`，首次更新必须重新创建容器：

```powershell
git switch main
git pull --ff-only origin main

python .\scripts\init_local.py --no-migrate
docker compose up -d --build --force-recreate --remove-orphans
```

检查容器：

```powershell
docker compose ps -a
```

检查具体 Runner：

```powershell
docker compose logs --tail=100 ops-runner
docker compose logs --tail=100 approval-runner
docker compose logs --tail=100 self-upgrade-runner
docker compose logs --tail=100 validation-runner
docker compose logs --tail=100 repair-runner
```

随后进入 CLI 执行：

```text
/doctor
```

正常摘要应接近：

```text
Runner 5/5 健康；路径异常 0；技能错误 0
```

## 配置

`app/config.yaml`：

```yaml
runtime_health:
  stale_after_seconds: 15
  expected_runners:
    - ops-runner
    - approval-runner
    - self-upgrade-runner
    - validation-runner
    - repair-runner
```

`stale_after_seconds` 是 Doctor 的最低陈旧阈值；实际阈值还会尊重每个 Supervisor 在心跳中声明的 `expires_after_seconds`。

## 安全边界

- Agent 容器对 `data/runner-health` 使用只读挂载；
- 只有各 Runner 的 Supervisor 拥有该目录的写权限；
- 心跳不是授权，也不能触发外部副作用；
- `/doctor` 是 `read + pure` 工具，无需审批；
- 心跳正常不代表某次业务操作成功，业务结果仍以对应 `ops-results`、`validation-results`、`repair-results` 或 `self-upgrade-results` 为准；
- `runtime_health.py`、`runtime_doctor.py` 和 Supervisor 属于普通自进化的保护边界，修改它们需要主人两阶段授权升级。

## 典型排障

### `ops-runner` missing 或 stale

```powershell
docker compose up -d --force-recreate ops-runner
docker compose logs --tail=100 ops-runner
```

然后重新执行 `/doctor`。如果心跳恢复但服务器操作仍失败，再查看 `/ops <op-id>`，区分 Runner 生命周期问题与 SSH/服务器配置问题。

### `approval-runner` missing 或 stale

```powershell
docker compose up -d --force-recreate approval-runner
docker compose logs --tail=100 approval-runner
```

审批请求不会因 Runner 暂停而自动变成批准；恢复后仍需使用 `/approvals` 和 `/approve <id>`。

### `self-upgrade-runner` missing 或 stale

```powershell
docker compose up -d --force-recreate self-upgrade-runner
docker compose logs --tail=100 self-upgrade-runner
```

再执行：

```text
/upgrade status
```

确认候选是否仍在 `apply_queued`，并继续相同升级会话。

### 路径异常

先运行：

```powershell
python .\scripts\init_local.py --no-migrate
```

再检查宿主机目录权限和 Docker Desktop 文件共享。`app-tmp` 必须可写；`auth-decisions` 与 `runner-health` 在 Agent 内必须保持只读。
