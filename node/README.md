# Agenelf Node Runtime

Node.js 24 LTS 原生 TypeScript 实现。核心运行时零第三方 npm 依赖，直接执行 `.ts`。

```bash
npm ci --ignore-scripts
npm run test:node
make init
make start
make chat
```

默认 `docker-compose.yml` 已使用 Node Agent/API/CLI；尚未迁移的 Web/API 能力通过无公网端口的
`legacy-agent` 内部兼容，审批、运维、验证、修复和自升级 Runner 仍保持独立信任域。

- 迁移计划：`docs/NODE_MIGRATION.md`
- 生产拓扑：`docs/NODE_PRODUCTION_TOPOLOGY.md`
- 事件协议：`docs/AGENT_EVENT_PROTOCOL.md`
- Session Ledger：`docs/SESSION_LEDGER.md`
