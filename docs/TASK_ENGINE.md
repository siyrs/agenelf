# Agenelf 受治理长期任务引擎

## 定位

Agenelf 已有 `agent.task_board`，适合轻量待办、步骤推进和完成归档。本文件描述更高层的 `agent.workflow`：它面向跨会话、跨渠道、可暂停恢复且包含真实副作用的长期任务。

Task Engine **不直接执行** SSH、Docker、代码补丁或模型调用。它只保存任务定义、状态、步骤依赖、授权引用和证据；真实动作仍分别交给：

- `server.operations`；
- `software.validation`；
- 受控代码沙盒与 Gate；
- 后续 `software.release`。

## 数据位置

```text
data/tasks/task-<16hex>.json
logs/task-engine.log
```

每个任务单文件原子写入，并包含递增 `revision`。CLI、HTTP、Web、Mobile 和 Voice 修改任务时可以携带 `expected_revision`；版本不一致会拒绝覆盖。

## 创建契约

任务必须包含：

```json
{
  "title": "部署并验证服务",
  "owner_goal": "服务升级后保持可用",
  "priority": "P1",
  "steps": [],
  "acceptance_criteria": ["冒烟验证通过"],
  "evidence_plan": ["保存运维请求和验证请求 ID"],
  "rollback_plan": "恢复上一版 compose"
}
```

只要任一步风险不是 `read`，`rollback_plan` 就必须存在。任务步骤只保存能力、操作、目标和 `parameters_ref`，不允许把密码、Token 或完整私钥写入任务。

## 任务状态机

```text
proposed -> planned -> running -> verifying -> completed
                 |         |
                 |         +-> waiting_approval -> running
                 +-> paused -> running
                 +-> failed -> planned
                 +-> cancelled
```

- `completed` 和 `cancelled` 是终态；
- `failed` 可以经人工或策略复盘后回到 `planned`；
- 主人取消后，未完成步骤统一标记为 `cancelled`；
- `completed` 只能从 `verifying` 进入。

## 步骤状态机

```text
pending -> running -> succeeded
   |          |          
   |          +-> waiting_approval -> running/succeeded
   |          +-> failed -> pending/running
   +-> skipped/cancelled
```

依赖步骤必须先处于 `succeeded` 或 `skipped`。高风险步骤进入 `waiting_approval` 时必须关联 `op-` 或 `auth-` 请求 ID；成功步骤必须关联证据引用。

## 可信完成门

任务不能因为模型说“完成了”而结束。全部步骤成功后，任务进入 `verifying`，且至少存在一条格式有效的可信证据：

| 类型 | 引用示例 |
|---|---|
| `operation` | `op-0123456789abcdef` |
| `validation` | `val-fedcba9876543210` |
| `test` | `data/test-results/run-001.json` |
| `promotion` | `evo-20260725-001` |

普通 `note`、聊天回复或模型总结不是可信完成证据。

## 能力工具

`app/skills/workflow_tasks.py` 暴露：

```text
workflow_create_task
workflow_list_tasks
workflow_get_task
workflow_transition_task
workflow_update_step
workflow_add_evidence
workflow_next_action
```

这些工具只改变 Agenelf 自身任务记录，因此不能代替服务器审批、验证 Runner 或代码晋升 Gate。

## 组合示例：部署服务

```text
1. workflow_create_task
2. server.operations.inspect           -> op-...
3. workflow_update_step(succeeded)     -> 关联 op-...
4. server.operations.compose_deploy    -> awaiting approval
5. workflow_update_step(waiting_approval, auth/op ID)
6. 主人批准，Runner 执行               -> op-... result
7. software.validation.run_suite       -> val-...
8. workflow_update_step(succeeded)     -> 关联 val-...
9. workflow_transition_task(completed)
10. self_development 沉淀结果与教训
```

## 当前边界

本轮已完成长期任务状态、证据门、暂停恢复、取消、失败重试和多端并发保护。尚未宣称完成：

- 后台定时调度器；
- 自动 DAG 并行执行器；
- 通用跨能力补偿/回滚执行器；
- 手机 APP 与语音客户端 UI。

这些后续组件必须继续复用当前 Task Engine，而不能创建旁路执行通道。
