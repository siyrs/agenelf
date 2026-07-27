# Pi 架构借鉴与 Agenelf 后续演进基线

> 状态：已接受（Batch 1 已开始落地）  
> 适用范围：当前 Python 主干、后续 Node.js + TypeScript 重构、Web/CLI/Runner 全链路  
> 核心原则：**统一语言栈，但不统一信任域。**

## 1. 当前主分支的真实状态

K3 的最新重构完成了大量有价值的 Python 架构收敛，但当前 `main` **仍不是
Node.js 后端**：

- 运行入口仍为 FastAPI、Python Agent Core 与 Python Runner；
- CI 仍以 Python 3.12、`compileall` 和 `unittest` 为主；
- 根目录没有 Node workspace 的 `package.json`；
- 最新重构主要合并了重复 continuation 运行时、加强 Docker 运维、自升级恢复、
  审批 wiring、运行时 doctor 和 CLI 交互。

因此，后续工作不能把“Python 重构完成”等同于“Node.js 迁移完成”。本文件将
Pi 的优秀设计先沉淀为语言无关契约，再逐步接入当前运行时和未来 Node Agent Core。

## 2. 必须保留的 Agenelf 资产

以下能力比 Pi 默认安全模型更适合 Agenelf，迁移或重构时不得削弱：

1. **Split Runtime**
   - Agent/API、approval、ops、validation、repair、self-upgrade 继续分进程/分容器；
   - 主 Agent 不读取 `local/secrets/`，不持有 Docker Socket；
   - Runner 使用最小挂载、最小网络和最小权限。

2. **统一 Policy Engine**
   - 所有工具调用在 Registry 边界先解析 execution contract；
   - 风险级别与执行位置分离；
   - 未分类或策略缺失时 fail-closed。

3. **精确审批与可信证据**
   - 审批绑定 capability、operation、target、parameters fingerprint；
   - TTL、单次消费、撤销、过期、重放拒绝必须保留；
   - “完成”必须有测试、Runner 结果或宿主晋升证据。

4. **安全自我升级**
   - 普通改动走 `app-tmp → test → gate → promotion`；
   - 受保护控制面走主人明确授权的独立 self-upgrade runner；
   - 不允许修改测试、策略或 Runner 来伪造成功。

5. **主人私有连续性**
   - `local/profile`、preferences、context、memory、self 不随通用代码升级覆盖；
   - 记忆、反思、意向和优化记录继续脱敏、可审计、可回滚。

## 3. 从 Pi 直接吸收的设计

### 3.1 Event-first Agent Core

运行时状态不再只靠“最终文本”表达，统一为结构化事件：

- `run.started`
- `turn.started`
- `message.delta`
- `message.completed`
- `tool.preflight`
- `tool.started`
- `tool.delta`
- `tool.completed`
- `approval.required`
- `approval.resolved`
- `runner.started`
- `runner.completed`
- `run.compacted`
- `run.checkpointed`
- `run.settled`
- `run.failed`

事件必须有稳定 envelope、序号、session/run 关联和版本号。Web、CLI、审计、回放
和未来 RPC 共同消费同一事件事实，不各自发明状态枚举。

### 3.2 Tree-shaped Session Ledger

借鉴 Pi 的 `id / parentId` 树结构，但增强为 Agenelf 的主人本地、可审计账本：

- append-only；
- `parent_id` 表达分支；
- `branch_id` 标识活跃路径；
- `prev_hash + entry_hash` 提供追加顺序的篡改检测；
- payload 写入前递归脱敏；
- JSONL 是可迁移、可导出格式；未来主存可切换到 Postgres；
- 事件账本和“当前状态投影”分离。

Batch 1 已新增：

- `app/core/session_ledger.py`
- `app/skills/session_ledger.py`
- `contracts/session-ledger-entry.schema.json`
- `app/tests/test_session_ledger.py`

这批能力先提供显式 append/list/get/branch/verify 工具。后续 Batch 2 再把 Agent
对话、工具、审批、Runner 和反思自动写入账本。

### 3.3 Tool Lifecycle Hooks

未来 Agent Core 应正式提供：

```text
beforeToolCall
  → contract resolve
  → policy decision
  → approval / deny / rewrite
  → execution
afterToolCall
  → redaction
  → evidence
  → event append
  → projection update
```

不得把审批、路径保护、脱敏和审计散落在各 Skill 的 `if/else` 中。

### 3.4 Progressive Disclosure Resource Loader

统一发现并加载：

- capabilities
- skills
- prompt templates
- context files
- model providers
- UI descriptors

启动时只注入名称、描述、风险、版本和健康状态；匹配任务后才加载完整 Skill 文档和
资源，减少系统提示膨胀。第三方资源必须增加 trust decision、来源、版本、hash 和
启用范围，不能照搬 Pi 的“扩展拥有当前进程全部权限”。

### 3.5 SDK / RPC / Web 三层接口

长期目标：

- Node SDK：同进程嵌入 Agent Core；
- Runner RPC：严格 JSONL 或版本化消息协议；
- 浏览器：HTTP 控制面 + SSE 事件流；
- 只有需要双向低延迟协同时才引入 WebSocket。

## 4. 不直接照搬 Pi 的部分

- 不采用“核心无 permission system”的默认立场；
- 不把扩展直接运行在拥有全部宿主权限的 Agent 进程；
- 不用 TUI 假设设计 Web 产品；
- 不用单机 JSONL 文件代替未来多客户端场景的事务主存；
- 不通过递归拉起 Agent 进程模拟正式多 Agent；
- 不把 shell 当成所有能力的通用逃生口。

## 5. 当前审计发现与优先级

### P0

1. **Node.js 迁移尚未发生**
   - 当前构建、运行、测试、Docker 仍是 Python；
   - 需要单独建立 Node workspace 和可回滚迁移批次。

2. **`/chat/stream` 仍是伪流式**
   - 当前先执行完整 `Agent.chat()`，再把最终文本切块；
   - 应新增真实生命周期事件端点，保留旧接口一轮兼容。

3. **全局 Agent 单例存在并发状态风险**
   - 会话桶、LLM 温度、system prompt、memory/self 写入共享可变状态；
   - 需要 run/session 隔离、写入串行化和持久化存储。

4. **会话历史仍主要在进程内**
   - 进程重启后对话桶丢失；
   - Session Ledger 是第一步，随后需要自动接线和状态投影。

### P1

- API、事件和 Runner 协议缺少统一语言无关 schema；
- Skill/Capability/Execution Contract 存在多来源维护；
- 依赖与镜像可复现性仍需继续锁定；
- Web UI 缺少完整事件回放、运行图和 E2E；
- 最新大重构需要以当前 main 再跑一次完整集成 smoke，而不能只依赖旧 PR 结果。

## 6. 分批实施路线

### Batch 1：Session Ledger Foundation（本轮）

- 语言无关 JSON Schema；
- append-only JSONL；
- tree parent/branch；
- hash-chain integrity；
- recursive redaction；
- pure/local_state 工具合同；
- unit tests 与篡改测试。

### Batch 2：Agent Lifecycle Event Wiring

- 给 `Agent.chat` 增加 `event_sink` / async iterator；
- Registry 返回 policy/contract/execution trace；
- 自动写入 run、turn、tool、approval、checkpoint、settled 事件；
- 新增 `/v1/sessions/{id}/events` 和真实 SSE；
- 旧 `/chat/stream` 保持兼容但标记 deprecated。

### Batch 3：Session Projection & Compaction

- 从 ledger 重建历史；
- 分支切换与 label；
- token 预算驱动 compaction；
- branch summary；
- 重启恢复与 deterministic replay；
- JSONL 导入导出。

### Batch 4：Resource Loader

- Skill 元数据按需加载；
- trust/source/version/hash；
- capability、prompt、context、UI descriptor 统一发现；
- 第三方扩展默认不执行代码。

### Batch 5：Node.js Agent Core

- TypeScript monorepo；
- Agent event contract 与 Python ledger 完全兼容；
- Fastify API；
- OpenAPI/JSON Schema 同源类型；
- Runner trust domains 保持独立；
- Python 数据可导入、可导回、可回滚。

### Batch 6：Postgres Authoritative Store

- sessions/runs/events/approvals/evidence 投影；
- JSONL 继续作为审计导出；
- correctness lock 使用数据库事务或 advisory lock；
- Redis 只做 fan-out/cache，不作为审批与晋升唯一真相源。

## 7. Batch 1 验收标准

- 同 session entry 的 `seq` 严格递增；
- 默认 parent 指向上一 entry；
- 可从任意已有 entry 创建分支；
- `prev_hash` 和 `entry_hash` 任一被修改时 verify 失败；
- payload 中 password/token/API key 等字段写盘前被脱敏；
- 非法 session、entry、branch、event type 被拒绝；
- 单 entry payload 有界；
- schema 枚举与 Python 实现由测试保持同步；
- Skill 在 capability catalog 中被分类为 pure/local_state，不成为未分类工具；
- 不新增 SSH、Docker、shell、网络或 secrets 权限。

## 8. 后续提交纪律

每个 Batch 必须：

1. 独立分支和 PR；
2. 单元、契约、安全和 smoke 测试；
3. 保留旧接口或提供明确迁移/回滚；
4. 不在同一批次同时重写 Policy、Runner 和数据格式；
5. CI 成功后才进入 main；
6. 文档、schema、实现和测试同提交更新。
