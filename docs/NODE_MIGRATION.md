# Agenelf Node.js / TypeScript 迁移基线

> 状态：Foundation 已实现，生产切换分批推进  
> 目标运行时：Node.js 24 LTS 原生 TypeScript type stripping  
> 迁移原则：统一语言栈，但不合并信任域。

## 1. 本批已迁移

- Node.js 24 原生 TypeScript 项目骨架，运行时零第三方 npm 依赖；
- Agent Core：会话串行、工具循环、Mock/真实 OpenAI 兼容模型网关；
- Event Core：run/turn/reasoning/message/tool/approval/runner 生命周期事件；
- Session Ledger：append-only、branch、hash chain、跨进程目录锁；
- Policy Engine：risk 与 execution mode 分离、缺失 contract fail-closed；
- Skill Registry：内置 Skill、统一合同、统一审计；
- Pi 风格 ResourceLoader：progressive disclosure、trust/source/hash、默认不执行第三方代码；
- Owner Memory、Node Task Store；
- Python Runner 兼容队列：Node Agent 可继续提交 `data/ops-requests`；
- Node deterministic runner：runtime info、受限文件摘要、精确 allowlist command；
- Node HTTP API、CLI、真实 lifecycle SSE 与断点游标；
- 13 项 Node 单元/集成测试。

## 2. 为什么不依赖 Fastify/Express/tsx

Node 24.12 起原生 TypeScript type stripping 已稳定。当前核心只使用可擦除 TypeScript
语法，因此可以直接运行 `.ts`，避免在迁移第一阶段引入 npm 安装脚本、框架插件和
额外供应链。后续如需要完整编译、装饰器或前端构建，可在单独依赖审计 PR 中引入。

## 3. 目录

```text
node/
├── apps/
│   ├── api/       # HTTP + SSE
│   ├── cli/       # 主人终端
│   └── runner/    # Node deterministic runner
├── packages/
│   ├── core/      # Agent/Event/Ledger/Policy/Storage/Model/Runner
│   └── skills/    # 内置技能
├── resources/     # progressive disclosure manifests
├── scripts/       # 无依赖检查
└── tests/         # Node built-in test runner
```

## 4. API

- `GET /health`
- `GET /status`
- `GET /capabilities`
- `GET /resources`
- `POST /chat`
- `POST /chat/stream`
- `POST /v1/chat/runs`
- `GET /v1/sessions/:sessionId/runs/:runId/events`

除 `/health`、根跳转和 `/ui/*` 外，全部 API 默认要求 `X-Agenelf-Token`；未配置 token
时 fail-closed。SSE 支持 `Last-Event-ID`/`after_seq`，客户端断开不自动终止已授权 Runner。

## 5. 与 Python 运行时的兼容

迁移期间 Node Agent 继续写入既有 `data/ops-requests`，因此原 approval/ops runner
可不变地消费请求。请求字段、fingerprint、TTL、审批目录与结果目录保持兼容。

这是过渡兼容，不是最终状态。后续批次逐个把 Runner 迁移为 Node，并保留：

- 独立进程/容器；
- secrets 最小挂载；
- network none；
- immutable request；
- exact approval；
- revoke/expiry/idempotency；
- trusted result/evidence。

## 6. 后续批次

### Batch N2：生产 Agent/API/CLI 切换

- 默认 Compose 使用 `Dockerfile.node`；
- Python Agent/API 保留 `legacy-python` profile；
- Web 改用 `/v1/chat/runs` + SSE；
- UAT：聊天、工具、审批等待、Runner result、断线重连。

### Batch N3：Runner 迁移

按风险从低到高迁移：

1. validation runner；
2. read-only ops；
3. approval runner；
4. repair runner；
5. change/privileged ops；
6. self-upgrade runner。

每个 Runner 单独 PR，不允许一次性重写全部安全控制面。

### Batch N4：Python 退役

- 数据双读、Node 单写；
- 关键队列 shadow verification；
- 删除 Python 入口前保留一版回滚 tag；
- 最终生产镜像不再安装 Python；
- `app/` 归档到 `legacy/python/` 或删除。

## 7. 验收

```bash
npm ci --ignore-scripts
npm run test:node
node node/apps/api/src/main.ts
node node/apps/cli/src/main.ts
```

要求：Node test、Python regression、Security、CodeQL、Compose smoke 全绿后才能切换默认运行时。
