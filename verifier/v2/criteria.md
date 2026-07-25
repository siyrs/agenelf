# 验收标准 v2 — 研究报告 M1 治理基线落地 + 五自回归

## A. 复审（Codex v3 更新）
- A1 基线全量测试通过（236 项）
- A2 差距清单确认：policy 引擎/双签/三文档/CI 供应链

## B. 报告 M1 交付物
- B1 app/core/policy.py：加载 policy/*.yaml，evaluate(capability,operation,subject)
  返回 allowed/risk/approval(none|single|dual)/ttl/requires_textual_confirmation/policy_version；
  默认 deny；策略缺失优雅降级
- B2 双签审批：privileged 操作需 2 名不同批准人、更短 TTL，一次性核销
- B3 移动端规则：mobile_device 的 change/privileged 必须文本二次确认且不能自批准
- B4 文档：docs/SAFETY_POLICY.md、docs/ROADMAP.md、local/self/intentions.template.json
- B5 CI：security.yml 供应链门禁（policy lint/依赖审计/密钥扫描/SBOM/ShellCheck）
  + 契约测试锁定这些门禁存在

## C. 五自回归（政策落地后必须仍然工作）
- C1 全量 unittest 通过（基线 236 + 新增）
- C2 实弹演练：认知/沉淀/完善/迭代(mock补丁→gate→promote→completed)/优化 全链路成功
- C3 触发后功能：晋升技能可调用；新策略评估被真实执行（证据）

## D. 安全回归
- D1 高危单签授权流程不受影响（既有测试全过）
- D2 双签：单人两票/两票同人/过期 均拒绝执行
