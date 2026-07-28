# Node Read-only Ops Catalog

> 执行域：独立 Node.js/TypeScript SSH Runner  
> 事实源：`data/ops-results/op-*.json`  
> 回放索引：`data/ops-events/op-*.jsonl`

## 1. 信任模型

Agent 只提交结构化 `op-*` 请求，不读取 SSH 密钥，不生成远程命令。Node read runner
读取主人维护的 `local/servers.yaml` 与 `local/secrets/`，重新验证 schema、语义风险、
fingerprint、TTL、服务器策略和参数允许清单，然后使用 OpenSSH 的精确 argv 执行固定
远程命令模板。

`ops-events` 用于 Web、CLI、审计和重放，不能代替 `ops-results` 作为可信完成证明。

## 2. Node 只读操作

| capability | operation | 参数 | 约束 |
|---|---|---|---|
| `server.operations` | `inspect` | 无 | 固定主机/磁盘/内存/Docker 摘要 |
| `server.operations` | `docker_ps` | 无 | Docker 命令只能是主人配置的 `docker` 或 `sudo -n docker` |
| `server.operations` | `service_status` | `service` | 必须在 `allowed_services` |
| `docker.operations` | `get_docker_logs` | `container`, `tail` | container 允许清单，tail 1-1000 |
| `docker.operations` | `inspect_docker_container` | `container` | 固定 inspect format，不读取 `Config.Env` |
| `docker.operations` | `run_docker_check` | `check` | 只选择主人配置的 `docker_checks` alias |

以下操作仍由 Python change/privileged runner 执行并要求原精确审批：APT 更新、Docker
安装、Compose 部署/关闭、服务重启、容器重启。

## 3. 永久拒绝

- 任意模型提供的 Shell、argv 或 SSH command；
- `shell:true`、`exec/execSync`、动态代码执行；
- 请求自报 `risk` 改变语义路由；
- 未配置服务器、服务、容器或诊断别名；
- fingerprint 不匹配、未知参数、路径逃逸、符号链接凭据；
- 过期请求建立 SSH 连接；
- 将 SSH secrets、审批 key、Docker Socket 挂入 Agent/API。

## 4. Pi 风格事件

每个请求在共享文件锁内追加：

- `ops.runner.claimed`
- `ssh.started`
- `ssh.completed`
- `ops.result.persisted`
- `ops.failed`

事件 payload 写盘前脱敏且有界。Runner 输出还会额外清除代理 URI 与 URL 中的
password/token/secret/API key 参数。

## 5. Python 回滚

显式运行：

```bash
docker compose -f docker-compose.python.yml up -d --build
```

不会加载默认 Node overlay，原 Python Ops Runner 继续处理 read/change/privileged 全部请求。
