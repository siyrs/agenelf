# Agenelf 隔离代码修复能力

## 目标

`code.repair` 将“模型直接写文件和执行代码”替换为可审计的分离执行链：模型只提交标准 Git 补丁，独立 `repair-runner` 在只读源码的临时副本中应用补丁并运行主人预配置的测试。

```text
Agent / Workflow
  │ repository alias + unified diff
  ▼
data/repair-requests/                 Agent 可写
  │ fingerprint-bound request
  ▼
repair-runner                          无 LLM、无网络、无 secrets
  │ clone local read-only repository
  │ validate patch / protected paths
  │ git apply --check + git apply
  │ owner-configured argv tests
  ▼
data/repair-results/                  Agent 只读
  └─ repair-space/<repair-id>/        补丁与一次性工作副本
```

## 主人配置

运行 `make init` 后编辑：

```text
local/repositories.yaml
```

源码仓库由主人放在：

```text
code-workspaces/<source_dir>/
```

示例：

```yaml
schema_version: 1
repositories:
  pmp:
    description: PMP
    language: java
    source_dir: pmp
    default_test_profile: maven
    allowed_test_profiles: [maven]
    protected_paths: [.github/workflows/, policy/, local/, secrets/]

test_profiles:
  maven:
    commands:
      - [mvn, -B, test]
    timeout_seconds: 1200
```

测试命令必须是 argv 数组，不能使用 `shell -c` 或 `python -c`。Runner 只支持代码内置的受限执行器名称；命令来自主人配置，模型不能覆盖。

## 使用

聊天中：

```text
列出可修复代码仓库。
把这个 unified diff 在 pmp 的隔离副本中应用，并运行 maven 测试。
查询 repair-xxxxxxxxxxxxxxxx 的结果。
```

工具：

```text
list_code_repair_repositories
submit_code_repair_patch
get_code_repair_result
```

宿主机查看：

```bash
make repair
```

## 可信结果

成功结果包含：

- 源仓库基线 commit；
- 补丁 SHA-256；
- 变更文件列表；
- 测试 profile；
- 每条命令的 argv、退出码、耗时和脱敏输出尾部；
- `source_repository_modified: false`；
- `committed/pushed/merged: false`。

`repair-runner` 不会修改源仓库，也不会自动创建 commit、push 或 PR。通过测试只说明候选在隔离副本通过；发布仍需后续人工或受治理的 PR/Release 流程。

## 旧代码快车道收敛

- `code_writer.run_python` 已永久禁用；`write_code_file` 只写 `workspace/scratch` 文本草稿。
- `app-space` 自动加载和 `skill_forge` 默认关闭。
- 实验性锻造必须由主人同时设置两个开关、附带测试，并通过静态规则；核心与外部副作用能力仍必须走正式仓库变更。

## 当前限制

- v1 不支持二进制补丁和重命名补丁；
- 运行镜像必须已安装目标项目测试依赖；
- v1 不自动创建分支、commit、push 或 PR；
- 更强的恶意代码隔离应继续演进为一次性容器/虚拟机执行池。
