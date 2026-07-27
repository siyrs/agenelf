# Session Ledger 协议与信任语义

> Schema：`contracts/session-ledger-entry.schema.json`  
> 当前实现：`app/core/session_ledger.py`  
> 模型能力入口：`app/skills/session_ledger.py`

## 1. 定位

Session Ledger 是 Agenelf 的主人本地、append-only 会话/运行事件账本。它借鉴 Pi 的
树状 session entry 模型，用 `id / parent_id` 表达分支，同时增加：

- `branch_id`：稳定分支标识；
- `seq`：文件追加顺序；
- `prev_hash + entry_hash`：追加顺序完整性链；
- `origin`：事件来源；
- recursive redaction：写盘前递归脱敏；
- 跨进程文件锁：API 与 CLI 共享 bind mount 时保持序号与哈希链一致。

JSONL 是语言无关的迁移/审计格式。未来 Node.js Agent Core 必须复用同一 JSON Schema，
不得重新定义不兼容的 session 格式。

## 2. Entry 结构

```json
{
  "schema_version": 1,
  "id": "evt-0123456789abcdef",
  "session_id": "default",
  "seq": 1,
  "parent_id": null,
  "branch_id": "main",
  "type": "message",
  "origin": "runtime",
  "ts": "2026-07-27T15:00:00+00:00",
  "payload": {},
  "prev_hash": "",
  "entry_hash": "...sha256..."
}
```

## 3. 核心信任规则

### 3.1 Event type 绝不等于可信证明

`approval_ref`、`evidence_ref`、`tool_result` 等名称只表达事件语义，不能单独证明：

- 审批真实存在；
- Runner 真实执行；
- 测试真实通过；
- 晋升真实完成。

消费者必须根据 `origin` 和 payload 中的 reference，回到 authoritative store 核验：

- 审批：签名 decision/approval result；
- 运维：ops result；
- 验证：validation result；
- 修复：repair result；
- 晋升：promotion history/self-upgrade result。

Ledger 是可回放索引和审计链，不替代原可信事实源。

### 3.2 Origin 枚举

| origin | 含义 | 默认可信度 |
|---|---|---|
| `agent_skill` | 模型通过受控 Skill 主动追加 | 低信任叙事，不能作为安全证明 |
| `owner` | 主人明确输入或宿主命令产生 | 需结合入口身份/签名核验 |
| `runtime` | Agent Core 生命周期自动产生 | 可作为运行轨迹，不自动证明外部副作用 |
| `runner` | 可信 Runner 自动产生 | 必须继续核验 Runner result/hash/signature |
| `migration` | 数据迁移产生 | 需结合 migration batch/hash 对账 |

### 3.3 模型可写类型

模型工具 `session_ledger_append` 只允许：

- `message`
- `checkpoint`
- `reflection`
- `intention`
- `label`
- `custom`

模型不能写：

- `tool_call`
- `tool_result`
- `approval_ref`
- `evidence_ref`
- `compaction`
- `branch_summary`（只能通过专用 branch 工具生成）

即使模型在 payload 中自称“已审批”或“已验证”，也不能改变 `origin=agent_skill`，不能
被 Task/Approval/Promotion 投影视为可信证据。

## 4. 树与哈希链

- `parent_id` 必须指向同一 session 中已经存在的 entry；
- 默认 append 以最后 entry 为 parent；
- 创建 branch 时可指定任意历史 parent，并生成 `br-*`；
- `prev_hash` 连接物理追加顺序；
- `entry_hash` 覆盖除自身外的完整 canonical JSON；
- `verify()` 同时检查 schema version、ID、seq、parent、branch、type、origin 和哈希链。

树表达语义分支，hash chain 表达文件追加完整性，两者不能互相替代。

## 5. 并发与存储

路径：

```text
local/memory/session-ledger/<session_id>.jsonl
local/memory/session-ledger/<session_id>.jsonl.lock
```

- 同进程使用 `RLock`；
- POSIX 使用 `flock`；
- Windows 使用一字节 `msvcrt` advisory lock；
- 写者在锁内读取尾部、计算 seq/hash、追加并 fsync；
- 只读访问不存在的 ledger 不创建目录；
- 单 payload 与单 ledger 都有大小上限；
- payload 写盘前使用统一 privacy sanitizer。

当前文件锁适合单机 Compose。未来多节点部署应把 authoritative event store 迁移到
Postgres transaction/advisory lock，JSONL 继续作为审计导出格式。

## 6. 后续自动接线

当前 Batch 1 提供独立 Store 和显式 Skill。Batch 2 必须由运行时自动记录：

- run/turn/message；
- tool preflight/start/delta/completed；
- approval required/resolved；
- runner started/completed；
- checkpoint/compaction/settled/failed/cancelled。

安全关键事件不能依赖模型主动调用 append，也不能允许模型选择“不要记录”。

## 7. Node.js 兼容要求

Node 实现必须：

1. 读取并验证当前 Schema v1；
2. 使用完全相同的 canonical JSON 与 SHA-256 规则；
3. 保留 origin 和信任解释；
4. 提供幂等 importer/exporter；
5. 支持 JSONL → Postgres projection；
6. 不因迁移改变已有 entry hash；
7. 新 Schema 使用显式版本和迁移工具，不能原地静默改写历史记录。

## 8. 测试要求

至少覆盖：

- 顺序 append 与默认 parent；
- 历史分支；
- recursive redaction；
- 篡改检测；
- 损坏 parent/origin 安全失败；
- 非法 ID/origin/type/超大 payload 拒绝；
- API/CLI 多进程并发写入保持连续 seq/hash；
- 模型工具无法写安全关键事件；
- Schema 与实现枚举同步。
