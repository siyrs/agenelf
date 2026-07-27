# 安全代码修复批次终验记录

本文件用于触发并记录运输载荷清理后的最终独立 CI。

## 被验收的正式源码

- `app/core/code_repair.py`
- `app/skills/code_repair.py`
- `scripts/repair_runner.py`
- 动态代码快车道默认关闭与旧 `run_python` 禁用
- `local/repositories.yaml` 初始化与 Docker 选择性挂载

## 必须通过

1. 治理策略解析与安全约束一致性；
2. Python 全量编译；
3. 仓库完整 unittest 套件；
4. 真实临时 Git 仓库补丁应用与测试；
5. 请求指纹篡改、保护路径、凭据补丁和路径逃逸拒绝；
6. 干净 local 初始化与 Docker Compose 拓扑；
7. 全部 Shell 控制面脚本语法；
8. PR 不含运输载荷或临时应用工作流。

只有该最终 Head 的 GitHub Actions CI 全绿后才允许合并 `main`。
