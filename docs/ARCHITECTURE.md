# Agenelf 能力架构

## 目标

Agenelf 不是一个不断堆工具的单体机器人。它由可组合的**能力域**组成：

- 服务器运维 `server.operations`
- 代码修复 `code.repair`（后续）
- 软件验证 `software.validation`（后续）
- 发布交付 `software.release`（后续）

每个能力域可以包含多个技能和工具，但必须遵守同一份能力契约、风险模型、操作请求和审计协议。这样可以组合出“修复代码 → 跑测试 → 部署服务器 → 验证服务 → 失败回滚”的完整工作流，而不是让 LLM 用一段任意 Shell 串起所有动作。

## 分层

```text
用户自然语言
    │
    ▼
Agent 对话与短期上下文
    │ 识别能力域、拆解步骤、调用工具
    ▼
SkillRegistry + Capability Catalog
    │
    ├── server.operations
    ├── code.repair            (planned)
    ├── software.validation    (planned)
    └── software.release       (planned)
    │
    ▼
结构化 Operation Request
    │
    ├── read ───────────────┐
    ├── change ── Human ────┤
    └── privileged ─ Human ─┤
                            ▼
                    deterministic runner
                            │
                            ▼
                    trusted result + audit
```

## 能力契约

技能仍使用原来的 `SKILL_META / TOOLS / execute` 协议。新增可选 `CAPABILITY_META`：

```python
CAPABILITY_META = {
    "id": "server.operations",
    "name": "服务器运维",
    "domain": "operations",
    "version": "1.0.0",
    "composes_with": ["software.validation", "software.release"],
    "operations": [
        {"name": "inspect", "description": "服务器巡检", "risk": "read"},
        {"name": "compose_deploy", "description": "Compose 部署", "risk": "change"},
    ],
}
```

旧技能不声明 `CAPABILITY_META` 时仍可加载，注册中心会生成兼容描述。工具名全局唯一；冲突技能不会注册，避免 LLM 调用被静默劫持。

## 风险模型

| 级别 | 含义 | 执行方式 |
|---|---|---|
| `read` | 不改变目标状态 | Runner 可自动执行 |
| `change` | 可回滚或影响有限的变更 | 精确绑定的人类批准 |
| `privileged` | 安装软件、系统级变更 | 精确绑定的人类批准，策略可进一步禁止 |
| `forbidden` | 安全红线 | 永不提交、永不执行 |

批准绑定以下完整载荷的 SHA-256 指纹：

```json
{
  "capability": "server.operations",
  "operation": "compose_deploy",
  "target": "primary",
  "parameters": {"project": "demo", "compose_yaml": "...", "pull": true}
}
```

因此，“批准服务器 A 的 apt update”不能被复用于“重启服务器 B 的 nginx”，更不能复用于另一份 Compose。

## 三权分离

### Agent：提议权

- 理解自然语言和组合任务。
- 只能写 `data/ops-requests/`。
- 没有 SSH 私钥、服务器密码或 Docker Socket。
- 只能读取 Runner 结果和人类决定。

### 人类：裁决权

- 在宿主机运行 `scripts/approve.sh`。
- 决定文件写入 `data/auth-decisions/`。
- 该目录在 Agent 容器中为只读挂载。

### Ops Runner：执行权

- 不调用 LLM。
- 只接受固定操作枚举和固定命令模板。
- 独占 SSH 密钥、known_hosts 与连接环境变量。
- 再次校验目标清单、操作清单、服务清单、Compose 红线和批准指纹。
- 结果写入 `data/ops-results/`，在 Agent 容器中为只读挂载。

## 组合工作流约定

后续能力应使用以下通用输入输出：

- **输入**：目标、结构化参数、上一步产物引用。
- **输出**：状态、摘要、证据、产物路径、可供下一步使用的数据。
- **状态**：`queued / awaiting_approval / succeeded / failed / blocked`。
- **证据**：命令退出码、测试报告、部署状态、健康检查结果等。
- **副作用**：必须声明风险级别，不能隐藏在只读工具中。

组合器只负责依赖关系和步骤状态，不直接获得任何特权。每一步仍由所属能力的策略和 Runner 执行。

## 下一阶段建议

1. `software.validation`：HTTP/TCP 健康检查、日志断言、冒烟测试、验收证据归档。
2. `code.repair`：仓库工作树、补丁、单元测试；仅允许在任务沙盒修改。
3. `software.release`：构建产物、版本、发布单、回滚点。
4. Workflow：把上述能力编排成 DAG，并支持失败停止、补偿动作和人工断点。
