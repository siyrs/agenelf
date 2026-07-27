# 交互式 CLI 命令菜单

Agenelf 的交互终端使用 `prompt-toolkit` 提供 Claude Code 风格的斜杠命令菜单。

## 使用方式

在 `你 >` 后输入：

```text
/
```

终端会立即显示命令清单和说明。操作键：

- `↑` / `↓`：选择菜单项；
- `Tab`：把当前选项补全到输入框；
- `Shift+Tab`：选择上一个候选；
- `Enter`：执行；
- `Ctrl+C`：取消当前输入。

支持命令名过滤，例如：

```text
/ap
```

会收敛为：

```text
/approvals
/approve
```

部分参数也会动态补全：

- `/approve`、`/deny`：等待审批的 `op-...` 请求；
- `/ops`：最近运维请求；
- `/reload`：当前已加载技能；
- `/validate`：`check`、`suite`、`result`；
- `/remember`：`fact`、`preference`；
- `/intend`：`P0`～`P3`。

输入 `/help` 可显示完整命令表。`/commands` 是 `/help` 的别名，`/exit` 是 `/quit` 的别名。未知命令会给出最接近的建议。

## Windows 启动

首次更新到本版本需要重建镜像，以安装 `prompt-toolkit`，并重新创建容器以应用新的源码挂载：

```powershell
git switch main
git pull --ff-only origin main
docker compose up -d --build --force-recreate
```

然后继续使用：

```powershell
docker compose exec agenelf python /agenelf/app-fork/cli.py
```

路径仍叫 `/agenelf/app-fork` 是为了保持现有命令兼容，但 Docker Compose 现在把宿主机 `app/` 直接只读挂载到该路径。这样 `git pull` 后不会再因为旧 `app-fork/` 副本而出现“主分支有命令、正在运行的 CLI 却提示未知命令”的漂移。

## 非交互环境

当 stdin/stdout 不是 TTY 时，CLI 自动回退到原有的 Rich 文本输入，不启用菜单，便于管道、测试和后台任务使用。

临时关闭菜单：

```powershell
$env:AGENELF_INTERACTIVE_COMPLETION="0"
```

强制在特殊终端启用：

```powershell
$env:AGENELF_FORCE_INTERACTIVE_PROMPT="1"
```
