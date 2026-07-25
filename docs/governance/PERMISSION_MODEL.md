# Agenelf 权限与授权模型

## 1. 角色

### Agent

- 理解自然语言；
- 生成结构化计划和请求；
- 无权创建主人授权；
- 无权读取 Runner 凭据；
- 无权直接执行高权限命令。

### Owner

- 定义目标、范围和约束；
- 批准、拒绝、暂停、撤销高风险任务；
- 可以精确授权 `privileged` 和 `irreversible` 操作；
- 不能通过普通授权放行治理绕过型 `forbidden` 行为。

### Policy Decision Point

- 对请求进行风险分类；
- 校验目标、参数、授权、时效和 nonce；
- 输出允许、等待授权或拒绝；
- 不执行实际任务。

### Deterministic Runner

- 不调用 LLM；
- 只执行固定操作模板；
- 再次校验策略和授权；
- 使用所属能力的最小凭据；
- 输出可信结果。

### Evidence Store

- 保存请求、授权、执行、验证、回滚和晋升证据；
- Agent 只能追加提议或读取脱敏结果；
- 不允许 Agent 删除或改写历史证据。

## 2. 权限对象

每次授权作用于一个不可变操作对象：

```json
{
  "capability": "server.operations",
  "operation": "compose_deploy",
  "target": "production-a",
  "parameters": {
    "project": "agenelf",
    "compose_digest": "sha256:..."
  },
  "risk": "privileged",
  "nonce": "...",
  "issued_at": "...",
  "expires_at": "..."
}
```

授权存储的是规范化载荷的哈希，而不是一段自然语言。

## 3. 授权状态机

```text
proposed
  ├─ read ───────────────────────────────> executable
  └─ change/privileged/irreversible
       └─> awaiting_approval
              ├─ approve ────────────────> executable
              ├─ deny ───────────────────> denied
              ├─ expire ─────────────────> expired
              └─ revoke ─────────────────> revoked

executable
  ├─ runner lock ────────────────────────> running
  └─ revoke before start ────────────────> revoked

running
  ├─ success ────────────────────────────> succeeded
  ├─ failure ────────────────────────────> failed
  └─ owner stop at safe checkpoint ──────> cancelled
```

## 4. 一次性消费

Runner 开始执行前必须原子消费授权：

1. 校验请求 ID；
2. 校验载荷指纹；
3. 校验风险级别；
4. 校验有效期；
5. 校验 nonce 未使用；
6. 写入消费记录；
7. 获取任务锁；
8. 开始执行。

任何一步失败都不能接触目标系统。

## 5. 多服务器范围

多服务器任务不应使用一个模糊授权。建议拆分为：

- 一个任务级计划；
- 每台服务器一个具体操作；
- 每个操作一个载荷指纹；
- 主人可以批量批准，但批量决定必须列出全部子请求摘要；
- 任一子任务失败时按工作流策略停止或继续。

## 6. 凭据模型

凭据不属于模型上下文，也不属于普通任务参数。

推荐顺序：

1. 短时证书或短时 Token；
2. 专用低权限服务账号；
3. 受限 SSH Key；
4. 受限 sudoers；
5. 必须时才使用长期高权限凭据。

凭据引用使用别名：

```yaml
credential_ref: prod-ops-ssh
```

只有对应 Runner 能将别名解析成真实凭据。

## 7. Owner Override 语义

主人授权高风险操作时，系统不得以“模型认为危险”为由无限拒绝。正确流程是：

1. 准确说明风险和影响；
2. 收集满足该风险级别的授权证据；
3. 精确绑定载荷；
4. Runner 按载荷执行；
5. 保存结果和回滚证据。

Owner Override 不允许：

- 让 Agent 自己批准；
- 关闭审计；
- 暴露秘密给模型；
- 修改授权记录；
- 绕过 Gate；
- 隐藏执行事实。

## 8. 渠道统一

CLI、HTTP、Web、手机和语音必须调用同一个授权服务。渠道只能改变交互体验，不能改变风险分类和执行权限。
