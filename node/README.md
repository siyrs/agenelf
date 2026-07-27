# Agenelf Node Runtime

Node.js 24 LTS 原生 TypeScript 实现。核心运行时零第三方 npm 依赖，直接执行 `.ts`。

```bash
npm ci --ignore-scripts
npm run test:node
AGENELF_API_TOKEN=change-me npm run node:start
```

生产迁移与安全边界见 `docs/NODE_MIGRATION.md`。
