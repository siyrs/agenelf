# Agenelf Node.js / TypeScript 迁移基线

> 状态：Node Agent/API/CLI 已是默认生产入口；Validation Runner 已迁移；Node 生产代码已纳入主人授权自升级治理；其余安全 Runner 分批迁移  
> 目标运行时：Node.js 24 LTS 原生 TypeScript type stripping  
> 迁移原则：统一语言栈，但不合并信任域。

## 1. 已迁移能力

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
- Node Validation Queue/Runner：严格 YAML、alias-only、HTTP/TCP、suite、可信结果与 heartbeat；
- Node owner-authorized upgrade scopes、TypeScript 语法、测试哈希保护、永久红线和双运行时控制面；
- 完整 Node、Python、Compose、安全和供应链门禁。

## 2. 为什么不依赖 Fastify/Express/tsx

Node 24.12 起原生 TypeScript type stripping 已稳定。当前核心只使用可擦除 TypeScript
语法，因此可以直接运行 `.ts`，避免在迁移第一阶段引入 npm 安装脚本、框架插件和
额外供应链。后续如需要完整编译、装饰器或前端构建，可在单独依赖审计 PR 中引入。

## 3. 目录

```text
node/
├── apps/
│   ├── api/                 # HTTP + SSE
│   ├── cli/                 # 主人终端
│   ├── runner/              # 通用 Node deterministic runner
│   └── validation-runner/   # 独立软件验证 Runner
├── packages/
│   ├── core/                # Agent/Event/Ledger/Policy/Storage/Model/Validation
│   └── skills/              # 内置技能
├── resources/               # progressive disclosure manifests
├── scripts/                 # 无依赖检查
└── tests/                   # Node built-in test runner
```

## 4. Node 原生 API

- `GET /health`
- `GET /status`
- `GET /capabilities`
- `GET /resources`
- `POST /chat`
- `POST /chat/stream`
- `POST /v1/chat/runs`
- `GET /v1/sessions/:sessionId/runs/:runId/events`
- `GET /validation/catalog`
- `POST /validation/checks/:check`
- `POST /validation/suites/:suite`
- `GET /validation/results/:validationId`

除 `/health`、根跳转和 `/ui/*` 外，全部 API 默认要求 `X-Agenelf-Token`；未配置 token
时 fail-closed。SSE 支持 `Last-Event-ID`/`after_seq`，客户端断开不自动终止已授权 Runner。

Validation 配置缺失或损坏时，Validation API 返回 503 并保持 fail-closed，不会把验证 URL、
host 或 headers 交给模型，也不会静默回退到 legacy API。

## 5. 与 Python 运行时的兼容

迁移期间 Node Agent 继续写入既有 `data/ops-requests`，因此原 approval/ops runner
可不变地消费请求。请求字段、fingerprint、TTL、审批目录与结果目录保持兼容。

Node Validation 继续复用：

- `val-*` ID；
- `software.validation` capability；
- `run_check / run_suite` operation；
- alias-only target；
- 空 `parameters`；
- Python 同源 canonical JSON 与 SHA-256 fingerprint；
- `data/validation-requests/results/locks`；
- `logs/validation.log`。

`docker-compose.python.yml` 保留原 Python Validation Runner 作为明确回滚路径。

主人授权升级仍复用 Python `authorized_upgrade` 工作流和审批/证据协议。Node 扩展只增加
Node scopes、语法、测试保护、红线和双运行时验证，不创建第二套授权事实源。详细说明见
[`NODE_SELF_UPGRADE_GOVERNANCE.md`](NODE_SELF_UPGRADE_GOVERNANCE.md)。

## 6. Runner 与治理迁移进度

### Batch N2：生产 Agent/API/CLI 切换（已完成）

- 默认 Compose 使用 `Dockerfile.node`；
- Python API 仅作为内部 `legacy-agent` 兼容路由，不公开端口；
- 默认 CLI 为 Node，旧 Python CLI 保留 `legacy-cli` profile；
- Web 使用 Node 真实 Event Core，`/chat/stream` 提供旧事件投影；
- Node 原生会话历史、清空、断点 SSE 和内部代理均有集成测试。

### Batch N3.1：Validation Runner（已完成）

- Node Agent/API/Skill 共用 `ValidationQueue`；
- Node Validation Runner 独立容器执行网络检查；
- 模型只能选择主人配置 alias，不能传自由 URL、host 或 headers；
- Runner 不挂载 secrets、profile、memory、self、approval key 或 Docker Socket；
- requests 只读，results/locks/heartbeat/logs 可写；
- HTTP 重定向、响应大小、断言数量、超时、YAML 大小/深度均有边界；
- 真实 HTTP/TCP、suite、篡改、幂等与 Docker E2E 已验收。

### Batch N3.2：Node 主人授权自升级治理（已完成）

- Node runtime、skills、runners、tests、build 与 contracts 有正式 scope；
- 既有 Python 与 Node 测试均由基线 SHA-256 保护，不得修改或删除；
- 生产/控制面变更必须新增回归测试；
- 候选真实执行完整 Python 与 Node 套件；
- diff-aware 红线禁止 Node 任意 Shell、动态代码、TLS 绕过和 npm 生命周期脚本；
- `Dockerfile.control-plane` 提供 network-none Python + Node 可信测试环境；
- Self-upgrade Runner 仍为 Python 控制面，本批没有宣称该 Runner 本体已 Node 化。

### Batch N3.3 及以后

按风险从低到高继续：

1. read-only ops；
2. approval runner；
3. repair runner；
4. change/privileged ops；
5. self-upgrade runner 本体。

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
node node/apps/validation-runner/src/main.ts
```

要求：Node test、Python regression、专项 Runner/Upgrade E2E、Security、CodeQL、Compose smoke 全绿后才能切换默认运行时或扩大自升级范围。
