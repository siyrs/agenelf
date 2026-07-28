# Node Native API Cutover

## 状态

Node API 的逐路由迁移已经完成：

- compatibility allowlist：**空**；
- `node/apps/api` 不再包含 `proxyLegacy`；
- Node API 不再读取 `AGENELF_LEGACY_API_URL`；
- 未知端点统一返回 Node `404`；
- 即使环境中残留 legacy URL，任何 API 路由也不会访问 Python upstream。

默认 Compose 中的 `legacy-agent` 尚保留为部署兼容服务，但已经没有 Node API 调用者。下一批将从默认服务图删除它。

## 已迁移端点

### Chat、Event、Validation、Prompt 与 Resources

- `/health`
- `/status`
- `/capabilities`
- `/resources`
- `/prompts`
- `/prompts/:name/expand`
- `/chat`
- `/chat/stream`
- `/chat/history`
- `/v1/chat/runs`
- `/v1/sessions/:session/runs/:run/events`
- `/validation/*`

### Owner Context 与 Memory

- `/local/status`
- `/local/reload`
- `POST /memory`
- `/memory/search`

### Approvals、Tasks 与 Operations

- `/approvals`（永远只读）
- `/tasks`
- `/tasks/:id`
- `/operations/:id`

### Repair、Evolution 与 Self-upgrade Evidence

- `/code-repair/catalog`
- `POST /code-repair/requests`
- `/code-repair/requests/:id`
- `/evolution/status`
- `/self-upgrade/status`

### Self-development

- `/self`
- `/self/assessment`
- `/self/capability-health`
- `/self/roadmap`
- `/self/development`
- `GET/POST /self/reflections`
- `GET/POST /self/intentions`
- `/self/intentions/:id`
- `/self/intentions/:id/pursue`

### Self Optimization

- `GET /self/optimization`
- `POST /self/optimization/apply`
- `POST /self/optimization/rollback`
- `POST /self/optimization/auto`

兼容 `local/self/optimizations.json`，保留：

- 固定参数白名单；
- 类型和硬上下界；
- cooldown；
- active/history/rollback；
- 审计日志；
- `consciousness_claim=false`。

有效覆盖真实进入 Node Runtime：

- `agent.memory_prompt_limit` 控制注入记忆条数；
- `agent.memory_prompt_max_chars` 控制记忆块字符数；
- `llm.temperature` 控制模型请求温度。

### Pi-style Autonomy Cycles

- `POST /autonomy/cycles`
- `GET /autonomy/cycles`
- `GET /autonomy/cycles/:id`

流程固定为：

```text
observe → assess → plan → Node Task → owner-authorized Self-upgrade
```

约束：

- 快照只读取 Node Runtime、Session Ledger、Resources、Prompts、Validation、Runner heartbeat 和 result 元数据；
- 默认 `apply_changes=false`，只生成可审计计划；
- `apply_changes=true` 只创建 Node Task、改进意向和主人授权下一步；
- Autonomy API 不直接修改源码、Git、Runner、策略或宿主机；
- 候选仍必须经过双阶段主人授权、完整 Node/Python 测试、红线、备份和回滚；
- cycle 写入 `data/autonomy-cycles`；
- 生命周期追加到 `data/autonomy-events/*.jsonl`。

## 数据与信任边界

- Memory：`local/memory/node-memory.json`；
- Self-development：`local/self/state.json`、`reflections.json`、`intentions.json`；
- Optimization：`local/self/optimizations.json`；
- Node Tasks：`data/node-tasks`；
- Autonomy：`data/autonomy-cycles`、`data/autonomy-events`；
- Operations：`op-*` 请求/决定/结果协议；
- Repair：`repair-*` 请求和 networkless Runner；
- Self-upgrade：session/request/auth/result/backup/event 证据链。

Node API 不读取：

- `local/secrets`；
- SSH key；
- approval HMAC key；
- Docker Socket；
- Git metadata。

## 验收要求

- Node HTTP 合同测试；
- Optimization 白名单、边界、cooldown、历史和 rollback 测试；
- Runtime temperature/memory 参数真实生效测试；
- Autonomy plan-only、Task、events 和源码零修改测试；
- 源码负向扫描：禁止 `proxyLegacy`、`LEGACY_COMPATIBILITY_PATHS`、`AGENELF_LEGACY_API_URL`；
- 无 Python upstream 的真实 Node 容器 smoke；
- 未知路由 `404`；
- 全量 Node/Python rollback、Runner、Security 与 CodeQL 门禁。

## 下一批：部署层退役

1. 默认 Compose 删除 `legacy-agent`；
2. Node API 删除对 legacy health/dependency 的拓扑要求；
3. `make start/logs/status` 不再包含 legacy 服务；
4. 默认生产 smoke 只启动 Node API 与独立 Node Runners；
5. Python API 只保留在 `docker-compose.python.yml`；
6. 生产镜像和默认安装路径移除 Python；
7. 固定 rollback tag，并将保留代码归档到 `legacy/python/`。

## Pi 架构保留

部署退役不得改变以下事实源：

- Agent Event Core；
- Session Ledger/hash chain/branch/replay；
- ResourceLoader progressive disclosure；
- Markdown Prompt Templates 与主人私有覆盖；
- `ops-events`、`repair-events`、`self-upgrade-events`、`autonomy-events`；
- Runner results、artifacts、backups 和 authorization evidence。
