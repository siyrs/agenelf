# Registry 统一执行策略验收矩阵

## 目标

证明 K3 的运行时 Policy Engine 已从部分模块接入升级为所有 Skill 工具调用的统一前置决策层，同时不改变现有运维、验证、代码修复和自主沙盒的隔离模型。

## 功能验收

| 场景 | 期望 |
|---|---|
| 纯读取工具 | `execution_mode=pure`，可在策略允许的渠道执行 |
| 本地任务/记忆/反思状态写入 | `execution_mode=local_state`，只修改 Agenelf 有界状态，不要求生产变更授权 |
| 运维、验证、代码修复 | `execution_mode=queued_runner`，副作用只由指纹绑定 Runner 执行 |
| 自主迭代 | `execution_mode=controlled_sandbox`，只进入 `app-tmp`、测试和晋升申请 |
| Skill Forge | `execution_mode=host_controlled`，Agent/HTTP/移动/语音均不能调用 |
| `run_python` | `execution_mode=forbidden`，任何渠道和授权都不能放行 |
| 未分类非只读工具 | Registry 在进入 `Skill.execute` 前默认拒绝 |

## 动态合同验收

- `manage_system_service(action=status)` → `read + queued_runner`；
- `manage_system_service(action=restart)` → `change + queued_runner`；
- 非法 action → 默认拒绝；
- `pursue_improvement_intention(apply_changes=false)` → `local_state`；
- `pursue_improvement_intention(apply_changes=true)` → `controlled_sandbox`；
- 移动端和语音不能直接触发 `controlled_sandbox`。

## 渠道一致性

以下入口必须共享 `SkillRegistry.dispatch(..., subject=...)`：

- 模型工具调用：`agent`；
- CLI：`cli`；
- HTTP：`api` / `http`；
- 手机：`mobile_device`；
- 语音：`voice`。

## 安全验收

- `logs/policy-dispatch.jsonl` 不记录工具参数；
- 审计不出现补丁内容、密码、Token 或主人输入；
- `policy/` 以只读方式挂载到 `agenelf`、`ops-runner`、`validation-runner`、`repair-runner`；
- 策略文件缺失或损坏时，真实 Agent 默认拒绝；
- PolicyEngine、execution policy、Registry 和 capability metadata 均受宿主机 Gate 保护；
- `scripts/validate_governance.py` 同时校验安全策略和执行模式策略。

## 自动化测试

新增测试覆盖：

- 全部内置工具合同完整性；
- 未分类变更工具拒绝；
- 只读能力安全继承；
- 动态合同解析；
- forbidden/host-only 渠道阻断；
- 无参数审计；
- Docker 策略挂载；
- Gate 保护路径。

## 合并门

合并到 `main` 前必须同时通过：

1. GitHub Actions `CI`；
2. `Security & Supply Chain`；
3. `CodeQL`；
4. 治理策略校验；
5. Python 全量编译；
6. 完整 unittest；
7. 干净 local 初始化；
8. Docker Compose 拓扑；
9. 全部 Shell 语法；
10. PR Head SHA 精确绑定合并。
