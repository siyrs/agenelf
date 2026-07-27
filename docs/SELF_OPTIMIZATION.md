# 证据驱动自我优化

"五自"闭环中的**自我优化**：对运行期可调参数做证据驱动微调。不改代码、不走晋升管道，
是安全的"快车道"；与自我迭代（慢车道，app-tmp→gate→promote）互补。

## 可调参数白名单（有界护栏）

| 键 | 默认 | 范围 | 生效点 |
|---|---|---|---|
| `agent.memory_prompt_limit` | 50 | 10–100 | 每轮系统提示刷新时读取 |
| `agent.memory_prompt_max_chars` | 8000 | 2000–20000 | 同上 |
| `llm.temperature` | 0.6 | 0.0–1.0 | 每轮 chat 调用前设置 |

白名单外的键、越界值一律拒绝；绝不修改 `config.yaml`。

## 存储与审计

- `local/self/optimizations.json`：`{active, history, cooldowns}`，原子写入、脱敏、有界（100 条）
- 每次 apply/rollback 写 `logs/audit.log`；同键 1 小时冷却防抖动
- `rollback(key)` 回退到上一历史值（无历史恢复默认）

## 自动优化 `auto_tune`

不调用 LLM。读取 capability_health 的可信证据：
- ≥2 条记忆/截断相关失败 → `memory_prompt_max_chars` 收缩 20%（下限 2000）
- 连续健康且当前值低于默认 → 回调一档
- 无相关证据 → 明确"保持现状"（证据优先于动作）

## 负反馈自动回滚

优化不能只看"调了参数"，还要看"调完有没有变好"。闭环如下：

- `apply()` 时顺手记录当前 capability_health 摘要快照（总观测数/成功率/连续失败数），
  存入 `active[key]["health_at_apply"]` 并随状态持久化；模块不可用或读取失败时容错记 `None`；
- `auto_tune()` 的第一步固定为**负反馈检查**：对持有基线的 active 键，对比当前健康快照，
  任一满足即自动回滚该键（走现有 `rollback` 审计链）：
  - 成功率较应用时下降 ≥20 个百分点；
  - 连续失败数较应用时增加 ≥2；
- 每次自动回滚追加 `optimization_auto_rollback` 审计记录并注明"负反馈自动回滚"，
  同时在返回结果的 `auto_rollbacks` 字段说明（键、判定理由、前后健康摘要）；
- 健康不变或改善时不回滚；没有基线的旧 active 项不回滚；
- 回滚后基线失效，后续由新的 apply 重新建立，避免沿历史链反复回退。

`auto_rollbacks` 与 `note` 都会被成长守护进程（`scripts/growth_daemon.sh`）以 JSON 行
留痕到 `logs/growth.log`，可随时审计。

## 接口

```text
技能：optimize_status / optimize_apply / optimize_rollback / optimize_auto
HTTP：GET /self/optimization
      POST /self/optimization/apply|rollback|auto
```

与 `self_development` 一致：一切状态可核查，`consciousness_claim: false`。
