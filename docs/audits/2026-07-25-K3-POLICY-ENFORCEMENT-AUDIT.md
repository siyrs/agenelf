# K3 Policy Engine 后续架构审计

日期：2026-07-25

## 结论

K3 新增的统一 Policy Engine、双签名审批和供应链 CI 明显提升了治理成熟度，但运行时策略只接入了运维、验证和授权队列。其余 Skill 仍由 `SkillRegistry.dispatch()` 直接调用，因此“统一策略引擎”尚未形成真正的单一执行入口。

## P0

### Registry 缺少统一策略前置检查

影响：

- 本地状态 Skill、实验 Skill、自主沙盒和 Runner Skill 使用不同的自定义防护；
- 新增 Skill 忘记接入策略时仍可被模型直接调用；
- 移动端/语音渠道规则无法自动覆盖所有工具；
- 审计无法回答一次工具调用在什么合同下被允许。

处理：

- 所有调用统一经过 `resolve_contract -> PolicyEngine -> Skill.execute`；
- 未分类非只读工具默认拒绝；
- 参数动态决定风险的工具在执行前解析；
- 审计不保存工具参数。

## P1

### 风险和执行方式混在一起

此前只有 `read/change/privileged/irreversible/forbidden`，但以下操作不能仅靠风险区分：

- 读取服务器状态：风险是 read，但必须进入 SSH Runner；
- 创建任务：风险是 change，但只改变 Agenelf 本地状态；
- 代码修复：风险是 read，但会在无网络 Runner 中执行测试；
- 自主迭代：风险是 change，但只能进入 `app-tmp` 沙盒；
- Skill Forge：风险是 change，但应限制为宿主机/CLI 实验入口。

处理：新增 `pure/local_state/queued_runner/controlled_sandbox/host_controlled/forbidden` 六类执行模式。

## P2 后续建议

1. 将显式合同从内置 Python 表迁移为签名/版本化清单，同时保留代码生成类型检查；
2. 将 `task_board` 数据迁移到 `TaskEngine` 的轻量视图，减少双状态源；
3. 为 ModelRouter 增加真实调用的成本、延迟、错误率和质量回放；
4. 将共享 Runner 逐步升级为每请求一次性容器；
5. 对依赖锁、SBOM 和漏洞例外建立到期机制。

## 本轮验收

- 内置工具无未分类项；
- 未知非只读工具默认拒绝且不进入 `execute`；
- `run_python` 在 Registry 层和 Skill 层双重禁止；
- `forge_skill` 不能由 Agent/HTTP 调用；
- 动态工具参数映射正确；
- API、CLI、模型调用共用 Registry 中间件；
- 策略审计不包含参数或凭据；
- 治理校验、完整单测、初始化、Docker 拓扑和 Shell 校验全部通过。
