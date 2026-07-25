# Agenelf 多端与语音统一入口

## 原则

CLI、HTTP、Web、Mobile 和 Voice 只是输入渠道，不是新的权限系统。所有渠道必须先生成同一结构的命令信封，再进入 Task Engine、Policy、审批、Runner 和 Evidence。

```text
CLI / HTTP / Web / Mobile / Voice
                |
                v
        CommandEnvelopeStore
                |
                v
       Task Engine + Policy
                |
       capability / approval
                |
                v
        deterministic runners
```

## 命令信封

每个请求包含：

```json
{
  "channel": "voice",
  "actor_id": "owner-sirius",
  "session_id": "phone-session-1",
  "message": "检查 primary 服务器",
  "idempotency_key": "voice-command-0001",
  "authorization_refs": ["task-..."],
  "metadata": {
    "device_id": "phone-1",
    "locale": "zh-CN",
    "transcript_confidence": 0.96,
    "client_version": "1.0"
  }
}
```

落盘位置：

```text
data/channel-requests/
data/channel-idempotency/
logs/channel-envelope.log
```

## 防重放与并发

幂等键在 `actor_id + session_id` 范围内唯一：

- 同一个键、同一个载荷：返回原请求，标记 `replayed=true`；
- 同一个键、不同载荷：拒绝；
- 手机网络重试不会重复创建任务或重复提交高风险操作。

Task Engine 另有 `revision` 乐观并发控制，避免手机和 Web 同时更新任务时静默覆盖。

## 授权边界

渠道只能携带已有授权引用，例如：

```text
auth-...
op-...
task-...
val-...
intent-...
evo-...
```

渠道不能携带原始 Bearer Token、SSH 密码、私钥或 API Key。语音文本中的“我授权删除”也不能独立构成不可逆授权；它只能触发受控确认流程。

## 隐私

持久化前会：

- 对常见密码、Token、API Key 文本脱敏；
- 限制消息长度；
- 只保存 `device_id/locale/transcript_confidence/client_version`；
- 不保存来源 IP、Cookie 或原始认证头。

## 手机和语音实现顺序

1. Web/PWA：复用现有 HTTP API，展示任务、审批、证据和失败原因；
2. Mobile：使用设备登录和安全存储，生成命令信封；
3. Voice：ASR 只产生文本与置信度，仍进入同一命令信封；
4. 高风险确认：展示目标、参数、影响、回滚和有效期，不能只靠一句语音确认；
5. TTS：播报结果，但不得隐藏失败、待审批或未验证状态。

本轮已经完成统一命令信封和防重放核心，尚未宣称手机 APP、ASR 或 TTS 客户端已经实现。
