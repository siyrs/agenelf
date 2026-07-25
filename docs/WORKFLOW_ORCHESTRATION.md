# Agenelf Workflow Orchestration

## 目标

将 Agenelf 从单次工具调用 Agent 演进为可管理长期任务的私人助理。Workflow 不直接获得 SSH、Docker、Git 或模型特权，而是负责把主人目标转换成可恢复任务，并引用各能力域产生的授权和证据。

## 已实现分层

```text
CLI / HTTP / Web / Mobile / Voice
                |
                v
      Channel Command Envelope
                |
                v
       agent.workflow Task Engine
                |
    +-----------+------------+
    |           |            |
server.ops  validation   self-development
    |           |            |
    v           v            v
 op-/auth-    val-       intent-/evo-
    +-----------+------------+
                |
                v
         Evidence Gate
                |
                v
       completed / failed
```

## 标准流程

```text
主人目标
  -> 持久化 Task Definition
  -> Capability Selection
  -> 模型路由（可选，只负责规划）
  -> Authorization Check
  -> Deterministic Execution
  -> Independent Validation
  -> Evidence Archive
  -> Task Completion Gate
  -> Learning / Reflection
```

## 核心责任分离

| 组件 | 可以做 | 不能做 |
|---|---|---|
| Agent/LLM | 理解、拆解、选择能力、解释 | 自己授权、伪造证据 |
| Task Engine | 状态、依赖、暂停恢复、证据引用 | 直接执行外部副作用 |
| Policy | 风险和授权要求 | 调用模型决定放行 |
| Runner | 执行固定动作、写可信结果 | 修改任务目标或策略 |
| Validator | 独立验证结果 | 替代主人高风险授权 |
| Self Development | 沉淀失败和改进意向 | 自动合并 main 或削弱 Gate |

## 任务与步骤状态

任务状态、步骤状态、完成证据和 revision 并发控制详见 [TASK_ENGINE.md](TASK_ENGINE.md)。

关键规则：

- 高风险步骤必须进入 `waiting_approval` 并关联 `op-/auth-`；
- 步骤成功必须关联证据；
- 全部步骤成功后先进入 `verifying`；
- 至少一条可信 `operation/validation/test/promotion` 证据后才能 `completed`；
- 主人取消后不能继续执行未来步骤；
- 多端使用相同 `revision` 防止静默覆盖。

## 模型路由

模型路由详见 [MODEL_ROUTING.md](MODEL_ROUTING.md)。DeepSeek、GPT、GLM、Ollama 只影响规划质量、成本和隐私，不改变任务或授权规则。

## 多端输入

命令信封、防重放和语音授权边界详见 [CHANNELS.md](CHANNELS.md)。所有渠道必须复用同一控制面，禁止语音或移动端绕过权限系统。

## 能力组合示例

### 多服务器部署

```text
inspect -> backup -> deploy(wait approval) -> validate -> complete
```

### 代码修复

```text
reproduce -> isolated patch -> tests -> review/gate -> validation -> PR/promotion
```

### 主动管家

```text
scheduled trigger -> create task -> collect read evidence -> notify owner
                                      |
                                      +-> any change waits for approval
```

## 当前未完成

- 后台 Scheduler；
- DAG 并行执行和补偿动作；
- 通用 `code.repair` 外部仓库执行器；
- `software.release`；
- Web/PWA、Mobile、Voice 客户端。

这些后续模块必须接入已经实现的 Task Engine 和 Channel Envelope，不能另建旁路。
