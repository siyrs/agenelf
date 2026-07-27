# Agenelf 运维请求生命周期

Agenelf 的远程服务器与 Docker 操作由 Agent 生成结构化请求，再由隔离的确定性 Runner 执行。请求文件不是永久授权：每个请求都有精确载荷指纹和有限有效期，相同的未完成操作会复用现有请求。

## 解决的问题

旧流程中，每次模型重复调用变更工具都会创建新的 `op-*`：

```text
op-a
op-b
op-c
```

即使三者的服务器、容器和参数完全相同，也会形成多个审批项。另一方面，请求本身没有失效时间；旧请求在 Runner 长时间停机后仍可能重新进入执行，主人难以确认它是否仍符合当前意图。

新流程提供：

1. **同载荷幂等复用**：相同且未完成的请求只保留一个 `op-*`；
2. **请求有效期**：请求超过时限后不能再批准或执行；
3. **审批有效期**：已过期的批准不会让旧请求恢复执行；
4. **Runner 最终防线**：即使宿主机目录中存在旧决定，Runner 也会在连接 SSH 前把过期请求写为 `expired`；
5. **Doctor 队列债务**：`/doctor` 会显示过期、重复和无效请求。

## 默认有效期

| 风险 | 默认有效期 | 环境变量 |
|---|---:|---|
| `read` | 120 秒 | `AGENELF_OPERATION_READ_TTL_SECONDS` |
| `change` | 1800 秒 | `AGENELF_OPERATION_CHANGE_TTL_SECONDS` |
| `privileged` | 900 秒 | `AGENELF_OPERATION_PRIVILEGED_TTL_SECONDS` |

允许范围为 15–86400 秒。新请求会写入：

```json
{
  "created_at": "2026-07-27T10:00:00+00:00",
  "expires_at": "2026-07-27T10:30:00+00:00",
  "ttl_seconds": 1800
}
```

旧请求没有 `expires_at` 时，会用 `created_at + 风险默认有效期` 兼容计算；时间字段损坏或缺失时，Runner 失败关闭，不连接服务器。

## 同载荷复用

复用条件同时满足：

- `capability` 相同；
- `operation` 相同；
- `target` 相同；
- `parameters` 的规范化 JSON 相同；
- 有效风险级别相同；
- 请求尚未产生结果；
- 请求未过期；
- 没有拒绝决定；
- 没有过期的批准决定。

满足条件时，`submit_operation` 返回原请求 ID，并增加：

```json
{
  "reused_existing": true,
  "reuse_reason": "identical_unfinished_request"
}
```

读操作一旦已有结果，不会复用，因为巡检、日志和状态查询通常需要新鲜数据。

确实需要并行创建同载荷请求的可信代码，可以显式使用：

```python
submit_operation(..., deduplicate=False)
```

模型工具默认不会这样做。

## 状态

`/ops <op-id>` 或结构化查询可能返回：

| 状态 | 含义 |
|---|---|
| `queued` | 只读请求等待 Runner |
| `awaiting_approval` | 变更请求等待主人批准 |
| `collecting_approval` | 多人审批尚未达到法定人数 |
| `approved` | 已批准，等待 Runner |
| `approval_expired` | 审批窗口已关闭，必须重新提交 |
| `expired` | 请求自身已经过期，不会连接服务器 |
| `denied` | 主人拒绝 |
| `succeeded` / `failed` / `blocked` | 已有可信 Runner 结果 |

过期请求不会出现在 `/approvals` 中。显式输入旧 ID 会得到清晰错误，而不是创建无效决定。

## 审批入口

推荐在交互 CLI 中使用：

```text
/approve op-xxxxxxxxxxxxxxxx
```

也支持：

```text
审批通过 op-xxxxxxxxxxxxxxxx
```

Windows PowerShell 备用：

```powershell
.\scripts\approve.ps1 op-xxxxxxxxxxxxxxxx approve
```

跨平台 Python 备用：

```powershell
python .\scripts\approve.py op-xxxxxxxxxxxxxxxx approve
```

PowerShell/Python 入口与交互 CLI 使用同一待审批目录和过期判定。过期请求不能通过备用脚本重新激活。

## Runner 行为

`ops-runner` 由 Supervisor 启动新的生命周期入口：

```text
runner_supervisor.py
  └─ ops_runner_entry.py
       └─ UnifiedOpsRunner
```

处理顺序：

```text
读取请求
→ 检查是否已有结果
→ 检查请求有效期
→ 已过期：写入 expired 结果，commands=[]，不加载服务器配置，不连接 SSH
→ 未过期：进入 UnifiedOpsRunner 原有验证
→ 校验 Schema、指纹、风险、allowlist、审批和参数
→ 执行 SSH 操作
```

因此请求有效期是额外防线，不替代原有审批、服务器 allowlist、Docker 诊断别名或幂等结果检查。

## `/doctor`

运行：

```text
/doctor
```

队列部分新增：

```json
{
  "pending_operations": 2,
  "expired_unresolved_operations": 1,
  "duplicate_pending_operations": 1,
  "invalid_operation_requests": 0
}
```

含义：

- `pending_operations`：仍在有效期内且尚无结果的请求；
- `expired_unresolved_operations`：已经过期、但 Runner 尚未写入终态；
- `duplicate_pending_operations`：历史遗留的同指纹有效请求数量，扣除每组保留的一条；
- `invalid_operation_requests`：无效 JSON 或文件名与 ID 不一致的请求。

出现后三类时 Doctor 返回 `degraded`，并给出对应处理建议。恢复 `ops-runner` 后，过期请求会自动写成 `expired`；不需要删除请求或审批证据。

## 更新部署

本功能修改了 `ops-runner` 的子入口，更新后需重新创建该服务：

```powershell
git switch main
git pull --ff-only origin main

docker compose up -d --build --force-recreate ops-runner agenelf
```

检查：

```powershell
docker compose logs --tail=100 ops-runner
docker compose exec agenelf python /agenelf/app-fork/cli.py
```

然后执行：

```text
/doctor
/approvals
/ops
```

不要手工删除 `data/ops-requests`、`data/auth-decisions` 或 `data/ops-results`；这些文件是审计和幂等证据。
