# Agenelf 成长报告

把分散的成长证据聚合成一份人类可读的 Markdown 报告，让主人一眼看清"它最近成长了什么"。
生成器是**确定性脚本**（`scripts/growth_report.py`，仅标准库）：不调用 LLM、不修改任何
数据源，只做只读聚合；所有数据源缺失时优雅标注"无数据"，绝不崩溃。

与 `self_development` 一致：报告只引用可核查的文件证据，`consciousness_claim: false`。

## 用法

```bash
python3 scripts/growth_report.py [--days 7] [--out docs/growth-reports/] [--root 仓库根]
```

- `--days`：统计周期天数（默认 7）；
- `--out`：输出目录（相对 `--root` 解析，默认 `docs/growth-reports/`）；
- `--root`：仓库根（默认取脚本上级目录）。

输出 `docs/growth-reports/<UTC日期>.md`，同日重复生成会覆盖；stdout 打印报告路径与一行
摘要（供守护进程/cron 留痕）。`docs/growth-reports/` 已加入 `.gitignore`（保留 `.gitkeep`），
报告属于数据产物，不进入 Git。

## 报告语义与数据血缘

| 小节 | 内容 | 数据来源 |
|---|---|---|
| 自我连续性 | 连续性 ID、创建/最近沉淀时间、固定原则 | `local/self/state.json` |
| 反思沉淀 | 历史总数、期内新增数、最新反思及其教训 top5 | `local/self/reflections.json` |
| 改进意向 | 按状态统计表；期内到达 completed/blocked 的清单（按 `updated_at` 判定） | `local/self/intentions.json` |
| 晋升历史 | 期内晋升：ID、时间（`promoted_at` → evo-ID 内嵌时间戳 → 目录 mtime）、候选摘要前 12 位 | `data/promotion-history/*/`（含 `candidate.sha256`） |
| 参数优化 | 当前 active 项；期内 apply/rollback 动作，理由含"负反馈"的标注为自动回滚 | `local/self/optimizations.json` |
| 能力健康 | 可信证据总数、各能力 scorecard（健康度/成功率/连续失败） | `app/core/capability_health.py`（可 import 才取，否则标注不可用） |
| 运行日志事件 | 期内计数：授权（approve/deny/auth_*）、技能锻造、参数优化、守护轮次 | `logs/audit.log`、`logs/growth.log` |
| 下一步建议 | P0/P1 开放（非终态）意向 top3；无则写"保持当前节奏" | 由意向数据推导 |

时间口径：周期为 `now - days` 到 `now`（UTC 比较）；日志中的朴素时间戳按本地时区解释。

## 与守护进程/cron 的配合

`scripts/growth_daemon.sh` 新增可选报告步骤：

- 传入 `--with-report`：本轮额外生成一次成长报告；
- 即使不传，每第 24 轮也自动生成一次（轮次计数保存在 `data/.growth-daemon-rounds`）；
- 结果以 `action="growth_report"` 的 JSON 行写入 `logs/growth.log`；报告失败不中断守护轮次。

```bash
# 手动触发一轮并立即出报告
scripts/growth_daemon.sh --once --with-report
```

周报 cron 示例（每周一 09:00 生成近 7 天报告）：

```cron
0 9 * * 1 cd /path/to/agenelf && python3 scripts/growth_report.py --days 7
```

或直接复用守护进程（假设守护进程每 30 分钟一轮，24 轮 ≈ 12 小时自动出一次报告）：

```cron
*/30 * * * * /path/to/scripts/growth_daemon.sh --once
```

## 样例片段

```markdown
# Agenelf 成长报告（2026-07-25）

## 反思沉淀

- 历史反思总数：5
- 本周期内新增：5
- 最新反思：`reflection-20260725-020731-40ad12`（2026-07-25 02:07 UTC，触发：growth_daemon）
- 最新教训（top5）：
  - 分析并修复能力 agent.self_development 的最近失败：…

## 晋升历史

| 晋升 ID | 时间 | 候选摘要（前 12 位） |
|---|---|---|
| evo-20260725-013543-2f85c1 | 2026-07-25 01:35 UTC | `b953d840f9f1` |

## 下一步建议

- **P1** [blocked] 补充成长脉动演示能力（`intent-20260725-012941-9bbe3b`）
```
