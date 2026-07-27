# Agenelf 结构化任务板

## 定义

任务板把 `workspace/tasks` 从"待办/笔记落盘"升级为结构化任务流：

```text
主人指派任务 → agent 分解步骤 → 逐步推进 → 证据关联 → 完成归档
```

任务记录是 agent 私有工作区状态：创建、推进、阻塞、关联意向只改变任务板自身，
不直接改动服务器、代码或主人数据。涉及代码或服务器的步骤仍走既有的
`app-tmp → 测试 → gate → 晋升` 与审批链，任务板只负责跟踪进度与沉淀证据引用。

## 数据位置

```text
workspace/tasks/
├── board.json           # 主板：进行中的任务（有界 max_tasks=200）
├── board-archive.json   # 归档：被挤出主板的已完成旧任务（有界 1000）
├── todos.json           # task_handler 的简单待办（并存，互不影响）
└── notes/               # task_handler 的笔记（并存）
```

- root 探测：`AGENELF_ROOT` 环境变量优先，否则按 `app/` 上一级推断；
  推断不到项目根时回退 `app/memory_store/`；
- 所有写入为原子 JSON（临时文件 + `os.replace`）；
- `board.json` 损坏或结构非法时重建空板，不会崩溃；
- 主板超过 200 条时，最旧的 `done` 任务被移入 `board-archive.json`，
  保持主板精简；归档同样有界（保留最近 1000 条）。

## 数据结构

```json
{
  "tasks": [
    {
      "id": "task-20260725-120000-ab12cd",
      "title": "修复登录页 500",
      "steps": [
        {"text": "复现问题", "status": "done", "note": "已复现"},
        {"text": "定位代码", "status": "doing", "note": ""},
        {"text": "修复并回归测试", "status": "pending", "note": ""}
      ],
      "status": "doing",
      "priority": "P1",
      "created_at": "2026-07-25T12:00:00+00:00",
      "updated_at": "2026-07-25T12:05:00+00:00",
      "done_at": null,
      "evidence": [],
      "linked_intention": null,
      "block_reason": ""
    }
  ]
}
```

- 任务状态：`open | doing | done | blocked`；
- 步骤状态：`pending | doing | done`，`task_advance` 每次把一步向前推一格；
- 全部步骤 `done` 时任务自动 `done` 并写入 `done_at`；
- `done` 是终态：不能再推进、阻塞或重复完成；
- 任务 ID 格式：`task-<时间戳>-<hash6>`。

## 工具

| 工具 | 说明 | 风险 |
|---|---|---|
| `task_create(title, steps=[], priority="P2")` | 创建任务；steps 为空时返回提示建议先分解 | change |
| `task_list(status="")` | 按状态过滤列出，含步骤进度 `x/y` | read |
| `task_advance(task_id, step_index, note="")` | 步骤 pending→doing→done；全 done 任务自动完成 | change |
| `task_complete(task_id, evidence=[])` | 带证据完成；剩余步骤一并标记 done | change |
| `task_block(task_id, reason)` | 标注阻塞原因；`task_advance` 可恢复推进 | change |
| `task_link_intention(task_id, intention_id)` | 关联改进意向 ID（只存 ID，不 import） | change |

所有变更 best-effort 追加 `logs/audit.log`：

```text
[2026-07-25T12:05:00+00:00] [task_board] action=advance id=task-... step=1 step_status=doing task_status=doing
```

## 与改进意向 / 自主循环的协作模式

任务分解时发现的**能力缺口**不应留在任务板里空转，协作闭环：

```text
task_create（分解步骤）
    │ 发现缺口：当前能力不足以完成某步
    ▼
create_improvement_intention（建立带验收条件的意向）
    ▼
task_link_intention(task_id, intent-...)   ← 只存 ID，松耦合
    ▼
pursue_improvement_intention(intent-...)   ← 由 agent 自行调用，
    │                                          进入 app-tmp→测试→gate→晋升链
    ▼
晋升历史 / 授权 / 测试报告等可信证据产出
    ▼
task_complete(task_id, evidence=["promotion-history:session-...", "auth:auth-...", ...])
```

`task_link_intention` 只记录意向 ID、不 import `self_development`：
两个能力域保持单向引用，推进动作与证据产出仍由各自的安全门控制。
反之，自主循环失败产生的意向也可以回填为任务板上的新任务，由主人确认优先级。

## 使用示例

### LLM 工具调用

```json
{"name": "task_create", "arguments": {
  "title": "修复登录页 500",
  "steps": ["复现问题", "定位代码", "修复并回归测试"],
  "priority": "P1"
}}
```

```json
{"name": "task_advance", "arguments": {"task_id": "task-20260725-120000-ab12cd", "step_index": 0, "note": "已复现，偶发"}}
```

```json
{"name": "task_complete", "arguments": {"task_id": "task-20260725-120000-ab12cd", "evidence": ["promotion-history:session-9f2", "app/tests/test_login.py"]}}
```

### CLI / Python

```python
import json
from skills import task_board

print(task_board.execute("task_create", {
    "title": "整理本周运维报告",
    "steps": ["收集日志", "汇总异常", "输出报告"],
}))
print(task_board.execute("task_list", {"status": "doing"}))
print(task_board.execute("task_block", {
    "task_id": "task-...", "reason": "等待对方接口就绪",
}))
```

返回均为 JSON 字符串，`ok: false` 时带 `error` 说明；
`execute` 协议保证永不抛异常。
