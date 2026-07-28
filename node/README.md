# Agenelf Node Runtime

Node.js 24 LTS 原生 TypeScript 实现。核心运行时零第三方 npm 依赖，直接执行 `.ts`。

```bash
npm ci --ignore-scripts
npm run test:node
make init
make start
make chat
```

默认生产控制面已经全部使用 Node：Agent/API/CLI、Approval、Validation、read/change Ops、Repair 与 Self-upgrade 均运行在独立 Node 信任域中。当前仅尚未完成迁移的 Web/API 路由通过无公网端口的 `legacy-agent` 内部兼容；Python Runner 只保留在显式诊断 profile 与完整 rollback 拓扑中。

## Pi 架构能力

- Event Core：run、turn、reasoning、message、tool、approval、runner 生命周期事件；
- Session Ledger：append-only、hash chain、branch、重放与恢复；
- ResourceLoader：progressive disclosure、trust/source/hash；
- Prompt Templates：内置 Markdown 模板与主人私有覆盖；
- Runner events：`ops-events`、`repair-events`、`self-upgrade-events`；
- 可信终态继续以 result、artifact、backup 与 authorization evidence 为事实源。

## Prompt Templates

内置模板位于 `node/prompts/*.md`，主人可在被 Git 忽略的 `local/prompts/*.md` 中同名覆盖。
CLI 输入 `/` 后可以补全命令，默认提供：

- `/plan <目标>`
- `/review <对象>`
- `/test <功能>`
- `/prompt:<name> <参数>`

模板只展开 Markdown 文本，不加载 JavaScript/TypeScript 扩展，也不会直接获得系统权限。

## 运行入口

```bash
node node/apps/api/src/main.ts
node node/apps/cli/src/main.ts
node node/apps/validation-runner/src/main.ts
node node/apps/read-ops-runner/src/main.ts --once
node node/apps/change-ops-runner/src/main.ts --once
node node/apps/repair-runner/src/main.ts --once
node node/apps/self-upgrade-runner/src/main.ts --once
```

## 文档

- 迁移计划：`docs/NODE_MIGRATION.md`
- Pi 迭代决策：`docs/PI_NODE_ITERATION.md`
- 生产拓扑：`docs/NODE_PRODUCTION_TOPOLOGY.md`
- 事件协议：`docs/AGENT_EVENT_PROTOCOL.md`
- Session Ledger：`docs/SESSION_LEDGER.md`
- Self-upgrade Runner：`docs/NODE_SELF_UPGRADE_RUNNER.md`
