# Code Repair 与动态代码收敛验收矩阵

## 自动化

```bash
python scripts/validate_governance.py
python -m compileall -q app scripts
cd app && python -m unittest discover -s tests -v
python scripts/init_local.py --no-migrate
docker compose config
```

必须覆盖：

- 补丁请求的 SHA-256 和 canonical payload 指纹；
- 公共查询视图不返回完整补丁或源码路径；
- 疑似凭据、二进制、重命名、逃逸路径和保护路径拒绝；
- 只读源仓库复制、`git apply --check`、应用和主人配置测试；
- 篡改请求在执行前阻断；
- 测试失败有真实退出码和脱敏证据；
- 源仓库不变，Runner 不 commit/push/merge；
- repair-runner 无网络、无 secrets、无 Agent memory/self；
- Agent 不挂载 code-workspaces；
- `code_writer.run_python` 永久拒绝；
- app-space 与 skill_forge 默认关闭；
- 锻造启用时测试必填且危险 AST 拒绝。
