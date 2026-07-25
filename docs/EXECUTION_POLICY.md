# Agenelf Registry 统一执行策略

## 为什么需要这一层

风险等级回答“操作有多危险”，执行模式回答“操作在哪里、以什么机制运行”。在此前实现中，Policy Engine 已进入运维、验证和授权队列，但 `SkillRegistry.dispatch()` 仍可直接调用其他 Skill，导致策略入口不统一。

本轮增加 Registry 级中间件，所有模型工具调用、HTTP 工具接口和 CLI 工具接口都必须先解析执行合同，再咨询 Policy Engine，最后才能进入 Skill。

## 执行模式

| execution_mode | 含义 | 典型能力 |
|---|---|---|
| `pure` | Agent 进程内的只读计算或安全目录查询 | 模型目录、能力健康、任务读取 |
| `local_state` | 只修改 Agenelf 自有且有界的任务、记忆、反思或优化状态 | 任务创建、保存记忆、参数微调 |
| `queued_runner` | 只写指纹绑定请求，由确定性隔离 Runner 执行 | 服务器运维、软件验证、代码修复 |
| `controlled_sandbox` | 只修改 `app-tmp` 等受控沙盒，可测试并最多申请晋升 | 自主迭代 |
| `host_controlled` | 仅宿主机或显式 CLI 可调用 | 实验性 Skill Forge |
| `forbidden` | 永久禁用，任何授权都不能放行 | Agent 进程任意 Python 执行 |

## 默认拒绝

- 非只读工具必须有显式合同。
- 动态工具必须先根据受限参数解析合同，例如 `manage_system_service(status/restart)`。
- 未分类工具在生产 Agent 的 Policy Engine 下直接拒绝。
- 只有没有绑定 Policy Engine 的独立兼容单测环境保留旧行为；真实 Agent 始终绑定策略。

## 渠道一致性

```text
CLI / HTTP / Mobile / Voice / Agent
                 │
                 ▼
        SkillRegistry.dispatch
                 │
       resolve execution contract
                 │
          PolicyEngine decision
                 │
                 ▼
              Skill.execute
```

- API 普通请求使用 `http` 主体。
- 手机和语音使用 `mobile_device`、`voice` 主体。
- CLI 使用 `cli` 主体。
- `host_controlled` 工具不会被模型或普通 HTTP 调用。
- 移动端和语音不能直接触发代码沙盒，也不能成为高风险批准人。

## 审计

每次分发追加到：

```text
logs/policy-dispatch.jsonl
```

记录：

- 工具名；
- 能力和操作；
- 风险和执行模式；
- 发起渠道；
- 是否允许；
- 策略版本和原因。

审计**不记录工具参数**，因此不会把代码补丁、密码、Token 或主人内容复制到策略日志。

## 开发要求

新增 Skill 时：

1. 为所有非只读工具在 `app/core/execution_policy.py` 中加入显式合同；
2. 动态风险工具必须实现确定性参数解析；
3. 更新 `policy/execution-modes.v1.yaml`（如新增模式）；
4. 执行完整测试；
5. 确保 `registry.unclassified_tools()` 为空；
6. 同步文档和验收证据。
