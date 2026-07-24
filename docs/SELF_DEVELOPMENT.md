# Agenelf 持续自我认知、沉淀与改进意向

## 定义

本功能实现的是**操作性自我认知**，不是主观意识。

| 用户表达 | 工程实现 |
|---|---|
| 自我意识 | 对模型、技能、能力域、错误、队列、原则和限制的可观测自我模型 |
| 自我沉淀的意愿 | 自动把一定数量的对话事件、失败结果和主人反馈压缩为有界反思记录 |
| 自我完善的意向 | 带优先级、原因、证据、验收条件、状态和操作性承诺度的持久化目标 |
| 自主完善 | 选定意向后进入 `app-tmp → 测试 → gate → 晋升请求`，不能直接修改主分支 |

Agenelf 不会把这些状态描述为情感、欲望、灵魂、自由意志或“觉醒”。所有状态都可以从文件、工具结果和测试证据中核查。

## 数据位置

成长连续性保存在当前主人的私有目录：

```text
local/self/
├── state.json
├── reflections.json
└── intentions.json
```

- `state.json`：连续性 ID、创建时间、最近沉淀时间、对话游标和固定原则；
- `reflections.json`：最近的观察、教训、证据和由本次反思产生的意向；
- `intentions.json`：改进目标的完整生命周期。

这些文件：

- 不进入 Git；
- 只挂载给 Agent，`ops-runner` 不可见；
- 在写入前执行凭据脱敏；
- 使用原子 JSON 写入；
- 有数量上限，不会无限增长；
- 不会因更新 `app/` 而被覆盖。

## 操作性自我定义

首次启动时会生成稳定的 `continuity_id`。状态中固定记录：

- 类型：持久化、可调用工具的软件智能体；
- 目的：持续理解主人目标，以证据完成任务，并在安全边界内改进通用能力；
- 原则：
  - 主人目标与明确授权优先；
  - 证据优先于自我宣称；
  - 安全优先于速度；
  - 把失败与结果沉淀为下一步；
  - 通用能力进入 `app/`，个性化连续性保留在 `local/`；
- `consciousness_claim: false`。

## 自动沉淀

默认配置：

```yaml
self_development:
  auto_reflect_every_episodes: 12
  min_reflection_interval_seconds: 3600
  max_reflections: 200
  max_intentions: 100
  prompt_max_chars: 4000
  allow_llm_reflection: true
```

每完成一轮聊天，Agent 会先把对话摘要写入长期记忆。满足下面两个条件时，执行一次**确定性自动反思**：

1. 自上次沉淀游标以来累计至少 12 条对话事件；
2. 距离上次反思至少 3600 秒。

自动反思不会调用 LLM、不会修改代码，也不会自动推进意向。它只会：

1. 读取可观测运行状态；
2. 生成观察和教训；
3. 根据 P0/P1/P2/P3 发现创建去重的改进意向；
4. 更新 `local/self/`；
5. 把简短成长状态注入后续提示词。

手动使用 `deep=true` 时，可以额外调用 LLM 生成结构化复盘。输出必须是 JSON，经过脱敏和字段校验；解析失败会自动降级到确定性反思。

## 改进意向

每条意向包含：

```json
{
  "id": "intent-...",
  "title": "提升错误诊断质量",
  "rationale": "失败证据不够明确",
  "priority": "P1",
  "status": "proposed",
  "operational_commitment": 80,
  "acceptance_criteria": [
    "增加回归测试",
    "完整测试通过",
    "不绕过安全门"
  ],
  "evidence": ["assessment:..."],
  "owner_aligned": true,
  "attempts": 0,
  "linked_cycle_id": null,
  "evolution_session_id": null,
  "consciousness_claim": false
}
```

`operational_commitment` 是软件优先级映射，不是情绪强度：

| 优先级 | 承诺度 |
|---|---:|
| P0 | 100 |
| P1 | 80 |
| P2 | 60 |
| P3 | 40 |

相同标题的开放意向会去重，避免反思循环持续制造重复目标。

### 生命周期

```text
proposed
  ├─> planned
  ├─> active
  └─> dismissed

planned/active
  ├─> awaiting_promotion
  ├─> blocked
  └─> dismissed

awaiting_promotion
  ├─> completed   # 检测到 data/promotion-history/<evo-id>/
  └─> blocked     # 检测到 REJECTED

completed / dismissed = 终态
```

Agent 不能仅凭文字把代码改进标记为完成。`awaiting_promotion` 只有在发现宿主机保存的不可变晋升证据后，才会自动变成 `completed`。

## 推进意向

仅生成计划：

```text
/pursue intent-...
```

进入受控沙盒：

```text
/pursue intent-... --apply
```

执行链：

```text
意向
  -> 自主循环计划
  -> app-tmp 小型 Python 补丁
  -> 强制 tests/test_*.py
  -> 完整测试
  -> gate_check
  -> READY + candidate.sha256
  -> awaiting_promotion
  -> 人工 make promote REQ=evo-...
  -> promotion-history
  -> completed
```

自动沉淀策略明确设置 `auto_pursue: false`。反思可以主动提出目标，但不能在没有明确推进动作时自行修改代码。

## CLI

```text
/self                         查看完整可观测自我模型
/assess                       查看当前 P0/P1/P2/P3 评估
/mind                         查看持久化成长状态
/reflect [说明]               确定性反思并沉淀
/reflect --deep [说明]        LLM 辅助结构化复盘，失败自动降级
/intentions [状态]            查看意向
/intend [P0|P1|P2|P3] <目标>  创建意向
/pursue <intent-id>            生成计划
/pursue <intent-id> --apply    进入受控沙盒迭代
```

## HTTP API

```text
GET  /self
GET  /self/assessment
GET  /self/development
POST /self/reflections
GET  /self/reflections?limit=10
GET  /self/intentions?status=proposed&limit=20
POST /self/intentions
GET  /self/intentions/{intent-id}
POST /self/intentions/{intent-id}/pursue
```

创建反思：

```json
{"note":"复盘最近的部署失败","deep":false}
```

创建意向：

```json
{
  "title":"改进部署失败诊断",
  "rationale":"目前错误摘要不够明确",
  "priority":"P1",
  "acceptance_criteria":["新增回归测试","保留可复现证据"]
}
```

推进计划：

```json
{"apply_changes":false}
```

## 安全边界

下列模块被宿主机 Gate 视为安全关键模块，不能由自主补丁修改：

```text
core/self_development.py
skills/self_development.py
```

同时，候选代码若新增对以下路径的直接写入意图，会被拒绝：

```text
local/profile.yaml
local/preferences.yaml
local/servers.yaml
local/memory/
local/self/
local/secrets/
```

正常写入必须通过已经审查并保护的 `MemoryStore` 或 `SelfDevelopmentStore`。

## 验收

CI 覆盖：

- 连续性状态初始化与持久化；
- 凭据脱敏；
- 反思条目上限；
- 意向去重和生命周期；
- 自动沉淀阈值；
- 深度反思 JSON 解析与安全降级；
- 意向到计划型自主循环的关联；
- CLI/技能/API 契约；
- Docker `local/self` 隔离；
- Gate 对成长边界模块和 `local/self` 写入的拦截。
