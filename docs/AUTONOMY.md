# Agenelf 受控自主反思与自我迭代

## 它所说的“自我”是什么

Agenelf 的自我模型是软件层面的可观测状态：当前模型、技能、能力域、加载错误、任务队列、安全不变量、迭代会话和晋升证据。它不表示主观意识、情感或独立人格，也不会使用“觉醒”作为技术结论。

## 自主循环

```text
观察自身状态
  -> 识别 P0/P1/P2 缺口
  -> 自动选择或接受一个改进目标
  -> 在 app-tmp 创建迭代会话
  -> LLM 生成最多 4 个 Python 整文件补丁
  -> 强制至少包含 tests/test_*.py
  -> 完整单元测试
  -> 宿主机 gate_check
  -> 生成绑定候选代码摘要的 READY
  -> 等待人工 make promote REQ=<id>
```

默认情况下，watcher 只提示 READY，不自动晋升。只有在 `.env` 显式设置 `AGENELF_AUTO_PROMOTE_EVOLUTION=1` 时，宿主机 watcher 才会自动调用 `promote.sh`。

## 安全关键文件

自主补丁不得修改：

- `core/autonomy.py`
- `core/operations.py`
- `core/permissions.py`
- `skills/evolution_ops.py`
- `skills/server_ops.py`
- 根目录 `scripts/`、`.env`、`docker-compose.yml`

自主引擎先拒绝这些路径，可信的 `gate_check.sh` 还会把 `app-tmp` 与 `app-fork` 再次逐文件比较。

## 防止 READY 后篡改

`gate_check.sh` 使用只读的 `scripts/tree_digest.py` 计算候选树 SHA-256，并写入：

```text
data/promote-requests/<id>/candidate.sha256
```

`promote.sh` 在同步到正式 `app/` 前重新计算摘要。只要 READY 之后任何候选文件发生变化，就会：

1. 拒绝晋升；
2. 删除旧 READY；
3. 写入 REJECTED；
4. 要求重新运行完整 gate。

成功晋升后，报告、摘要和时间会保存在 `data/promotion-history/<id>/`，不会随请求目录清理而丢失。

## 使用方式

CLI：

```text
/self
/reflect
/autonomy --plan-only
/autonomy 优化对话工具选择并补充回归测试
/evolve 修复某个明确问题并补充测试
```

`/evolve` 现在是安全自主循环的兼容别名，不再创建 Git 分支或直接合并主分支。

HTTP API：

```text
GET  /self
GET  /self/assessment
POST /autonomy/cycles
GET  /autonomy/cycles
GET  /autonomy/cycles/{cycle_id}
```

创建计划：

```json
{"goal":"", "apply_changes":false}
```

执行沙盒补丁：

```json
{"goal":"优化工具选择并补充测试", "apply_changes":true}
```

## 能力组合

`agent.self_reflection` 可以作为未来组合工作流的入口：

```text
agent.self_reflection
  -> code.repair
  -> software.validation
  -> server.operations
  -> software.release
```

每个能力域仍保持独立执行器、风险分类、审批和证据，不会退化为任意 Shell。
