# Docker 运维、自我升级与任务续办

本文说明 Agenelf 如何通过 SSH 管理受管服务器上的 Docker 容器，以及当当前技能不足时，如何升级能力、重新加载并继续最初任务。

## 1. 本轮修复的问题

实际故障由四条链路共同造成，而不是单纯缺少一条 `docker logs` 命令：

1. Agent 每次读取 `local/servers.yaml`，但旧 `ops-runner` 仅在进程启动时加载一次，导致新服务器别名在 Agent 侧可见、Runner 侧却报“未知服务器别名”。
2. 旧 Docker 能力只有容器列表，没有容器日志、结构化诊断和容器级重启。
3. Docker 默认运行的是只读 `app-fork`；仓库 `app` 已修复而 `app-fork` 未同步时，单纯重启容器仍会执行旧代码。
4. 对话达到单段工具调用上限后直接返回固定失败文本，没有在技能升级或热加载后恢复原始目标。

修复后，这四条链路分别由 `ops_runner_v2.py`、`docker_ops.py`、`docker-compose.override.yml` 和 `task_continuation.py` 负责。

## 2. 新增的 Docker 能力

### `docker_logs`

读取精确指定容器最近 1～2000 行日志。它是只读操作，可由 Runner 自动执行。

### `docker_diagnose`

一次返回以下只读证据：

- 容器名称、镜像、状态、退出码、错误和重启次数；
- 启停时间、入口程序、参数与挂载信息；
- 环境变量数量，但**不读取环境变量值**；
- 有界的最近日志；
- 一次性的 CPU、内存和进程数快照。

### `docker_restart`

重启精确指定容器，并在重启后读取状态、退出码和重启次数。它会改变外部服务器状态，因此只创建绑定服务器、容器、操作和参数指纹的请求；主人在宿主机批准后，Runner 才执行。

以下能力仍然没有开放：

- 任意远程 Shell；
- `docker exec`；
- 将 Docker Socket 挂入 Agent；
- 由模型提供命令片段、管道或重定向；
- 读取容器环境变量值或 SSH 私钥。

## 3. 服务器授权配置

推荐在 `local/servers.yaml` 的目标服务器中显式声明：

```yaml
allowed_operations:
  - inspect
  - docker_ps
  - docker_logs
  - docker_diagnose
  - docker_restart
  - service_status
  - service_restart
```

为了兼容已经部署的配置：

- 只要旧配置允许 `docker_ps`，`docker_logs` 和 `docker_diagnose` 即可执行；
- 只有旧配置同时允许 `docker_ps` 与 `service_restart`，`docker_restart` 才会被接受；
- 无论使用显式授权还是兼容授权，`docker_restart` 都必须经过本次请求的精确宿主机审批。

Runner 会在处理每一个请求前重新读取 `local/servers.yaml`。新增别名或调整允许操作后，不再需要为了刷新服务器清单而重启 Runner。若 YAML 正在写入时暂时无效，Runner 会保留最后一次有效配置并记录审计事件，避免瞬时坏文件导致全部服务器离线。

## 4. 部署最新版运行时

默认的 `docker-compose.override.yml` 会把四个 Python 运行容器的 `/agenelf/app-fork` 挂载目标统一替换为仓库当前的 `./app`，并让 `ops-runner` 使用 `ops_runner_v2.py`。这样仓库更新后不会继续执行历史副本。

在仓库根目录执行：

```bash
git pull origin main

docker compose config > /tmp/agenelf-compose.resolved.yaml
docker compose up -d --build --force-recreate \
  agenelf ops-runner validation-runner repair-runner

docker compose ps
docker compose logs --tail 100 ops-runner
```

不要只运行 `docker compose restart`：restart 不会重新创建容器，也不会应用新增或变化的 Compose 挂载与 command。

## 5. 使用方式

进入 CLI 后可直接给出最终目标，而不是先手动要求某个底层命令：

```text
检查 pve-ubuntu 上的 sing-box。先执行 Docker 诊断，根据日志定位订阅或配置问题；
能只读修复规划就继续，需要重启时创建精确审批请求。技能升级或重新加载后继续这个原始任务，
不要停在能力缺口说明上。
```

正常流程为：

1. Agent 使用 `docker_diagnose` 获取可信证据；
2. 根据日志区分订阅失效、JSON/配置版本不兼容、文件缺失、权限或网络错误；
3. 若现有结构化能力足够，则继续操作；
4. 若需要新增技能，升级只是中间步骤，运行时会在下一有界工具段继续原目标；
5. 若需要 `docker_restart`，Agent 返回请求 ID 和精确批准命令；
6. 主人批准后，Agent 查询结果并继续验证容器状态，而不是把“已提交重启”当成“修复完成”。

## 6. 任务连续性

`task_continuation` 会在系统提示中加入以下运行约束：

- 新增、升级或重载技能不是任务终点；
- 单段工具轮次耗尽后，自动使用最新工具清单、对话历史和记忆继续；
- 默认最多连续执行 3 个有界工具段，可通过 `AGENELF_CONTINUATION_SEGMENTS` 或 `agent.continuation_segments` 调整，范围为 2～6；
- 总预算仍耗尽时，保存并返回可恢复检查点，下一轮可直接续办，不再只输出“达到最大工具调用轮数”。

任务只有在以下情况结束：

- 已完成并有 Runner 或测试证据；
- 正在等待绑定具体载荷的外部审批；
- 存在无法自动消除的真实外部阻塞。

## 7. 验收

本能力的自动化验收覆盖：

- 新服务器别名无需重启 Runner 即可处理；
- Docker 日志与诊断为只读并严格限制容器名和日志行数；
- 诊断命令不读取环境变量值，也不使用 `docker exec`；
- 日志证据经过常见密钥模式脱敏；
- 容器重启必须等待指纹绑定审批，并在执行后验证状态；
- 无效服务器 YAML 保留最后一次有效配置；
- Compose 默认运行当前 `app` 且保持只读、去能力和 `no-new-privileges`；
- 技能升级后自动续办原目标，总预算耗尽时生成可恢复检查点。
