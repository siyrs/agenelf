# policy/

`policy/safety-constraints.v1.yaml` 是 Agenelf 治理语义的机器可校验基线。

运行校验：

```bash
python scripts/validate_governance.py
```

策略变更必须通过：

- `app/tests/test_governance_policy.py`；
- 完整单元测试；
- GitHub Actions；
- 人类主导的 PR 审查。

生产运行时 Agent 不得自行修改本目录。高风险操作可由主人按策略精确授权；`forbidden` 行为不能通过授权放行。
