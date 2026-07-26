# Compose Down 与自主迭代恢复闭环

本版本同时解决两个相互关联的问题：

1. Agenelf 缺少结构化 `docker compose down` 能力；
2. 主人要求它自我迭代补能力时，候选沙盒、测试环境、工具循环和模型流式连接可能把 CLI 拖入失败循环。

## 1. 安全的 Compose Down

对话中可以直接说：

```text
把 pve-ubuntu 上的 vpn Compose 项目 down 掉，保留卷和镜像。
```

Agent 会调用 `down_compose_project`，生成精确绑定请求。也可以先要求只看计划：

```text
先 plan_only 看一下 pve-ubuntu/vpn 的 compose down 计划。
```

运行边界：

- 仅允许 `local/servers.yaml` 中已有的服务器别名；
- 项目只能位于 `managed_root/<project>`；
- 项目名只能包含字母、数字、点、下划线和短横线；
- Runner 必须先确认 `compose.yaml` 存在并通过 `docker compose config --services`；
- 实际命令只包含 `down --timeout ...` 和可选 `--remove-orphans`；
- 永远不传 `--volumes` 或 `--rmi`；
- named volumes、镜像、`compose.yaml` 和 `.agenelf-backups` 会保留；
- 每次仍需主人对服务器、项目、超时和参数进行精确审批。

审批示例：

```text
/approve op-0123456789abcdef
```

或：

```text
审批通过 op-0123456789abcdef
```

Windows 备用入口：

```powershell
.\scripts\approve.ps1 op-0123456789abcdef approve
```

为了兼容已有私人配置，只要服务器已经允许 `compose_deploy`，新的 `compose_down` 也可提交，但它仍会创建独立审批请求；新模板会显式列出 `compose_down`。

## 2. app-tmp 生命周期

旧实现把 `/agenelf/app-tmp` 建成容器内 tmpfs，却由宿主机 `gate_check.sh` 和 `promote.sh` 读取。两边看见的不是同一份候选；同时代码还尝试删除挂载点本身，容易出现：

```text
[Errno 17] File exists: '/agenelf/app-tmp'
```

新实现：

- `./app-tmp` 以唯一可写代码候选目录挂载到容器；
- 清理时只删除目录内容，绝不删除挂载根；
- 候选放在 `app-tmp/repo/app`；
- 宿主机 gate 与 promote 读取完全相同的候选；
- `app/` 主源码、scripts、policy 和最终运行时代码仍为只读。

## 3. 完整但不含秘密的仓库快照

仓库级测试需要 `.github/workflows`、policy、scripts、Compose 拓扑、文档和 example 文件。旧候选只复制 `app/`，因此会把“CI 文件不存在”等环境缺失误判为代码回归。

现在 Compose 只把明确安全的仓库夹具挂载到 `/agenelf/repo-source`，然后复制到候选仓库。不会包含：

- `local/servers.yaml`；
- `local/secrets/`；
- `.env`；
- owner memory/self 数据；
- SSH 密钥、Token 或真实生产配置。

在请求模型写代码之前，`evolution_begin` 会先对原样候选执行可信基线测试。基线失败时状态变为 `baseline_failed`，不会进入补丁生成阶段。

## 4. 禁止通过修改测试“修复”失败

自主候选可以新增：

```text
tests/test_new_feature.py
```

但不能修改或删除任何原有测试与测试夹具。以下路径会被 Agent、测试 Runner 和宿主机 gate 三层拒绝：

- 覆盖现有 `tests/test_*.py`；
- 修改 `tests/__init__.py` 做 monkey-patch；
- 删除失败测试；
- 修改 `.github/workflows`、policy、scripts 或 gate；
- 修改核心权限、审批、Runner、持续对话和候选工作区模块。

可信基线测试与候选新增测试在不同 Python 进程中运行，新测试无法先 monkey-patch 基线测试环境。

## 5. 宿主机控制面目标快速分流

下面这类目标本来就不属于 app-tmp 自主范围：

- 修改 ops/approval/validation runner；
- 增加 Compose down 等 Runner 操作；
- 修改 Docker 挂载、网络拓扑；
- 修改审批、策略、CI、CodeQL、scripts 或 gate。

`evolution_scope_guard` 会在创建候选前返回：

```text
status: host_review_required
next_action: human_managed_repository_change
```

它不会再重复尝试 80 轮不可能通过的沙盒任务。

## 6. 工具无进展熔断

同一工具名、相同参数和相同归一化结果连续出现 3 次时，当前任务会停止无效循环，保存 `task_continuation` 检查点，并保留 CLI：

```text
reason: automatic_no_progress_loop
```

动态 operation ID 和时间戳会在比较前归一化，因此重复创建同载荷请求也会被识别。

## 7. reasoning 流中断恢复

遇到：

```text
RemoteProtocolError: incomplete chunked read
```

运行时会：

1. 判断是否为连接、超时、流不完整、429 或 5xx；
2. 关闭当前请求的流式模式；
3. 对同一模型轮次执行有界重试；
4. 成功后恢复下一轮的实时 reasoning；
5. 仍失败时保存续跑检查点并返回 CLI，不再抛出整段 traceback 退出。

认证错误、参数错误和其他非瞬态错误不会盲目重试。

## 8. 升级部署

本版本修改了 `app-tmp` 挂载和运行时能力，需要重新创建容器：

```powershell
git switch main
git pull --ff-only origin main
docker compose down
docker compose up -d --build --force-recreate
```

这里的本地 `docker compose down` 是更新 Agenelf 自身容器；不会带 `-v`，因此命名卷保留。

启动后检查：

```powershell
docker compose ps -a
docker compose logs --tail=100 agenelf ops-runner approval-runner
```

然后进入：

```powershell
docker compose exec agenelf python /agenelf/app-fork/cli.py
```

可用 `/skills` 确认：

- `compose_lifecycle`；
- `evolution_scope_guard`；
- `zz_transport_resilience`；
- `evolution_ops` 版本 `0.2.0`。
