# Agenelf Node.js / TypeScript 迁移基线

> 状态：Foundation、生产 Agent/API/CLI 与 Validation Runner 已迁移；其余安全 Runner 分批迁移  
> 目标运行时：Node.js 24 LTS 原生 TypeScript type stripping  
> 迁移原则：统一语言栈，但不合并信任域。

## 1. 已迁移能力

- Node.js 24 原生 TypeScript 项目骨架，运行时零第三方 npm 依赖；
- Agent Core：会话串行、工具循环、Mock/真实 OpenAI 兼容模型网关；
- Event Core：run/turn/reasoning/message/tool/approval/runner 生命周期事件；
- Session Ledger：append-only、branch、hash chain、跨进程目录锁；
- Policy Engine：risk 与 execution mode 分离、缺失 contract fail-closed；
- Skill Registry：内置 Skill、统一合同、统一审计；
- Pi 风格 ResourceLoader 与 Markdown Prompt Templates；
- CLI 斜杠命令自动补全，主人可在 `local/prompts` 中覆盖内置模板；
- Owner Memory、Node Task Store；
- Python Runner 兼容队列：Node Agent 可继续提交 `data/ops-requests`；
- Node deterministic runner：runtime info、受限文件摘要、精确 allowlist command；
- Node Validation Control Plane 与独立 Validation Runner；
- Node HTTP API、CLI、真实 lifecycle SSE 与断点游标；
- Node 单元、集成、安全、篡改与 Compose 联合冒烟测试。

## 2. 为什么不依赖 Fastify/Express/tsx

Node 24.12 起原生 TypeScript type stripping 已稳定。当前核心只使用可擦除 TypeScript
语法，因此可以直接运行 `.ts`，避免在迁移第一阶段引入 npm 安装脚本、框架插件和
额外供应链。后续如需要完整编译、装饰器或前端构建，可在单独依赖审计 PR 中引入。

## 3. 目录

```text
node/
├── apps/
│   ├── api/                # HTTP + SSE
│   ├── cli/                # 主人终端与斜杠补全
│   ├── runner/             # Node deterministic runner
│   └── validation-runner/  # 独立验证执行域
├── packages/
│   ├── core/               # Agent/Event/Ledger/Policy/Storage/Model/Validation
│   └── skills/             # 内置技能
├── prompts/                # 内置 Markdown Prompt Templates
├── resources/              # progressive disclosure manifests
├── scripts/                # 无依赖检查
└── tests/                  # Node built-in test runner
```

## 4. Node 原生 API

- `GET /health`
- `GET /status`
- `GET /capabilities`
- `GET /resources`
- `GET /prompts`
- `POST /prompts/:name/expand`
- `GET /validation/catalog`
- `POST /validation/checks/:name`
- `POST /validation/suites/:name`
- `GET /validation/results/:id`
- `POST /chat`
- `POST /chat/stream`
- `POST /v1/chat/runs`
- `GET /v1/sessions/:sessionId/runs/:runId/events`

除 `/health`、根跳转和 `/ui/*` 外，全部 API 默认要求 `X-Agenelf-Token`；未配置 token
时 fail-closed。SSE 支持 `Last-Event-ID`/`after_seq`，客户端断开不自动终止已授权 Runner。

## 5. 与 Python 运行时的兼容

迁移期间 Node Agent 继续写入既有 `data/ops-requests`，因此原 approval/ops runner
可不变地消费请求。Validation 继续使用既有 `val-*` 请求、fingerprint、结果和审计协议，
默认由 Node Runner 消费；显式 Python rollback Compose 仍由 Python Validation Runner 消费。

兼容期继续保留：

- 独立进程/容器；
- secrets 最小挂载；
- network 边界按 Runner 需要配置；
- immutable request；
- exact approval；
- revoke/expiry/idempotency；
- trusted result/evidence。

## 6. 迁移批次

### Batch N2：生产 Agent/API/CLI 切换（已完成）

- 默认 Compose 使用 `Dockerfile.node`；
- Python API 仅作为内部 `legacy-agent` 兼容路由，不公开端口；
- 默认 CLI 为 Node，旧 Python CLI 保留 `legacy-cli` profile；
- Web 使用 Node 真实 Event Core，`/chat/stream` 提供旧事件投影；
- Node 原生会话历史、清空、断点 SSE 和内部代理均有集成测试。

### Batch N3.1：Validation Runner（本轮完成）

- Node 严格 YAML 子集；
- alias-only catalog 与提交 API；
- canonical fingerprint 与篡改失败关闭；
- HTTP/TCP 有界检查与 suite；
- request `ro`、result/lock/health `rw` 的独立容器；
- 默认 Node、显式 Python 回滚；
- 联合 Docker 冒烟验证真实请求、结果与 heartbeat。

### Batch N3.2+：其余 Runner

按风险从低到高迁移：

1. read-only ops；
2. approval runner；
3. repair runner；
4. change/privileged ops；
5. self-upgrade runner。

每个 Runner 单独批次，不允许一次性重写全部安全控制面。

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
node node/apps/validation-runner/src/main.ts
```

要求：Node test、Python regression、Security、CodeQL、Compose topology 与联合 smoke 全绿后才能合并到主分支。
