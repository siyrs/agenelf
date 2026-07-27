# Agenelf 架构审计与后续推进清单

> 审计基线：`main@b50ddf36d31b117e1ff5f4e614485d280a5d25b0`  
> 审计日期：2026-07-27  
> 覆盖范围：README、Agent Core、Registry/Policy、Skill、Memory/Self、API/Web、
> Channel Envelope、Model Router、Task/Continuation、Runner、Docker Compose、CI、
> Security/SBOM、宿主晋升与自升级链路。

## 1. 执行结论

K3 最新一轮重构是一次**有效的 Python 运行时收敛**，不是一次失败的改动。它修复或
改善了以下关键问题：

- 将两套相近对话循环合并为单一 `Agent.chat`；
- 用显式 priority hook 取代依赖 Skill 文件名顺序的 monkeypatch；
- 将 Skill runtime context 从模块级全局状态迁移到 Registry 实例；
- 补强 Runner supervisor/heartbeat、Docker 运维、自升级恢复和 CLI 斜杠命令；
- 修复 Web 静态目录 Compose 挂载；
- 保留并强化精确审批、撤销、Runner 隔离和可信证据链。

但必须如实记录：**当前主分支仍是 Python 3.12 + FastAPI + Python Runner，Node.js
后端迁移尚未开始。** 根目录没有 Node workspace；Dockerfile、Compose、CI 与全部
运行入口仍以 Python 为中心。后续不得再把“重构完成”表述成“Node.js 迁移完成”。

综合评价：

| 维度 | 当前评价 | 说明 |
|---|---:|---|
| 安全治理 | 8.5/10 | split-runtime、审批、撤销、证据链是核心资产 |
| 运行时可维护性 | 7/10 | 单一主回路与显式 hooks 已明显改善 |
| 会话与事件模型 | 5/10 | 仍以进程内历史和扁平 SSE 为主；Batch 1 开始补账本 |
| 并发与持久化 | 5/10 | 全局 Agent 与多个可变文件 Store 仍缺事务边界 |
| 前端/实时体验 | 6/10 | UI 信息架构可用，但 `/chat/stream` 仍是伪流式 |
| 供应链可复现性 | 5.5/10 | 有 audit/SBOM/gitleaks，但依赖与镜像未完整锁定 |
| Node.js 迁移完成度 | 0/10 | 尚无 Node workspace 或生产 Node 服务 |

## 2. 必须保留的设计

### 2.1 Split Runtime 与最小挂载

必须保留：

- `agenelf`：模型与编排，不持有 SSH secrets 或审批 HMAC key；
- `cli`：主人交互入口，按明确用途读取审批 key；
- `approval-runner`：无网络、独立 HMAC 决策；
- `ops-runner`：唯一读取服务器凭据的运行域；
- `validation-runner`：按主人定义 alias 验证，不接受模型自由 URL；
- `repair-runner`：只读源码、一次性修复空间、可信测试；
- `self-upgrade-runner`：受保护控制面的主人授权升级；
- `app-tmp → gate → promote`：普通自我迭代慢车道。

Node.js 迁移只能更换实现语言，不得把这些服务收回一个“全能 Node 进程”。

### 2.2 Registry 统一执行合同

必须保留：

- `risk` 与 `execution_mode` 分离；
- 所有工具统一在 Registry 边界解析合同并咨询 PolicyEngine；
- 未分类工具 fail-closed；
- `pure / local_state / queued_runner / controlled_sandbox / host_controlled /
  forbidden` 语义保持稳定；
- 参数不写入普通策略审计，避免敏感信息扩散。

未来 TypeScript 版本应直接生成同名 union/types，不应重新发明另一套风险枚举。

### 2.3 精确审批、撤销和可信证据

必须保留并继续加测试：

- 审批与 capability、operation、target、parameter fingerprint 精确绑定；
- TTL、单次消费、撤销、过期与重放拒绝；
- Runner 在执行前重新核验决策；
- 完成状态依赖真实结果、测试或宿主晋升证据；
- 失败/撤销结果证明命令是否已经启动。

### 2.4 主人私有连续性

必须保留：

- `local/` 与通用代码分离；
- profile/preferences/context/memory/self 不进入通用仓库；
- secrets 永远不进入 Agent prompt；
- 记忆、反思、意向、优化记录可迁移、可回滚、可脱敏。

### 2.5 K3 的单一主回路和显式 Hook 管线

这是 K3 本轮最值得保留的内部重构之一：

- `Agent.chat` 是唯一主回路；
- bounded segments/no-progress/checkpoint 逻辑不再由额外模块 monkeypatch；
- LLM wrapper/cycle guard 按显式 priority 组合；
- 同名 hook 覆盖以保持幂等。

Node Agent Core 应继续这一思想，而不是重新堆多套 chat/continue/retry 循环。

## 3. P0：必须优先推进

### P0-1 Node.js 迁移尚未开始

现状：

- 无根 `package.json`/workspace；
- API 是 FastAPI；
- Agent、Skill、Runner、CLI 全部是 Python；
- CI 只编译和测试 Python；
- Docker 基础镜像是 Python。

建议：单独建立迁移 Epic，不再通过“继续重构 Python”代替 Node 迁移。目标顺序：

1. 语言无关 contract；
2. Node monorepo 与共享 types；
3. Node Agent Core/Event Core；
4. Fastify API 与兼容路由；
5. Runner Broker；
6. 各 Runner 按信任域迁移；
7. 数据导入/导出与回滚；
8. Python 运行时退役。

### P0-2 `/chat/stream` 是伪流式

现状是完整执行 `Agent.chat()` 后，将最终文本按固定长度切块发送。它不提供：

- 模型内容首字节实时输出；
- tool preflight/start/delta/completed；
- approval required/resolved；
- runner progress；
- checkpoint/settled；
- cancellation/backpressure。

应实现统一 lifecycle event stream，并保留旧接口一个兼容版本。

### P0-3 全局 Agent 单例和共享可变状态

当前 API 使用进程级 `_agent`，其内部持有：

- 会话历史桶；
- LLM 温度、推理 listener/cache；
- MemoryStore；
- SelfDevelopment/Optimization；
- system prompt；
- Registry runtime context。

并发请求可能发生同一 session 重入、listener 覆盖、历史交错和文件写覆盖。应引入：

- `run_id`；
- 每 session 串行化；
- 独立 run context；
- session/event authoritative store；
- 可取消执行；
- 跨进程正确性锁或数据库事务。

### P0-4 会话历史缺少重启恢复和事件事实源

当前主要会话桶仍在进程内。Batch 1 已落地 Pi 风格的 append-only、tree-shaped、
hash-chained Session Ledger，下一步必须自动接入 Agent 生命周期，而不是要求模型手工
调用 append 工具。

### P0-5 最终主分支必须重新跑完整门禁

K3 重构后的最终 `main` 没有可核验的同提交 PR workflow 结果。PR #24 首次执行安全
门禁后连续发现：

- `watcher.sh` 删除路径缺少 `${REQUESTS_DIR:?}` 防空保护；
- `promote.sh` 遗留未使用变量；
- `git-sync.sh` 遗留未使用变量；
- ShellCheck 循环遇到首个告警就退出，不能汇总完整诊断。

本轮均按 fail-closed 原则修复，未关闭告警。以后大重构完成后必须对最终合并树执行
CI、Security、CodeQL、Compose smoke 和关键 Runner smoke。

## 4. P1：高价值优化

### P1-1 Model Router 目前只是“建议器”

`ModelRouter` 能按任务、能力、成本、隐私返回 alias 和 fallback chain，但 Agent 仍在
初始化时固定一个 `self.llm`。因此“路由结果”不会自动切换真实 provider/client。

未来应拆成：

- `ModelCatalog`：脱敏元数据；
- `CredentialResolver`：仅后端/专门 broker 读取环境变量；
- `ModelGateway`：按 alias 创建或复用 client；
- `RoutingPolicy`：确定性选择；
- `RunContext.modelSelection`：把最终选择写入事件和成本审计。

### P1-2 依赖与镜像尚不可复现

当前：

- `requirements.txt` 多数使用 `>=`；
- `python:3.12-slim` 未固定 digest；
- 多个 GitHub Action 使用可移动 major tag；
- SBOM 是当前安装环境快照，不是锁文件的确定性产物。

建议：

- 当前 Python 过渡期引入 lock/hash；
- Node 迁移使用精确 lockfile；
- CI 默认 `--ignore-scripts`，仅对白名单依赖显式放行；
- 基础镜像和 Actions 固定 digest/full SHA；
- 依赖升级单独 PR。

### P1-3 多个 JSON/YAML Store 缺统一事务边界

Memory、Self、Task、Continuation、Command Envelope 和各 Runner 队列分别维护文件。
原子替换能防止半写，但不能天然解决多个进程之间的复合事务和投影一致性。

建议：

- Session/Run/Event/Approval/Evidence 使用 append-only ledger + projection；
- 单机阶段使用文件锁/SQLite STRICT；
- 产品阶段使用 Postgres；
- Redis 仅做 fan-out/cache，不做审批唯一事实源。

### P1-4 Command Envelope 尚未成为所有入口的统一入口

当前已经有稳定的 channel envelope、nonce 和 idempotency key 设计，但普通 `/chat`、
`/chat/stream` 与部分 CLI 路径仍可绕过这一统一命令事实。后续应把 Web/CLI/Mobile/
Voice 入口统一转换成 command envelope，再进入 run/session 服务。

### P1-5 动态 Skill 需要 Resource Trust 模型

目前 app-space 默认关闭是正确的；一旦开启，Python 模块仍会在 Agent 进程导入执行。
未来应借鉴 Pi 的 ResourceLoader 与 project trust，但加强为：

- manifest/source/version/hash；
- trust decision；
- 启用范围与 owner；
- progressive disclosure；
- 默认只加载文档/Schema，不执行第三方代码；
- 可执行扩展仅在隔离 Runner。

### P1-6 每类 Runner 应使用更小的生产镜像

当前多个 Runner 复用同一 Python 镜像，其中包含它们不一定需要的完整依赖与 `git`。
长期应按信任域构建最小镜像，降低供应链与攻击面。

## 5. P2：整理与退休

### P2-1 Deprecated 兼容壳

例如 `core/continuous_chat.py` 已明确成为兼容 shim。建议：

- 标记 removal version；
- 统计调用方；
- 完成迁移后删除，而不是永久保留。

### P2-2 `app-fork` 历史命名

当前 Compose 已直接把 `./app` 挂到 `/agenelf/app-fork`，功能上解决了 stale copy，
但命名会让新维护者误解仍存在两个源码树。Node 迁移时应统一为明确的 runtime mount，
并保留一次兼容符号或环境变量映射。

### P2-3 API 文件职责过重

当前 `api.py` 同时承载鉴权、静态资源、chat、memory、self、task、approval、validation、
repair 等路由。Fastify 迁移时应按领域插件拆分，而不是一比一翻译成一个巨大 TS 文件。

## 6. 已落地的 Pi Batch 1

本轮 PR 已新增：

- `app/core/session_ledger.py`
- `app/skills/session_ledger.py`
- `contracts/session-ledger-entry.schema.json`
- `app/tests/test_session_ledger.py`
- `app/tests/test_session_ledger_contract.py`
- `docs/PI_ARCHITECTURE_ADOPTION.md`

特性：

- append-only JSONL；
- parent/branch tree；
- `prev_hash + entry_hash` 完整性链；
- recursive redaction；
- payload/ledger 边界；
- API/CLI 跨进程文件锁；
- 篡改、损坏、并发、执行合同和 schema 同步测试；
- 不新增网络、shell、Docker 或 secrets 权限。

## 7. 后续实施顺序

### Batch 2：Agent Lifecycle Event Stream

- `run.started / turn.started / reasoning.delta / message.delta`；
- `tool.preflight / tool.started / tool.delta / tool.completed`；
- `approval.required / approval.resolved`；
- `runner.started / runner.completed`；
- `run.checkpointed / run.settled / run.failed`；
- 自动写入 Session Ledger；
- 新 `/v1/sessions/:id/events` SSE；
- 旧 `/chat/stream` deprecated 兼容。

### Batch 3：Replay、Branch 与 Compaction

- 从 ledger 重建会话；
- 分支切换；
- token 预算驱动 compaction；
- branch summary；
- 重启恢复；
- JSONL 导入/导出。

### Batch 4：ResourceLoader 与 Trust

- capability/skill/prompt/context/UI descriptor 统一发现；
- progressive disclosure；
- source/version/hash/trust；
- 第三方资源默认不可执行。

### Batch 5：Node Agent Core 与 Fastify

- TypeScript monorepo；
- contract-first；
- Event Core；
- ModelGateway；
- Fastify API；
- OpenAPI/JSON Schema 同源；
- 保留独立 Runner 信任域。

### Batch 6：Postgres Projection 与多客户端

- session/run/event/approval/evidence authoritative store；
- advisory locks/transactions；
- JSONL 审计导出；
- SSE fan-out；
- 多 Web/Mobile/Voice 客户端。

## 8. 每批强制验收

1. 独立分支和 PR；
2. 单元、contract、安全与 smoke；
3. 当前最终 head 的 CI/Security/CodeQL 全绿；
4. API/Schema/文档/测试同提交；
5. 明确回滚方案；
6. 不通过修改测试、关闭门禁或放宽策略来“修复”失败；
7. 不在同一批同时重写 Policy、Runner 与持久化三条高风险链路。
