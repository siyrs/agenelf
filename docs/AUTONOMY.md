# Agenelf 受控自主反思与自我迭代

## 两个相互配合的层次

Agenelf 把持续成长拆成两个独立层次：

1. `agent.self_development`：把观察、教训和改进意向持久化到 `local/self/`；
2. `agent.self_reflection`：读取当前运行状态，并把选定目标送入受控自主代码迭代。

详细的反思沉淀和意向生命周期见 [SELF_DEVELOPMENT.md](SELF_DEVELOPMENT.md)。

## 它所说的“自我”是什么

Agenelf 的自我模型是软件层面的可观测状态：

- 当前模型、技能和能力域；
- 加载或运行时错误；
- 运维、迭代和晋升队列；
- 安全不变量；
- 持久化连续性 ID；
- 最近反思、教训和开放改进意向；
- 测试、Gate 与宿主机晋升证据。

它不表示主观意识、情感、独立人格或自由意志，也不会使用“觉醒”作为技术结论。

## 自主循环

```text
读取可观测状态与开放意向
  -> 识别 P0/P1/P2/P3 缺口
  -> 自动选择、接受目标或推进指定意向
  -> 在 app-tmp 创建迭代会话
  -> LLM 生成最多 4 个 Python 整文件补丁
  -> 强制至少包含 tests/test_*.py
  -> 完整单元测试
  -> 宿主机 gate_check
  -> 生成绑定候选代码摘要的 READY
  -> 等待人工 make promote REQ=<id>
  -> 保留 promotion-history
  -> 对应意向才可标记 completed
```

默认情况下：

- 自动反思只会沉淀和提出意向；
- 意向不会自动进入代码修改；
- watcher 只提示 READY，不自动晋升；
- 只有 `.env` 显式设置 `AGENELF_AUTO_PROMOTE_EVOLUTION=1` 时，宿主机 watcher 才会自动调用 `promote.sh`。

## 自主循环和改进意向的关系

普通自主循环可以直接以文本目标启动：

```text
/autonomy --plan-only 改进错误诊断
/autonomy 改进错误诊断并补充测试
```

持久化意向则提供跨会话生命周期：

```text
/intend P1 改进错误诊断
/intentions
/pursue intent-...          # 只计划
/pursue intent-... --apply  # 进入沙盒
```

推进结果会写回意向：

| 自主循环结果 | 意向状态 |
|---|---|
| `plan_ready` | `planned` |
| 开始修改 | `active` |
| `promotion_requested` | `awaiting_promotion` |
| `failed/blocked` | `blocked` |
| 检测到 `promotion-history/<evo-id>` | `completed` |

模型不能仅凭回复文本把意向标记为完成。

## 安全关键文件

自主补丁不得修改权限、隐私、个性化、运维执行、成长连续性和晋升控制相关模块。宿主机 Gate 至少保护：

```text
core/autonomy.py
core/operations.py
core/permissions.py
core/configuration.py
core/local_context.py
core/privacy.py
core/memory.py
core/self_development.py
skills/evolution_ops.py
skills/server_ops.py
skills/local_context.py
skills/self_development.py
```

自主引擎负责路径、数量和 Python 语法的第一层检查；可信 `gate_check.sh` 再把 `app-tmp` 与 `app-fork` 逐文件比较。根目录 `scripts/`、`.env`、`docker-compose.yml` 和主人持久化数据也受保护。

## 防止 READY 后篡改

`gate_check.sh` 使用只读的 `scripts/tree_digest.py` 计算候选树 SHA-256，并写入：

```text
data/promote-requests/<id>/candidate.sha256
```

`promote.sh` 在同步到正式 `app/` 前重新计算摘要。只要 READY 之后任何候选文件发生变化，就会：

1. 拒绝晋升；
2. 删除旧 READY；
3. 写入 REJECTED；
4. 要求重新运行完整 Gate。

成功晋升后，报告、摘要和时间保存在：

```text
data/promotion-history/<id>/
```

成长引擎以该目录作为意向完成的可信依据。

## 使用方式

CLI：

```text
/self
/assess
/mind
/reflect
/reflect --deep
/intentions
/intend P1 <目标>
/pursue <intent-id>
/pursue <intent-id> --apply
/autonomy --plan-only
/autonomy <目标>
/evolve <目标>
```

`/evolve` 是安全自主循环的兼容别名，不会创建 Git 分支或直接合并主分支。

HTTP API：

```text
GET  /self
GET  /self/assessment
GET  /self/development
POST /self/reflections
GET  /self/intentions
POST /self/intentions
POST /self/intentions/{id}/pursue
POST /autonomy/cycles
GET  /autonomy/cycles
GET  /autonomy/cycles/{cycle_id}
```

## 能力组合

```text
agent.self_development
  -> agent.self_reflection
  -> code.repair
  -> software.validation
  -> server.operations
  -> software.release
```

每个能力域仍保持独立执行器、风险分类、审批和证据，不会退化为任意 Shell。

## 可信能力健康反馈

自主评估现在读取确定性 Runner 产生的运维与软件验证结果。连续失败至少两次，或三次以上观测成功率低于 60%，会形成 `capability_degraded:*` 发现，并由现有反思系统创建去重改进意向。

能力健康只影响计划和意向优先级；它不能自动推进补丁、批准操作或绕过晋升。

## 无人值守成长守护

`scripts/growth_daemon.sh` 是宿主机侧的确定性守护进程（不调用 LLM），按固定间隔自动触发成长链路的"快车道"环节：

```text
每轮（默认 3600s，--once 跑一轮退出）：
  1. 确定性反思：从 capability_health 可信证据沉淀一条反思
     （local/self/reflections.json，trigger=growth_daemon）
  2. optimize_auto：证据驱动参数微调 + 负反馈自动回滚检查
  3. 记录 capability_health 摘要
全部动作以统一 JSON 行留痕 logs/growth.log（时间戳/动作/结果摘要）
```

触发方式：

- docker 与 compose 服务可用：`docker compose exec -T agenelf` 在容器内执行（CLI 支持 `--reflect-once` 时反思走 CLI，否则 python 直调 core 模块）；
- docker 不可用：优雅降级为本地直调（`AGENELF_MOCK=1 AGENELF_ROOT=<根> python3`），仅依赖标准库；
- 部署：`cron`（`*/30 * * * * /path/scripts/growth_daemon.sh --once`）或 systemd user timer，示例写在脚本头部注释。

与人工闸门的关系：**守护进程只有"触发权"**。它只触发反思沉淀与白名单内运行期参数微调（快车道，可自动回滚）；代码晋升（`make promote REQ=<id>`）、改进意向的批准与推进、运维操作审批仍是人类闸门，守护进程绝不自动晋升、不修改代码、不触碰 `config.yaml` 与主人私有数据。
