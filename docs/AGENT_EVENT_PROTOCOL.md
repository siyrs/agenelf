# Agent Lifecycle Event Protocol

> Schema：`contracts/agent-event-envelope.schema.json`  
> Python foundation：`app/core/agent_events.py`  
> Session persistence：`app/core/session_ledger.py`

## 1. 目标

Agenelf 的 Web、CLI、审计、Runner 进度与未来 Node.js/RPC 不能继续依赖互不兼容的
“状态字符串”和最终文本。Agent Event Protocol 提供统一、版本化的运行事实：

- 每次任务有独立 `run_id`；
- 每个 run 的事件按 `seq` 单调递增；
- SSE、CLI 和回放消费同一 envelope；
- durable lifecycle event 自动进入 Session Ledger；
- streaming delta 默认只在有界内存流中存在，不把 reasoning/token 碎片写成长久记录；
- 一个 run 只能产生一个终态事件。

本批只落地 Event Core，不修改 `Agent.chat` 或 API。后续接线必须是小范围、可回滚 PR。

## 2. Envelope

```json
{
  "schema_version": 1,
  "id": "aevt-0123456789abcdef0123",
  "session_id": "default",
  "run_id": "run-0123456789abcdef",
  "seq": 1,
  "type": "run.started",
  "origin": "runtime",
  "ts": "2026-07-27T15:00:00+00:00",
  "transient": false,
  "payload": {}
}
```

字段含义：

| 字段 | 约束 |
|---|---|
| `schema_version` | 当前固定为 1；不兼容变化必须升版本 |
| `id` | 全局事件 ID，`aevt-*` |
| `session_id` | 主人会话标识 |
| `run_id` | 一次 Agent 执行标识，`run-*` |
| `seq` | 单 run 单调递增，从 1 开始 |
| `type` | 生命周期事件类型 |
| `origin` | runtime/owner/runner 等来源；类型不自动代表可信度 |
| `ts` | UTC ISO8601 |
| `transient` | 是否只保留在短期事件缓冲区 |
| `payload` | 递归脱敏、大小有界的 JSON object |

## 3. 事件类型

### Run / Turn

- `run.started`
- `turn.started`
- `run.checkpointed`
- `run.compacted`
- `run.settled`
- `run.failed`
- `run.cancelled`

### Reasoning / Message

- `reasoning.started`
- `reasoning.delta`
- `reasoning.completed`
- `message.delta`
- `message.completed`

Reasoning 只允许转发模型供应商明确返回的 reasoning 字段。`reasoning.delta` 默认 transient，
不写入 Ledger；完成事件应只保留可审计元数据，不应默认持久化完整隐藏推理正文。

### Tool / Approval / Runner

- `tool.preflight`
- `tool.started`
- `tool.delta`
- `tool.completed`
- `approval.required`
- `approval.resolved`
- `runner.started`
- `runner.completed`

安全关键 payload 只能保存脱敏摘要和 reference，原审批签名、Runner result、测试报告与
晋升证据继续保存在现有 authoritative store。Event Ledger 不替代证据核验。

## 4. Transient 与 Durable

默认 transient 类型：

- `reasoning.delta`
- `message.delta`
- `tool.delta`

其它事件默认 durable，并以 `custom` Session Ledger entry 保存完整 envelope。这样可以：

- 重启后恢复 run 的关键状态；
- 避免每个 token/delta 膨胀主人长期数据；
- 保留最终消息、工具结果引用、审批和终态；
- 未来把 authoritative store 切换到 Postgres 时保持 JSONL 审计导出兼容。

## 5. Replay 与游标

`RunEventStream` 提供：

- `events_after(after_seq)`：立即读取；
- `wait_after(after_seq, timeout_seconds)`：等待新事件、终态或超时；
- `snapshot()`：当前缓冲范围与终态；
- 显式 `EventCursorExpired`：游标早于内存缓冲起点时，调用方必须从 Ledger/数据库恢复，
  不能静默漏事件。

未来 SSE：

```text
GET /v1/sessions/{session_id}/runs/{run_id}/events
Last-Event-ID: <seq 或 event id>
```

连接恢复时先从持久化投影补齐 durable event，再切换到内存实时流。

## 6. 并发与终态

- 单个 `RunEventStream` 使用 Condition + RLock 串行分配 seq；
- durable event 先成功写入 Ledger，再对内存订阅者可见；
- 持久化失败时 seq 不前进；
- `run.settled / run.failed / run.cancelled` 只能出现一个；
- 终态之后任何新事件均拒绝；
- Hub 只驱逐终态 run，不会为了腾空间丢弃正在执行的 run；
- 达到活动 run 上限且无终态可驱逐时 fail-closed。

## 7. 隐私与信任

- payload 统一使用 `sanitize_value`；
- password/token/API key/Bearer 等写入前脱敏；
- 单 payload 64 KiB 上限；
- `origin` 只表达来源，不自动赋予可信度；
- `agent_skill` 事件不能被投影为审批、Runner 或验证事实；
- Runner/approval reference 必须回原数据目录或未来数据库核验。

## 8. Node.js 兼容

未来 TypeScript 实现必须：

1. 从 JSON Schema 生成或校验同源类型；
2. 保持 event type、origin、ID 和 seq 语义；
3. 不修改历史 Envelope v1；
4. 支持 JSONL/数据库双向导入导出；
5. 对 terminal、cursor expiry、transient/durable 使用一致行为；
6. 前端只消费协议，不依赖 Python 内部类名。

## 9. 下一步接线验收

后续 PR 必须完成：

- `Agent.chat` 使用显式 per-run event sink，而不是全局可变 listener；
- reasoning listener 支持多个 observer，CLI 与 Web 互不覆盖；
- Registry 暴露脱敏 contract/policy trace；
- Agent 生命周期自动写 Event Core，模型不能选择是否审计；
- 新真实 SSE 支持 heartbeat、断点续传、客户端断开和有界背压；
- 同 session 默认串行 run，不同 session 可并发；
- 旧 `/chat/stream` 保留一轮兼容并标记 deprecated。
