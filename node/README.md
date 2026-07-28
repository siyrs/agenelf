# Agenelf Node Runtime

Node.js 24 LTS 原生 TypeScript 实现。核心运行时零第三方 npm 依赖，直接执行 `.ts`。

```bash
npm ci --ignore-scripts
npm run test:node
make init
make start
make chat
```

默认生产入口为 Node Agent/API/CLI，默认软件验证也由独立 Node Validation Runner 执行；尚未迁移的
Web/API 能力通过无公网端口的 `legacy-agent` 内部兼容。审批、运维、修复和自升级 Runner 仍保持独立信任域，
显式 `docker compose -f docker-compose.python.yml ...` 可回滚到完整 Python 拓扑。

## Pi 风格 Prompt Templates

内置模板位于 `node/prompts/*.md`，主人可在被 Git 忽略的 `local/prompts/*.md` 中同名覆盖。
CLI 输入 `/` 后可以补全命令，默认提供：

- `/plan <目标>`
- `/review <对象>`
- `/test <功能>`
- `/prompt:<name> <参数>`

模板只展开 Markdown 文本，不加载 JavaScript/TypeScript 扩展，也不会直接获得系统权限。

## Node Validation

Agent/API 只看见主人配置的检查和套件别名，不暴露 URL、Host 或断言详情；独立 Runner 只读请求与配置，
只写结果、锁、审计和 heartbeat。请求使用 canonical fingerprint，篡改会失败关闭。

- 迁移计划：`docs/NODE_MIGRATION.md`
- 本轮迭代：`docs/PI_NODE_ITERATION.md`
- 生产拓扑：`docs/NODE_PRODUCTION_TOPOLOGY.md`
- 事件协议：`docs/AGENT_EVENT_PROTOCOL.md`
- Session Ledger：`docs/SESSION_LEDGER.md`
