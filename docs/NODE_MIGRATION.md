# Agenelf Node.js / TypeScript 迁移基线

> 状态：Node Agent/API/CLI、Approval、Validation、read/change Ops、Repair 与 Self-upgrade Runner 已迁移；唯一仍在线的 Python 生产面为 internal legacy API 兼容服务  
> 目标运行时：Node.js 24 LTS 原生 TypeScript type stripping  
> 迁移原则：统一语言栈，但不合并信任域；先证明等价和可回滚，再删除兼容实现。

## 1. 已迁移能力

- Node.js 24 原生 TypeScript 项目骨架，核心运行时零第三方 npm 依赖；
- Agent Core：会话串行、工具循环、Mock/真实 OpenAI 兼容模型网关；
- Event Core：run/turn/reasoning/message/tool/approval/runner 生命周期事件；
- Session Ledger：append-only、branch、hash chain、跨进程目录锁；
- Policy Engine：risk 与 execution mode 分离、缺失 contract fail-closed；
- Skill Registry：内置 Skill、统一合同、统一审计；
- Pi 风格 ResourceLoader：progressive disclosure、trust/source/hash、默认不执行第三方代码；
- Pi 风格 Prompt Templates：内置 Markdown 模板与主人私有覆盖；
- Owner Memory、Node Task Store；
- Node HTTP API、CLI、真实 lifecycle SSE 与断点游标；
- Node Validation Queue/Runner：严格 YAML、alias-only、HTTP/TCP、suite、可信结果与 heartbeat；
- Node Approval Key Init/Broker：主人 CLI 签名、networkless 验签与裁决、Python rollback；
- Node Read-only Ops Runner：固定 SSH 命令目录、语义风险分流、可信结果与事件回放；
- Node Change/Privileged Ops Runner：锁前/锁后精确审批、固定模板、Compose 双重验证、备份与回滚；
- Node Repair Runner：隔离 Git 副本、指纹绑定补丁、主人配置测试 argv、可信 artifact/result/event；
- Node Self-upgrade Runner：双阶段主人授权、候选/目标哈希、完整双运行时测试、一次性核销、原子应用与逆序回滚；
- Node owner-authorized upgrade scopes、TypeScript 语法、测试哈希保护、永久红线和双运行时控制面；
- 完整 Node、Python rollback、Compose、安全和供应链门禁。

## 2. 为什么迁移阶段不依赖 Fastify/Express/tsx

Node 24.12 起原生 TypeScript type stripping 已稳定。当前核心只使用可擦除 TypeScript
语法，因此可以直接运行 `.ts`，避免在迁移阶段引入 npm 安装脚本、框架插件和额外供应链。
未来若引入框架、完整编译或前端构建，必须通过单独依赖审计、锁文件和供应链 PR。

## 3. Node 目录

```text
node/
├── apps/
│   ├── api/                  # HTTP + SSE
│   ├── cli/                  # 主人终端
│   ├── runner/               # 通用 Node deterministic runner
│   ├── approval-key-init/    # 审批 HMAC key 初始化
│   ├── approval-runner/      # networkless 审批 Broker
│   ├── validation-runner/    # 独立软件验证 Runner
│   ├── read-ops-runner/      # 独立只读 SSH Runner
│   ├── change-ops-runner/    # 独立 change/privileged SSH Runner
│   ├── repair-runner/        # 独立无网络代码修复 Runner
│   └── self-upgrade-runner/  # 独立主人授权升级 Runner
├── packages/
│   ├── core/                 # Agent/Event/Ledger/Policy/Model/Validation/Ops/Repair/Upgrade
│   └── skills/               # 内置技能
├── prompts/                  # Pi 风格内置 Prompt Templates
├── resources/                # progressive disclosure manifests
├── scripts/                  # 无依赖检查
└── tests/                    # Node built-in test runner
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

当前仍由 internal `legacy-agent` 代理的路由必须逐项迁移到 Node，不允许通过扩大代理范围掩盖迁移缺口。

## 5. 跨运行时兼容与回滚

### Operations

OperationQueue 继续复用 `op-*`、canonical payload、SHA-256 fingerprint、TTL、请求、
裁决、结果和共享锁目录：

- 语义 read 请求由 Node read runner 处理；
- 已知 change/privileged 请求由 Node change runner 处理；
- 两个 Runner 在共享锁之前按 capability/operation 确定性分流；
- 请求自报 risk 不改变 Runner 选择，且声明风险仍必须与语义一致；
- change runner 在锁前预检查审批，锁后重新读取请求和裁决；
- Python Ops 仅在 `python-ops` profile 中用于诊断；
- 显式 Python rollback 不加载 Node overlay，原 Runner 处理全部操作。

### Approval 与 Validation

Node Validation 继续复用 `val-*` 和 `data/validation-*` 协议。Node Approval 继续复用
`auth-* / op-* / apc-*`、Python canonical HMAC、裁决与结果协议。

### Repair

Node Repair 继续复用：

- `repair-*` ID；
- `code.repair / apply_patch_and_test` capability/operation；
- 原始 UTF-8 patch SHA-256；
- canonical payload fingerprint；
- `data/repair-requests/results/locks` 与 `repair-space`；
- Python-compatible result/evidence 字段；
- 显式 Python Repair rollback。

### Self-upgrade

Node Self-upgrade 继续复用既有授权事实源，不创建第二套审批：

- `upgrade-*` session、`self-upgrade-*` request 与双阶段主人授权；
- exact candidate binding、candidate tree digest、changed-file manifest；
- baseline manifest、test report、目标 before SHA 与 candidate after SHA；
- diff-aware 永久红线和 root-of-trust token；
- 锁前、锁后、测试后授权复核与一次性 `auth-consumed`；
- networkless 双运行时镜像中执行完整 Python + Node 候选测试；
- 原子备份、写入后哈希和失败逆序回滚；
- `self-upgrade-events` 回放、result 和 backup 可信证据；
- Python Self-upgrade 仅在 `python-self-upgrade` profile 与完整 rollback 中保留。

Python 在 control-plane 镜像中暂时作为**候选测试工具**存在，不再作为默认 Self-upgrade 执行进程。

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

### Batch N3.6：Change/Privileged Ops Runner（已完成）

- Node 处理 APT 更新、Docker 安装、Compose 部署/停止、服务和容器重启；
- read/change Runner 保持独立容器、不同决策权限与共同不可变协议；
- 主人裁决在共享锁前和锁后各读取一次，撤销/替换/过期在 SSH 前获胜；
- `OpenSshTransport` 统一 known_hosts、密钥/密码、精确 argv、超时和脱敏；
- Compose 内容经 stdin 写入远端临时文件，不进入命令证据；
- 本地红线和远端 `docker compose config` 双重校验；
- pull/up 失败自动恢复备份并重新部署；
- `compose_down` 不删除 volumes 或 images；
- 真实本机 sshd + Docker 容器重启 E2E、Compose 回滚模拟和完整 Python rollback 已验收。

### Batch N3.7：Self-upgrade Runner（已完成）

- Node 为默认可信应用执行进程，Python 实现仅 profile-gated；
- 请求、会话、双签、候选摘要、证据文件和目标基线全部重新校验；
- 无效时间、候选/目标 symlink 逃逸和测试期间候选变化 fail-closed；
- 完整候选测试后再次核对批准文件，再核销一次性授权；
- 原子 backup/apply/post-write hash 与逆序 rollback；
- append-only `self-upgrade-events`、heartbeat、result 与 backup 证据；
- 真实 networkless owner-authorized candidate E2E 与完整 Python rollback 已验收。

## 7. 后续批次

1. 逐路由移除 internal `legacy-agent` API 代理；
2. 将主人审批 CLI、初始化和剩余宿主辅助脚本迁移或明确归类为 rollback tooling；
3. 生产镜像移除 Python；
4. 将保留的 Python rollback 归档到 `legacy/python/`，并固定 rollback tag；
5. 删除默认拓扑中的 `legacy-agent` 与 Python 依赖。

legacy API 移除必须按 API 合同和数据兼容分批进行，不允许一次删除后以 mock 或缺失页面代替功能。

## 8. Python 退役门槛

- 所有生产 Runner 已有 Node 等价实现和独立 E2E（已满足）；
- internal legacy API 的每条路由均有 Node 等价实现、合同测试和真实 smoke；
- Node API 不再依赖 `AGENELF_LEGACY_API_URL`；
- Web/CLI 不再调用 legacy-only endpoint；
- 默认 Compose 不再启动 `legacy-agent`；
- 最终生产镜像不再安装 Python；
- 保留回滚 tag 与 `docker-compose.python.yml`；
- `app/` 与 Python scripts 归档到 `legacy/python/` 或删除，私有数据不迁移、不丢失。

## 9. 验收

```bash
npm ci --ignore-scripts
npm run test:node
node node/apps/api/src/main.ts
node node/apps/cli/src/main.ts
node node/apps/validation-runner/src/main.ts
node node/apps/read-ops-runner/src/main.ts --once
node node/apps/change-ops-runner/src/main.ts --once
node node/apps/repair-runner/src/main.ts --once
node node/apps/self-upgrade-runner/src/main.ts --once
```

要求：Node test、Python rollback regression、专项 Runner/Upgrade E2E、Security、CodeQL、Compose smoke 全绿后才能切换默认运行时或删除兼容实现。
