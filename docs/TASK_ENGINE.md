# Agenelf Task Engine

## Purpose

Task Engine 是私人助理阶段的基础，不直接执行危险动作，只负责长期任务状态管理。

## Flow

```text
主人目标
 -> Task Plan
 -> Capability Selection
 -> Permission Check
 -> Execution
 -> Validation
 -> Evidence
 -> Learning
```

## Rules

- 每个任务必须有验收条件。
- 每个完成状态必须有证据。
- 变更任务必须经过现有权限系统。
- Voice/Web/Mobile 后续只能接入同一 Task Engine。

## Future

- 定时任务
- DAG 工作流
- 失败重试
- 回滚编排
- 任务经验沉淀
