# Agenelf 治理文档索引

本目录是 Agenelf 权限、安全和自主进化的设计入口。

- [安全治理政策](SAFETY_POLICY.md)
- [权限与授权模型](PERMISSION_MODEL.md)
- [受控自我进化规则](SELF_EVOLUTION_RULES.md)
- [架构决策 ADR-0001](../ADR/ADR-0001-agent-governance.md)
- [深度研究报告](../research/AGENTELF_EVOLUTION_RESEARCH.md)
- [长期演进路线图](../roadmap/ROADMAP.md)
- [验收测试计划](../testing/AGENT_ACCEPTANCE_TEST_PLAN.md)
- [机器可校验策略](../../policy/safety-constraints.v1.yaml)

## 修改规则

任何治理变更必须同时：

1. 修改机器策略；
2. 修改人类可读文档；
3. 增加或更新回归测试；
4. 运行 `python scripts/validate_governance.py`；
5. 运行完整单元测试；
6. 通过 PR 和 CI；
7. 由主人批准后合并主分支。
