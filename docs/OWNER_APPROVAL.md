# Windows / CLI 主人审批闭环

Agenelf 的远程变更仍然使用“精确请求指纹 + 短时效 + 单次裁决”的审批模型，但主人不再需要依赖 Bash。

## 交互式 CLI（推荐）

当 Agenelf 返回：

```text
运维请求已创建：op-0123456789abcdef
```

可以直接在同一个 `你 >` 提示符输入：

```text
/approve op-0123456789abcdef
```

或：

```text
审批通过 op-0123456789abcdef
```

拒绝请求：

```text
/deny op-0123456789abcdef 暂不修改端口
```

查看待审批清单：

```text
/approvals
```

只有一个待审批载荷时，也可以直接输入：

```text
审批通过
```

若存在多个不同载荷，CLI 会列出请求并要求明确 ID；不会猜测。多个完全相同的重复请求会批准最新请求，并自动拒绝其余重复请求，避免 Compose 被执行多次。

审批成功后，CLI 会自动查询执行结果，并让 Agent 从批准前的任务继续验证，不需要再次输入“继续”。

## 为什么不是 Agent 自己批准

`/approve` 和明确的中文审批短语在 `Console.input` 返回后、进入 `Agent.chat()` 之前解析。该能力没有注册成模型工具，模型输出、长期记忆和普通聊天文本都不能写入最终裁决。

CLI 只写入短时效、HMAC 签名的 `data/approval-commands/`。独立的 `approval-runner` 会再次验证：

- 命令来源必须是 `interactive_cli`；
- 请求 ID 格式；
- 当前请求文件仍存在；
- 当前请求指纹与主人看到的请求一致；
- HMAC 签名与过期时间；
- 已有最终裁决不可被反向覆盖。

最终 `data/auth-decisions/` 仍在 Agent 容器内保持只读；只有无网络、无 SSH 密钥的 `approval-runner` 拥有写权限。

## Windows PowerShell 备用方式

即使审批代理没有启动，也不需要 Bash：

```powershell
.\scripts\approve.ps1 op-0123456789abcdef approve
```

或者：

```powershell
py -3 .\scripts\approve.py op-0123456789abcdef approve
```

拒绝：

```powershell
.\scripts\approve.ps1 op-0123456789abcdef deny "暂不执行"
```

宿主机审批工具始终优先导入 `app/` 源码真理源，所以刚执行 `git pull`、尚未同步旧的 `app-fork/` 时也能完成审批。

Linux/macOS 的旧命令仍可用：

```bash
bash scripts/approve.sh op-0123456789abcdef approve
```

完整仓库中 `approve.sh` 会优先调用相同的 Python 实现。为兼容旧安装和“只复制一个脚本”的场景，它仍保留已测试的单文件后备实现。

## 部署升级

本版本新增 `approval-key-init` 和 `approval-runner`，首次升级需要重新创建 Compose 服务。

Windows PowerShell 原生方式：

```powershell
git switch main
git pull --ff-only origin main
powershell -ExecutionPolicy Bypass -File .\scripts\sync_fork.ps1
docker compose up -d --build --force-recreate
```

Git Bash、WSL、Linux 或 macOS 也可继续使用：

```bash
git switch main
git pull --ff-only origin main
bash scripts/sync_fork.sh
docker compose up -d --build --force-recreate
```

确认：

```powershell
docker compose ps -a
docker compose logs --tail=100 approval-key-init approval-runner ops-runner
```

`approval-key-init` 是一次性初始化任务，显示 `Exited (0)` 属于正常状态。它把随机控制密钥保存在 Docker 命名卷中，不写入 Git，也不需要主人手工配置。`approval-runner` 不挂载 SSH 私钥、没有网络，仅能读取请求和签名命令、写入裁决与审批结果。

## 故障排查

审批超时时检查：

```powershell
docker compose up -d approval-runner ops-runner
docker compose logs --tail=100 approval-runner
```

队列：

- `data/approval-commands/`：CLI 提交的签名命令；
- `data/approval-results/`：审批代理结果；
- `data/auth-decisions/`：最终指纹绑定裁决；
- `data/ops-results/`：远程执行结果。

不要手工编辑这些 JSON。需要重试时重新输入 `/approve <op-id>`；相同裁决是幂等的，参数变化则必须创建新请求。
