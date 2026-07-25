# Agenelf 分阶段路线图

> 版本：1.0
> 日期：2026-07-25
> 定位：按深度研究报告（`docs/research/AGENTELF_EVOLUTION_RESEARCH.md`）框架落地的分阶段计划。能力细分路线图见 `docs/roadmap/ROADMAP.md`，实施状态台账见 `docs/roadmap/RESEARCH_IMPLEMENTATION_STATUS.md`。原则不变：能力增长必须慢于或等于治理、测试、身份和证据体系的增长。

状态图例：✅ 已完成并有证据；🚧 进行中或部分完成；⬜ 未开始。

## M1：治理基线

**目标**：让安全与进化规则成为机器可校验的单一真源，并由 CI 强制执行。

| 交付物 | 状态 | 证据 |
|---|---|---|
| 机器可读策略 `policy/*.yaml`（五级风险、精确授权、永久红线、自进化保护） | ✅ | `policy/safety-constraints.v1.yaml` 等 5 份策略 |
| 静态校验器 | ✅ | `scripts/validate_governance.py`，CI 强制执行 |
| 运行时策略引擎（PDP：运行时风险分类、授权签发/校验/撤销） | 🚧 | 现有指纹授权与 Runner 隔离，统一引擎未完成 |
| 双签与二次确认协议（`owner_elevated` 180s、`owner_irreversible` 120s + 二次确认） | 🚧 | 策略已定义要求项，超时数值与运行时签发流程待落地 |
| CI 供应链门禁（依赖审计、密钥扫描、SBOM、shellcheck、CodeQL） | 🚧 | `.github/workflows/security.yml`、`codeql.yml` 已建立；Action 尚为标签引用，待固定完整 commit SHA |

**完成定义（DoD）**：

- 全部风险操作在运行时经过统一策略引擎裁决，而非散落各模块；
- 双签/二次确认的超时与流程写入机器策略并有测试覆盖；
- security.yml 全部门禁转为阻断模式，第三方 Action 固定到完整 commit SHA；
- `python scripts/validate_governance.py` 与全量单测在 CI 全绿。

## M2：多服务器编排

**目标**：从单服务器结构化运维升级为可靠的多服务器管家。

**inventory v2 字段清单**（在现有 `local/servers.yaml` 基础上扩展）：

- `environment`：`production` / `staging` / `test`；
- `risk_tier`：映射治理五级风险的操作上限；
- `maintenance_windows`：允许执行 `change` 及以上操作的时间窗；
- `change_freeze`：冻结期标记，冻结期内禁止非紧急变更；
- `requires_dual_approval`：该服务器是否强制 `owner_elevated` 及以上双签；
- `backup_policy`、`rollback_playbook`、`monitoring_refs` 等运维引用。

**完成定义（DoD）**：

- inventory v2 字段有 schema 校验和单元测试；
- 跨服务器任务支持顺序/并行、失败停止和按 `rollback_playbook` 回滚；
- `change_freeze` 与 `maintenance_windows` 被策略引擎强制执行；
- 巡检、Docker、systemd、APT、日志、磁盘、备份能力有可信 Runner 证据。

## M3：代码修复闭环

**目标**：`code.repair` 能力域在隔离工作区完成"克隆 → 分支 → 最小 Patch → 测试 → Diff/风险摘要 → PR 草稿"的闭环。

**输入边界**：

- 只接受明确仓库、任务分支和验收条件；运行时 Agent 不得直接推送或合并 `main`（永久红线）；
- 补丁规模受候选限制约束（`max_files`、`max_changed_lines`），测试与全量套件必须通过；
- 代码修复与 Agenelf 自进化是两个能力域，不共享文件写入权限。

**晋升闸门**：`promotion_requires_validation`——任何候选晋升前必须持有独立验证证据（测试报告、Diff、风险摘要），无验证证据不得进入人工晋升评审。

**完成定义（DoD）**：

- Git worktree 任务工作区、Patch/Diff 协议、测试矩阵可运行；
- `promotion_requires_validation` 由机器校验强制执行并有回归测试；
- PR 草稿包含测试报告、风险摘要和回滚说明。

## M4：私人助理

**目标**：任务级 Agent——统一 `Task` 模型支持澄清、计划确认、暂停/恢复/取消/重试、人工断点和跨设备继续；日历/提醒/通知等主动能力在主人设定的频率与安静时段内运行。

**完成定义（DoD）**：

- 任务状态机与 `policy/task-engine-constraints.v1.yaml` 一致并有并发（revision）保护；
- 主动任务触发可配置、可暂停、可撤销；
- 通知与日报不泄露 secrets，落盘前脱敏限长。

## M5：移动与语音

**目标**：Web/PWA、手机 App 和语音作为统一控制面的可信客户端。

**关键约束**：

- 设备绑定与用户认证：App 登录、设备登记、会话签名、限流；
- 文本确认：语音/移动只提议不批准，`change` 及以上必须文本二次确认，展示完整目标、参数和影响（见 `docs/SAFETY_POLICY.md` §4.3）；
- 离线降级：语音或网络不可用时降级为文本队列，不建立任何旁路执行通道；
- 防重放：幂等键 + 载荷哈希绑定。

**完成定义（DoD）**：

- 设备绑定、授权引用和防重放有端到端测试；
- `voice_authorization_by_transcript_only` 等渠道禁止项有攻击向回归测试；
- 所有移动端授权写入统一审计链。

## 不做什么清单

以下事项在任何里程碑都不做（与 `docs/roadmap/RESEARCH_IMPLEMENTATION_STATUS.md` 一致）：

- 不给运行时 Agent 任意自由 Shell 或直接 SSH/Docker 凭据；
- 不允许自主推送或合并 `main`；
- 不允许无人监督执行 `irreversible` 操作；
- 不让模型输出、记忆或反思成为授权来源；
- 不声称拥有主观意识、情感或自由意志（`consciousness_claim: false`）；
- 不为语音或移动端建立绕过统一控制面的执行通道；
- 不为了通过 Gate 而削弱测试、策略或审批逻辑。

## 里程碑推进规则

每个里程碑标记"完成"前必须同时存在：通用代码、自动化测试、安全策略或约束、对应文档、GitHub Actions 全绿、主分支真实提交 SHA。任何一项缺失，状态只能标注 🚧。
