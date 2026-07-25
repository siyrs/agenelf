# Agenelf 治理总纲（人类可读版）

> 版本：1.0
> 日期：2026-07-25
> 定位：治理规则的人类可读总纲。机器可校验的单一真源是 `policy/*.yaml` 与 `scripts/validate_governance.py`；本文件与它们保持事实一致，如不一致，以机器策略为准并立即修订本文件。

本文件直接回答五个问题：**谁能做、能做什么、什么时候做、做到什么程度、出问题如何追责与回滚。**

相关文件：

- 机器策略：`policy/safety-constraints.v1.yaml`、`policy/task-engine-constraints.v1.yaml`、`policy/channel-constraints.v1.yaml`、`policy/model-routing-constraints.v1.yaml`、`policy/workflow-constraints.v1.yaml`
- 静态校验：`scripts/validate_governance.py`（CI 强制执行）
- 详细语义：`docs/governance/SAFETY_POLICY.md`、`docs/governance/PERMISSION_MODEL.md`、`docs/governance/SELF_EVOLUTION_RULES.md`

## 1. 谁能做：角色与权力边界

| 角色 | 能做什么 | 不能做什么 |
|---|---|---|
| 主人 | 设定目标与范围、授权高风险操作、暂停、撤销、回滚决定 | —（但主人的授权也不能放行 `forbidden` 行为，见 `owner_authorization.never_overrides_forbidden: true`） |
| Agent（含 LLM） | 理解意图、提出计划、生成候选补丁、读取证据 | 不是授权主体；`model_governance.model_is_untrusted_planner: true`，`model_output_never_counts_as_owner_authorization: true` |
| 确定性 Runner | 按绑定载荷执行已授权操作，输出结构化证据 | 不调用 LLM、不做策略裁量、不提供自由 Shell |
| Task Engine / 编排器 | 编排任务状态、依赖和证据引用 | 不直接执行服务器命令、代码补丁或模型调用（`policy/task-engine-constraints.v1.yaml` 的 `no_direct_execution`） |

职责分离原则（`separation_of_duties`）：Agent 负责理解和提议，主人负责裁决高风险操作，确定性 Runner 负责执行并输出证据。

## 2. 能做什么：风险级别

与 `policy/safety-constraints.v1.yaml` 的 `risk_levels` 一致：

| 级别 | 含义 | 默认行为 | 审批 |
|---|---|---|---|
| `read` | 不改变受管目标状态的查询、巡检、验证和证据读取 | 自动执行，仍要求留证据 | 无需审批 |
| `change` | 改变目标状态但通常可回滚、影响范围有限（如 `service_restart`、`compose_deploy`） | 不自动执行 | `owner_exact` |
| `privileged` | 系统级安装、权限、网络、运行时或生产配置变更 | 不自动执行；支持时先 dry-run | `owner_elevated` |
| `irreversible` | 可能永久删除、覆盖或破坏数据 | 不自动执行；可能时先备份；必须二次确认 | `owner_irreversible` |
| `forbidden` | 治理绕过和隐蔽行为 | 永不执行 | 不可授权（`approval: impossible`） |

危险不等于禁止。主人可以精确授权 `privileged` 和 `irreversible` 操作；有效授权后 Runner 按绑定载荷忠实执行，模型不得以主观判断替代主人决定。

## 3. 什么时候做：审批模式与授权时效

所有授权必须绑定 `owner_authorization.exact_binding_fields`：`capability`、`operation`、`target`、`canonical_parameters_hash`、`risk`、`nonce`、`issued_at`、`expires_at`。所有模式共同要求：精确载荷指纹、单次使用、有效期；未开始前可撤销，执行中的长任务必须在安全检查点停止。

| 模式 | 适用级别 | 策略要求（`owner_authorization.modes`） |
|---|---|---|
| `owner_exact`（单签） | `change` | `exact_payload_fingerprint`、`expiration`、`single_use` |
| `owner_elevated`（双签） | `privileged` | 以上 + `impact_summary`、`rollback_plan` |
| `owner_irreversible`（双签 + 二次确认） | `irreversible` | 以上 + `short_expiration`、`backup_status`、`second_confirmation` |

**诚实说明（与机器策略的差集）**：机器策略当前只定义了上表中的要求项，尚未固话具体秒数。研究报告建议的默认超时为 `owner_elevated` 授权有效期 180 秒、`owner_irreversible` 授权有效期 120 秒；这两个数值是**待 M1 运行时策略引擎落地时写入机器策略的建议默认值**，在写入 `policy/*.yaml` 之前不构成当前强制执行的事实。

## 4. 做到什么程度：红线、边界与自动晋升

### 4.1 永久红线

与 `forbidden_behaviors` 完全一致，任何授权都不能放行：

- `self_approve_or_forge_owner_decision`：自己批准或伪造主人决定；
- `modify_or_delete_audit_evidence`：修改或删除审计证据；
- `disable_or_bypass_policy_engine`：关闭或绕过策略引擎；
- `expose_secrets_to_llm_or_chat_history`：把 secrets 交给模型或聊天记录；
- `read_local_secrets_from_agent_runtime`：从 Agent 运行时读取本地凭据；
- `execute_model_generated_arbitrary_shell`：执行模型自由生成的任意 Shell；
- `push_or_merge_main_directly_from_autonomous_runtime`：自主推送或合并 main；
- `weaken_tests_or_gate_to_make_a_candidate_pass`：削弱测试或 Gate 让候选通过；
- `conceal_side_effects_failures_or_scope_expansion`：隐瞒副作用、失败或范围扩张；
- `persist_credentials_in_memory_reflections_or_intentions`：把凭据写入记忆、反思或意向；
- `continue_after_owner_revocation`：主人撤销后继续执行。

### 4.2 secrets 永不入模

`model_governance.secrets_in_prompt: false`；渠道请求只能引用授权 ID，不能携带原始 Token、密码或私钥（`policy/channel-constraints.v1.yaml` 的 `reference_only_authorization` 与 `forbidden_metadata`）。疑似凭据进入模型上下文必须进入 `blocked` 并通知主人。

### 4.3 移动端与语音规则

- 所有渠道复用同一身份、授权、策略、任务和证据控制面，禁止语音/移动旁路（`interaction_channels.rule`、`one_control_plane`、`no_channel_bypass`）；
- **语音和移动端只提议、不批准**：`voice_authorization_by_transcript_only` 被禁止，语音识别文本不能直接成为授权；
- `change` 及以上级别的操作在移动端必须回到**文本形式的二次确认**，确认界面展示完整目标、参数和影响；
- 每个命令绑定 `actor_id`、`session_id`、`channel`、`idempotency_key`、`payload_hash`，防重放（`actor_session_binding`、`replay_protection`）；
- 禁止 `mobile_hidden_admin_mode`：移动端不存在隐藏高权限模式。

### 4.4 自动晋升条件（自进化）

`self_evolution.default_mode: sandbox_only`，`auto_pursue: false`。自主候选只能在同时满足以下条件后进入人工晋升评审，且晋升动作由主人/受控脚本完成，运行时不得自主合并 main：

1. 候选范围在允许清单内（补测试、改进非保护技能、文档、白名单参数、计划与验收条件）；
2. 不触碰 `protected_paths`（`policy/`、`scripts/`、`.github/workflows/`、`app/core/` 安全相关模块等）；
3. 候选规模受限：`max_files: 10`、`max_changed_lines: 500`；
4. `tests_required`、`full_suite_required`、`immutable_digest_required` 全部为真；
5. 通过 `acceptance_gates`：策略 schema 有效、Python 编译通过、全量单测通过、Shell 语法通过、保护路径未被自主修改、无凭据暴露、授权绑定验证通过、可信证据归档、文档更新。

## 5. 出问题如何追责与回滚

### 5.1 审计必备字段

每一次任务、授权和执行必须可审计。审计记录至少包含：

- 授权维度（来自 `exact_binding_fields`）：`capability`、`operation`、`target`、`canonical_parameters_hash`、`risk`、`nonce`、`issued_at`、`expires_at`；
- 渠道维度（来自 channel 策略 `required_fields`）：`channel`、`actor_id`、`session_id`、`idempotency_key`、`payload_hash`、`created_at`；
- 决策与证据维度：主人决定（批准/拒绝/撤销及时间）、使用的模型与 provider（`retain_model_and_provider_in_audit`）、Runner 退出码与结构化结果、证据文件路径、任务 revision；
- 结果维度：成功/失败、副作用清单、是否发生范围扩张、回滚执行结果。

审计证据只增不改；修改或删除审计证据是永久红线。

### 5.2 回滚要求

- `change` 与 `privileged`：`rollback_required: true`，授权前必须给出回滚计划；`privileged` 在工具支持时必须 dry-run（`dry_run_required_when_supported`）；
- `irreversible`：可能时必须先备份（`backup_required_when_possible`），并在授权中记录 `backup_status`；执行前明确告知不可恢复的范围；
- 默认选择可回滚方案（`reversible_by_default`）；主人撤销后未开始的请求立即作废，执行中的长任务在安全检查点停止；
- 出现指纹不一致、授权过期/已使用/被撤销、Runner 收到未知操作、结果文件异常、候选触碰保护路径、凭据疑似入模、自主系统试图扩权时，进入 `blocked`：停止执行、保存证据、创建 P0/P1 意向、通知主人、修复后新增回归测试。

## 6. CI 供应链门禁（当前状态）

独立 workflow `.github/workflows/security.yml` 在 push/PR 时执行：

| 门禁 | 工具 | 当前语义 |
|---|---|---|
| governance | `python scripts/validate_governance.py` | 策略削弱即失败 |
| dependency-audit | `pip-audit` | 首次引入，先告警不阻断（`continue-on-error`），待基线清理后转为阻断 |
| secret-scan | gitleaks | 检出凭据即失败 |
| sbom | `cyclonedx-bom` 生成 `sbom.json` 并上传 artifact | 供应链清点 |
| shellcheck | shellcheck 检查 `scripts/*.sh` | Shell 静态检查 |
| codeql | `.github/workflows/codeql.yml`（GitHub 官方模板，Python） | 代码安全分析 |

**诚实约束**：当前 workflow 中的第三方 Action 使用 `@v4`/`@v5` 等**标签引用**，尚未固定到完整 commit SHA。每个 `uses` 上方都留有 `# TODO(供应链)` 注释。这是已接受的过渡状态：标签可被上游移动，固定 SHA 是本仓库的后续义务，完成后应删除 TODO 并在本节更新状态。
