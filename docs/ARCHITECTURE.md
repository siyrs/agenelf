# Agenelf 能力架构

## 目标

Agenelf 不是一个不断堆工具的单体机器人。它由可组合的**能力域**组成：

- 主人个性化上下文 `owner.context`
- 可观测自我模型 `agent.self_reflection`
- 持续沉淀与改进意向 `agent.self_development`
- 服务器运维 `server.operations`
- 代码修复 `code.repair`（后续）
- 软件验证 `software.validation`（后续）
- 发布交付 `software.release`（后续）

每个能力域可以包含多个技能和工具，但必须遵守同一份能力契约、风险模型、证据和审计协议。这样可以组合出“发现缺口 → 建立意向 → 修复代码 → 跑测试 → 部署服务器 → 验证服务 → 失败回滚”的完整工作流，而不是让 LLM 用一段任意 Shell 串起所有动作。

## 四层结构

```text
┌──────────────────────────────────────────────────────────────┐
│ 1. owner-local continuity                                    │
│ local/profile + preferences + context + memory + self         │
│ 主人上下文、长期记忆、反思沉淀、改进意向                      │
└───────────────────────┬──────────────────────────────────────┘
                        │ selective / redacted
                        ▼
┌──────────────────────────────────────────────────────────────┐
│ 2. reasoning and capability plane                            │
│ Agent + SkillRegistry + Capability Catalog                   │
│ owner.context / self.reflection / self.development / ...     │
└───────────────────────┬──────────────────────────────────────┘
                        │ structured plans and requests
              ┌─────────┴─────────┐
              ▼                   ▼
┌───────────────────────┐  ┌───────────────────────────────────┐
│ 3A. code sandbox       │  │ 3B. operation queue               │
│ app-tmp + tests + gate │  │ read / change / privileged        │
└───────────┬───────────┘  └─────────────────┬─────────────────┘
            │                                │
            ▼                                ▼
┌──────────────────────────────────────────────────────────────┐
│ 4. host-controlled execution and evidence                    │
│ promote.sh / human decision / deterministic ops-runner        │
│ promotion-history / trusted results / audit                   │
└──────────────────────────────────────────────────────────────┘
```

## 自我发展不是特权层

`agent.self_development` 只能：

- 保存可观测软件状态；
- 把证据沉淀成反思；
- 创建带验收条件的改进意向；
- 生成计划；
- 把明确推进的意向交给既有自主沙盒。

它不能：

- 获取新的文件系统或网络权限；
- 自动读取 `local/secrets/`；
- 直接修改 `app/` 或 Git 主分支；
- 绕过测试、Gate、审批或 Runner；
- 仅凭模型回复把意向标记为完成；
- 把操作性承诺描述成主观意识或情感。

## 能力契约

技能仍使用 `SKILL_META / TOOLS / execute` 协议。新增可选 `CAPABILITY_META`：

```python
CAPABILITY_META = {
    "id": "agent.self_development",
    "name": "持续自我沉淀与改进意向",
    "domain": "agent-governance",
    "version": "0.1.0",
    "composes_with": [
        "agent.self_reflection",
        "software.validation",
        "server.operations",
    ],
    "operations": [
        {"name": "development_status", "description": "成长状态", "risk": "read"},
        {"name": "reflect_and_sediment", "description": "反思沉淀", "risk": "read"},
        {"name": "pursue_intention", "description": "推进意向", "risk": "change"},
    ],
}
```

旧技能不声明 `CAPABILITY_META` 时仍可加载，注册中心会生成兼容描述。工具名全局唯一；冲突技能不会注册，避免 LLM 调用被静默劫持。

## 风险模型

| 级别 | 含义 | 执行方式 |
|---|---|---|
| `read` | 不改变受管系统状态 | 可自动执行；仍需脱敏和限量 |
| `change` | 产生候选代码或有限变更 | 精确策略、测试、Gate 或人类批准 |
| `privileged` | 安装软件、系统级变更 | 精确绑定的人类批准，策略可进一步禁止 |
| `forbidden` | 安全红线 | 永不提交、永不执行 |

反思和创建意向是 `read`：它们只改变 Agent 自己的私有成长记录，不改变服务器或通用代码。推进意向到代码沙盒属于 `change`，仍受自主迭代安全门控制。

服务器批准绑定完整载荷的 SHA-256 指纹：

```json
{
  "capability": "server.operations",
  "operation": "compose_deploy",
  "target": "primary",
  "parameters": {"project": "demo", "compose_yaml": "...", "pull": true}
}
```

因此，“批准服务器 A 的 apt update”不能被复用于“重启服务器 B 的 nginx”，更不能复用于另一份 Compose。

## 权力分离

### Agent：理解、提议与沉淀

- 理解自然语言和组合任务；
- 读取脱敏主人上下文；
- 写 `local/memory` 和 `local/self`；
- 写 `data/ops-requests` 与 `app-tmp`；
- 没有 SSH 私钥、服务器密码或 Docker Socket；
- 只能读取 Runner 结果和人类决定。

### 人类：裁决与晋升

- 在宿主机运行 `scripts/approve.sh` 和 `scripts/promote.sh`；
- 决定文件写入 `data/auth-decisions`；
- 检查代码候选摘要、测试报告和意向验收条件；
- 这些可信目录在 Agent 容器中只读或不可写。

### Ops Runner：服务器执行

- 不调用 LLM；
- 只接受固定操作枚举和固定命令模板；
- 独占 SSH 密钥、known_hosts 与连接环境变量；
- 再次校验目标清单、操作清单、服务清单、Compose 红线和批准指纹；
- 结果写入 `data/ops-results`，在 Agent 容器中只读。

### Gate：自我修改否决

- 比较 `app-tmp` 与 `app-fork`；
- 拒绝安全关键模块变化；
- 拒绝新增代码直接写主人配置、记忆、成长记录或凭据；
- 运行完整测试；
- 生成候选树摘要；
- READY 后代码变化会使晋升失败。

## 组合工作流约定

后续能力应使用以下通用输入输出：

- **输入**：目标、结构化参数、上一步产物引用；
- **输出**：状态、摘要、证据、产物路径、可供下一步使用的数据；
- **状态**：`queued / planned / active / awaiting_approval / awaiting_promotion / succeeded / failed / blocked / completed`；
- **证据**：反思 ID、意向 ID、测试报告、命令退出码、部署状态、健康检查、晋升历史；
- **副作用**：必须声明风险级别，不能隐藏在只读工具中。

组合器只负责依赖关系和步骤状态，不直接获得任何特权。每一步仍由所属能力的策略和执行器控制。

## 下一阶段

1. `software.validation`：HTTP/TCP 健康检查、日志断言、冒烟测试、验收证据归档；
2. `code.repair`：仓库工作树、补丁、单元测试和审查证据；
3. `software.release`：构建产物、版本、发布单和回滚点；
4. Workflow：把意向、修复、验证、运维和发布编排成 DAG，支持失败停止、补偿动作和人工断点。
