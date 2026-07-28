# Node Native API Cutover

## 目标

逐路由删除 `node/apps/api` 对 internal Python `legacy-agent` 的依赖，直到默认生产拓扑不再启动 Python API。

迁移不以修改 URL 或删除页面作为完成标准。每个旧端点必须满足：

1. Node 原生实现；
2. 与既有 owner-local、queue、result 和 evidence 文件兼容；
3. HTTP 合同测试；
4. 未配置 legacy upstream 的真实 Node 容器 smoke；
5. 已迁移路由即使配置 legacy URL，也不得访问 Python；
6. 未知路由不得自动穿透 legacy。

## Batch A：可信 Node 底层复用

已迁移：

- `/local/status`
- `/local/reload`
- `POST /memory`
- `/memory/search`
- `/approvals`
- `/tasks`
- `/tasks/:id`
- `/operations/:id`
- `/code-repair/catalog`
- `POST /code-repair/requests`
- `/code-repair/requests/:id`
- `/evolution/status`
- `/self-upgrade/status`
- `/self`
- `/self/assessment`
- `/self/capability-health`
- `/self/roadmap`
- `/self/development`
- `GET/POST /self/reflections`
- `GET/POST /self/intentions`
- `/self/intentions/:id`
- `/self/intentions/:id/pursue`

### 数据与安全约束

- Memory 继续使用 `local/memory/node-memory.json`；
- Self-development 兼容 `local/self/state.json`、`reflections.json`、`intentions.json`；
- Node Tasks 使用 `data/node-tasks`，只读兼容旧 board/engine tasks；
- Approvals API 永远只读，不能创建裁决；
- Operations 继续复用 `op-*` 请求/决定/结果协议；
- Repair API 只提交 `repair-*` 不可变请求，补丁与测试只能由 networkless Runner 执行；
- 返回数据统一脱敏，不读取 `local/secrets`、approval key 或 Git metadata；
- 意向 `pursue` 只创建 Node Task，涉及代码变更时要求主人继续走 owner-authorized Self-upgrade。

## 当前显式 compatibility allowlist

仅以下路由可以暂时访问 internal legacy API：

- `/self/optimization*`
- `/autonomy/cycles*`

规则：

- 配置了 legacy URL：转发并添加 `X-Agenelf-Compatibility: legacy-allowlist`；
- 未配置 legacy URL：返回 `501` 和 `migration_pending=true`；
- 其它未知路由：返回 `404`；
- 不允许重新引入全路径 fallback。

## 后续 Batch

### Batch B：Self Optimization

迁移 `local/self/optimizations.json`：

- 参数白名单；
- 类型和上下界；
- cooldown；
- active/history/rollback；
- 审计记录；
- Node Runtime 实际读取有效覆盖。

完成后从 allowlist 删除 `/self/optimization*`。

### Batch C：Autonomy Cycles

迁移 `data/autonomy-cycles`：

- Pi 风格 observe → assess → plan；
- append-only cycle events；
- 默认 plan-only；
- `apply_changes=true` 只能创建 Node Task 和 owner-authorized Self-upgrade 意图；
- 禁止 API/Agent 直接修改源码、提交 Git 或绕过测试。

完成后从 allowlist 删除 `/autonomy/cycles*`。

### Batch D：删除 internal legacy API

必须同时满足：

- compatibility allowlist 为空；
- `AGENELF_LEGACY_API_URL` 不再被读取；
- Node API/CLI/Web 合同和真实 smoke 全绿；
- 默认 Compose 不启动 `legacy-agent`；
- Python API 只存在于显式 rollback 拓扑；
- 生产 Node 镜像不安装 Python。

## Pi 架构保留

API cutover 不改变以下事实源：

- Agent Event Core；
- Session Ledger/hash chain/branch/replay；
- ResourceLoader progressive disclosure；
- Markdown Prompt Templates 与主人私有覆盖；
- `ops-events`、`repair-events`、`self-upgrade-events`；
- Runner results、artifacts、backups 和 authorization evidence。
