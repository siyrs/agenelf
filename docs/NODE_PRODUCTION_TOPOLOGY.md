# Agenelf Node Production Topology

> 状态：Node Agent/API/CLI 已成为默认生产入口；Python API 仅内部兼容；安全 Runner 分批迁移。

## 1. 默认拓扑

```text
浏览器 / CLI / HTTP
        │
        ▼
agenelf (Node.js 24 / TypeScript, public 127.0.0.1:8000)
        │
        ├── Node-native Agent / Event Core / Session Ledger / Memory / Task
        ├── immutable ops requests ──▶ Python ops-runner
        ├── unmigrated API routes ──▶ legacy-agent:8000 (internal only)
        └── optional requests ───────▶ Node deterministic runner

approval-runner / validation-runner / repair-runner / self-upgrade-runner
继续保持独立容器、最小挂载和原可信结果目录。
```

## 2. 为什么保留内部 legacy API

Web 控制台仍包含成长、自治、审批、验证、修复和自升级页面。为了在迁移过程中不丢失
任何功能，Node API 只对尚未迁移的路由使用受限内部代理：

- legacy API 不暴露宿主端口；
- 只有 Node API 可以通过 Compose 内部网络访问；
- 只转发 `Accept`、`Content-Type` 与 `X-Agenelf-Token`；
- 不转发 Cookie、Authorization 或任意客户端请求头；
- 请求上限 1 MiB，响应上限 8 MiB，超时 60 秒；
- Node-native 路由永远优先，不会被 legacy 覆盖。

## 3. Node-native 路由

- `/health`
- `/status`
- `/capabilities`
- `/resources`
- `/chat`
- `/chat/stream`（兼容旧 Web 的 `status/message/done`）
- `/chat/history` GET/DELETE
- `/v1/chat/runs`
- `/v1/sessions/:sessionId/runs/:runId/events`（Pi 风格生命周期 SSE）

其余现有 API 暂时代理到 `legacy-agent`，并在后续批次逐个迁移。

## 4. 数据与挂载边界

Node Agent 只写：

- `local/memory/`
- `local/self/`
- `data/node-tasks/`
- `data/ops-requests/`
- `data/node-runner-requests/`
- `logs/`

Node Agent 只读：

- `data/ops-results/`
- `data/auth-decisions/`
- `data/node-runner-results/`
- profile/preferences/context/docs/web

Node Agent 看不到：

- `local/secrets/`
- Approval HMAC key
- Docker Socket
- Runner 的可写结果目录
- Git 元数据

## 5. 运维命令

```bash
make init
make start
make status
make chat          # 默认 Node CLI
make legacy-chat   # 仅迁移诊断
make test          # Node + Python 全量回归
```

完整 Python 拓扑保存在 `docker-compose.python.yml`，用于明确回滚与差异对比；默认
`docker-compose.yml` 不再公开 Python API 端口。

## 6. Web 流兼容

Node Event Core 是唯一真实事件源。为保证旧 Web 无需同步大改即可工作：

- `/v1/.../events` 返回完整 lifecycle event；
- `/chat/stream` 将同一事件流投影为旧 `status/message/done/error`；
- Web 不再依赖 Python 的“完整回复后切块”伪流式；
- Node 会话历史来自 append-only Session Ledger。

## 7. 下一批迁移

按风险从低到高：

1. Node validation runner；
2. Node read-only ops runner；
3. Node approval broker；
4. Node repair runner；
5. Node change/privileged ops；
6. Node self-upgrade runner；
7. 移除 legacy API；
8. 归档 Python runtime。

每批必须独立 PR、真实测试、Docker smoke、Security、CodeQL，并提供回滚路径。
