# Agenelf Node.js / TypeScript 迁移基线

> 状态：Node Agent/API/CLI、Approval、Validation、read-only Ops 与 Repair 已迁移；Node 生产代码已纳入主人授权自升级治理；其余高风险控制面分批迁移  
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
- Node HTTP API、CLI、真实 lifecycle SSE 与断点游标；
- Node Validation Queue/Runner：严格 YAML、alias-only、HTTP/TCP、suite、可信结果与 heartbeat；
- Node Approval Key Init/Broker：主人 CLI 签名、networkless 验签与裁决、Python rollback；
- Node Read-only Ops Runner：固定 SSH 命令目录、语义风险分流、可信结果与事件回放；
- Node Repair Runner：隔离 Git 副本、指纹绑定补丁、主人配置测试 argv、可信 artifact/result/event；
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
│   ├── approval-key-init/   # 审批 HMAC key 初始化
│   ├── approval-runner/     # networkless 审批 Broker
│   ├── validation-runner/   # 独立软件验证 Runner
│   ├── read-ops-runner/     # 独立只读 SSH Runner
│   └── repair-runner/       # 独立无网络代码修复 Runner
├── packages/
│   ├── core/                # Agent/Event/Ledger/Policy/Model/Validation/Ops/Repair
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

## 5. 与 Python 控制面的兼容

OperationQueue 继续复用 `op-*`、canonical payload、SHA-256 fingerprint、TTL、请求、
裁决、结果和共享锁目录：

- 语义 read 请求由 Node read runner 处理；
- change/privileged 与未知/损坏请求由 Python runner 处理；
- 请求自报 risk 不改变 Runner 路由；
- 显式 Python rollback 不加载 Node overlay，原 Runner 处理全部操作。

Node Validation 继续复用 `val-*` 和 `data/validation-*` 协议。Node Approval 继续复用
`auth-* / op-* / apc-*`、Python canonical HMAC、裁决与结果协议。

Node Repair 继续复用：

- `repair-*` ID；
- `code.repair / apply_patch_and_test` capability/operation；
- 原始 UTF-8 patch SHA-256；
- canonical payload fingerprint；
- `data/repair-requests/results/locks` 与 `repair-space`；
- Python-compatible result/evidence 字段；
- 显式 Python Repair rollback。

主人授权升级仍复用 Python `authorized_upgrade` 工作流和审批/证据协议。Node 扩展只增加
Node scopes、语法、测试保护、红线和双运行时验证，不创建第二套授权事实源。详细说明见
[`NODE_SELF_UPGRADE_GOVERNANCE.md`](NODE_SELF_UPGRADE_GOVERNANCE.md)。

## 6. Runner 与治理迁移进度

### Batch N2：生产 Agent/API/CLI（已完成）

- Node 为默认公网入口；
- Python API 仅作内部兼容路由；
- Web 使用真实 Event Core 与旧 SSE 投影；
- Session Ledger 提供历史、分支、重放与恢复。

### Batch N3.1：Validation Runner（已完成）

- alias-only HTTP/TCP/Suite；
- Runner 无 secrets、approval key 或 Docker Socket；
- 真实 Docker E2E 与 Python rollback 已验收。

### Batch N3.2：主人授权 Node 自升级治理（已完成）

- Node runtime、skills、runners、tests、build 与 contracts 有正式 scope；
- 既有 Python 与 Node 测试由基线 SHA-256 保护；
- 候选真实执行完整 Python 与 Node 套件；
- diff-aware 红线禁止任意 Shell、动态代码、TLS 绕过和 npm 生命周期脚本。

### Batch N3.3：Approval Broker（已完成）

- key 保存在独立卷或明确私有路径；
- 只有主人 CLI 与 networkless Broker 能读取 key；
- Agent/API 不挂载 key；
- Node 签名/验签与 Python canonical/HMAC 兼容；
- 显式 Python Approval rollback 保留。

### Batch N3.4：Read-only Ops Runner（已完成）

- Node 处理 inspect、docker ps/logs/inspect/check 与 service status；
- Python `change-only` 处理 APT、Compose、服务/容器重启、Docker 安装；
- OpenSSH 使用精确 argv、`shell:false` 与固定远程命令模板；
- 主人配置的服务器、服务、容器和检查 alias 重新校验；
- append-only `ops-events` 供 Web/CLI/审计回放，`ops-results` 仍是可信事实源；
- 真实本机 OpenSSH E2E、篡改、过期、脱敏、共享协议与 rollback 已验收。

### Batch N3.5：Repair Runner（已完成）

- Node 在无网络容器中复制主人只读 Git 仓库；
- patch SHA、请求 fingerprint、expected base、保护路径和文件/字节上限重新校验；
- 固定 Git argv 与主人配置测试 argv 使用 `shell:false`，禁止 shell/python `-c`；
- 逃逸符号链接、二进制/重命名补丁和凭据内容 fail-closed；
- 永不 commit、push、merge 或修改源仓库；
- append-only `repair-events` 供 Web/CLI/审计回放，result 与 artifact 仍是可信事实源；
- 真实 Docker 隔离 Git Repair E2E 与完整 Python rollback 已验收。

### 后续批次

按风险从低到高继续：

1. change/privileged Ops；
2. Self-upgrade Runner 本体；
3. 移除 internal legacy API；
4. Python runtime 归档。

每个高风险控制面单独 PR，不允许一次性重写后降低可审计性。

## 7. Python 退役条件

- 所有生产 Runner 已有 Node 等价实现和独立 E2E；
- 关键队列 shadow verification 完成；
- 删除 Python 入口前保留回滚 tag 和 `docker-compose.python.yml`；
- 最终生产镜像不再安装 Python；
- `app/` 归档到 `legacy/python/` 或删除。

## 8. 验收

```bash
npm ci --ignore-scripts
npm run test:node
node node/apps/api/src/main.ts
node node/apps/cli/src/main.ts
node node/apps/validation-runner/src/main.ts
node node/apps/read-ops-runner/src/main.ts --once
node node/apps/repair-runner/src/main.ts --once
```

要求：Node test、Python regression、专项 Runner/Upgrade E2E、Security、CodeQL、Compose smoke 全绿后才能切换默认运行时或扩大自升级范围。
