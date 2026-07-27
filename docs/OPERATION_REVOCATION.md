# 运维请求执行前撤销

Agenelf 的主人可以撤销一个**尚未开始执行**的 `op-*` 运维请求。撤销不是修改审批文件，也不是删除请求，而是通过与 `ops-runner` 相同的请求锁进行原子竞争，并写入可信的 `cancelled` 终态。

## 适用场景

- 已批准端口、Compose、Docker 重启或服务器变更，但随后改变了任务范围；
- Runner 暂停期间发现请求参数虽然正确，但已经不再需要执行；
- 希望立即终止等待中的请求，而不是等待请求 TTL 到期；
- 已经生成请求，但尚未批准，也希望明确留下“主人取消”的终态证据。

## Windows 使用

在项目根目录执行：

```powershell
.\scripts\revoke.ps1 op-0123456789abcdef "任务范围已改变"
```

也可以使用 Python：

```powershell
py -3 .\scripts\revoke.py op-0123456789abcdef "任务范围已改变"
```

撤销最近一个仍可撤销的请求：

```powershell
.\scripts\revoke.ps1 latest "不再执行"
```

## Linux/macOS 使用

```bash
bash scripts/revoke.sh op-0123456789abcdef "任务范围已改变"
```

或：

```bash
python3 scripts/revoke.py op-0123456789abcdef "任务范围已改变"
```

## Agenelf 中的只读查询

`operation_control` 技能会自动加载。可以用自然语言要求：

```text
列出当前仍可撤销的运维请求，并告诉我 Windows PowerShell 命令。
```

模型可以调用：

- `list_revocable_operations`
- `get_operation_control_status`
- `get_operation_revocation_instructions`

这些工具全部是 `read + pure`。它们不会写取消结果，也不能替主人撤销请求。

## 原子竞争协议

每个运维请求使用：

```text
data/ops-locks/<op-id>.lock
```

执行和撤销遵循同一把锁：

```text
主人撤销                              ops-runner
    |                                     |
    |-- O_EXCL 创建同一请求锁 ------------>|
    |                                     |
    |  成功：主人赢得竞争                 | 创建锁失败，稍后看到 cancelled 结果
    |  写 data/ops-results/<id>.json       |
    |  status=cancelled                    |
    |  commands=[]                         |
    |  cancellation.started=false          |
    |  释放锁                              |
```

反过来，如果 Runner 已经取得锁：

```text
ops-runner 先取得锁
→ 撤销命令失败关闭
→ 明确提示“可能已经开始”
→ 不会伪装成撤销成功
```

因此不存在“撤销命令显示成功，但 SSH 操作其实仍在执行”的静默竞态。

## 可信终态

撤销成功后会保留原始请求，并新增：

```json
{
  "schema_version": 2,
  "id": "op-0123456789abcdef",
  "status": "cancelled",
  "commands": [],
  "cancellation": {
    "request_id": "op-0123456789abcdef",
    "request_fingerprint": "...",
    "cancelled_at": "...",
    "cancelled_by": "host:sirius",
    "reason": "任务范围已改变",
    "started": false
  }
}
```

不会删除：

- `data/ops-requests/<id>.json`
- 已存在的批准决定
- 审计日志
- 其他请求和结果

保留批准决定是有意设计：审计记录应当显示“主人曾批准，随后在执行前撤销”，而不是覆盖历史。

## 安全边界

- 模型没有 `revoke_operation` 工具；
- 撤销必须从宿主机 PowerShell、Python 或 Shell 执行；
- 请求 ID、完整载荷指纹和当前请求文件会重新校验；
- 请求已过期时不会通过撤销重新激活；
- 请求已有成功、失败、阻断或过期终态时不能覆盖；
- 请求已被 Runner 锁定时撤销失败关闭；
- 撤销结果明确包含 `commands: []` 与 `started: false`；
- 原始请求、决定和审计证据不会被删除或重写。

## 状态排查

在 Agenelf 中查看：

```text
/ops op-0123456789abcdef
```

或者要求：

```text
检查 op-0123456789abcdef 是否还能撤销，只返回安全状态，不要显示 Compose 正文或参数秘密。
```

可能状态：

| 状态 | 含义 |
|---|---|
| `revocable: true` | 尚无结果、未过期、未被 Runner 锁定 |
| `cancelled` | 已成功在执行前撤销 |
| `executing: true` | Runner 已取得执行锁，不能宣称撤销成功 |
| `expired` | 请求 TTL 已过，Runner 会写入 `expired` 终态 |
| `succeeded/failed/blocked` | 已有可信终态，不能覆盖为取消 |

## 与拒绝审批的区别

- `/deny`：在审批阶段拒绝请求；
- `revoke.ps1`：即使请求已经批准，只要 Runner 还没开始，也可以原子撤销；
- 请求已经开始后：当前短操作不能强行中断，只能等待可信结果，再按操作类型执行回滚或新的修复请求。
