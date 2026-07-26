# 服务器与远程 Docker 运维能力

Agenelf 通过隔离的 SSH Runner 操作已经配置的服务器。Agent 只负责把意图转换为结构化请求；SSH 私钥只挂载到 Runner，永远不进入模型上下文。

## 当前能力

| 能力 | 风险 | 行为 |
|---|---|---|
| `inspect` | 只读 | 主机、负载、磁盘、内存和 Docker 概览 |
| `docker_ps` | 只读 | 查看全部容器 |
| `get_docker_logs` | 只读 | 读取指定容器最近 1–1000 行日志并脱敏 |
| `inspect_docker_container` | 只读 | 查看状态、镜像、挂载、标签、重启策略和网络；排除 `Config.Env` |
| `run_docker_check` | 只读 | 运行主人在 `docker_checks` 中预配置的诊断别名 |
| `restart_docker_container` | 变更 | 精确审批后重启指定容器并读取新状态 |
| `service_status` | 只读 | 查看允许清单中的 systemd 服务 |
| `apt_update` | 变更 | 执行 `apt-get update` |
| `compose_deploy` | 变更 | 校验、备份、部署 Compose；失败尝试恢复上一份 Compose |
| `service_restart` | 变更 | 重启允许清单中的 systemd 服务 |
| `docker_install` | 高权限 | 安装 Docker 与 Compose 插件 |

它**不会**开放任意远程 Shell，也不会让模型自由生成 `docker exec` 命令。模型只能选择主人预配置的诊断别名。

## 1. 初始化运行目录

```bash
make init
```

新安装使用：

- `local/servers.yaml`：服务器与运维允许清单；
- `local/secrets/`：SSH 私钥和 `known_hosts`；
- `.ops-runner.env`：Runner 专用环境变量。

生成 API Token：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

把结果填入 `.env` 的 `AGENELF_API_TOKEN`。用 `id -u` 和 `id -g` 填写 `.env` 的 `AGENELF_UID`、`AGENELF_GID`，使容器使用宿主机账号访问绑定目录。

## 2. 目标服务器账号

建议创建专用用户，不要使用 root：

```bash
sudo useradd --create-home --shell /bin/bash agenelf
sudo install -d -o agenelf -g agenelf /srv/agenelf
```

Docker 已安装时，可让该账号加入 docker 组：

```bash
sudo usermod -aG docker agenelf
```

重新登录后生效。Docker 组权限接近 root，因此该账号仍应视为运维账号。`apt_update`、`docker_install` 和 `service_restart` 使用 `sudo -n`，请通过 `visudo` 只放行实际需要的命令，不要配置 `NOPASSWD: ALL`。

## 3. SSH 配置

把私钥放入 `local/secrets/`：

```bash
chmod 700 local/secrets
chmod 600 local/secrets/primary_ed25519
```

通过可信渠道核对主机指纹后写入：

```bash
ssh-keyscan -H 服务器IP >> local/secrets/known_hosts
chmod 600 local/secrets/known_hosts
```

生产环境保持 `allow_unknown_host_key: false`。

## 4. 服务器与 Docker 策略

`local/servers.yaml` 示例：

```yaml
servers:
  pve-ubuntu:
    host: 192.168.50.202
    port: 22
    username: sirius
    auth:
      type: private_key
      private_key: pve_ubuntu_ed25519
    known_hosts: known_hosts
    allow_unknown_host_key: false
    managed_root: /srv/agenelf
    docker_command: docker

    allowed_operations:
      - inspect
      - docker_ps
      - service_status
      - compose_deploy
      - service_restart

    allowed_docker_operations:
      - get_docker_logs
      - inspect_docker_container
      - run_docker_check
      - restart_docker_container

    allowed_containers:
      - sing-box
      - 9router

    docker_checks:
      sing-box-config:
        container: sing-box
        argv: [sing-box, check, -c, /etc/sing-box/config.json]

    allowed_services:
      - docker
      - nginx
    allowed_bind_roots:
      - /srv/agenelf-data
```

说明：

- `allowed_docker_operations` 缺省时，为兼容旧配置，允许当前版本提供的结构化 Docker 操作；
- `allowed_containers` 缺省时，允许任何语法有效的容器名；生产环境建议显式列出；
- `docker_checks` 的 `argv` 由主人保存在本地配置中。请求文件只记录别名，不记录也不接受模型提供的命令；
- `docker_command` 仅允许 `docker` 或 `sudo -n docker`；
- Agent 能读取清单摘要，但看不到 `local/secrets/` 和 `.ops-runner.env`。

## 5. Runner 热刷新

统一 `ops-runner` 在**每次扫描请求队列前**重新读取 `local/servers.yaml`。新增 `pve-ubuntu` 后，不再需要为“未知服务器别名”手动重启 Runner。

若编辑过程中 YAML 临时不完整，Runner 会：

1. 写入 `profiles_reload_failed` 审计记录；
2. 保留最后一份有效服务器快照；
3. 下一轮继续尝试加载，而不是崩溃退出。

修改 Compose Runner 入口的版本升级后，需要执行一次：

```bash
make start
```

后续仅修改 `local/servers.yaml` 不需要重启。

## 6. sing-box 排查示例

对话中可直接说：

```text
查看 pve-ubuntu 上 sing-box 最近 200 行日志。
安全检查 sing-box 容器的镜像、挂载、网络和重启状态。
运行 pve-ubuntu 预配置的 sing-box-config 检查。
重启 pve-ubuntu 上的 sing-box，并确认重启后的状态。
```

前三项是只读操作，会自动进入 Runner。重启会返回类似：

```text
Docker 运维请求已创建：op-0123456789abcdef
批准命令：bash scripts/approve.sh op-0123456789abcdef approve
```

检查载荷后批准或拒绝：

```bash
cat data/ops-requests/op-0123456789abcdef.json
bash scripts/approve.sh op-0123456789abcdef approve
bash scripts/approve.sh op-0123456789abcdef deny "暂不重启"
```

批准只绑定该服务器、容器、超时和操作指纹。参数变化必须创建新请求。

## 7. 输出隐私边界

Runner 返回日志或 inspect 结果前会脱敏：

- 常见密码、Token、API Key、Bearer 值；
- `vmess://`、`vless://`、`trojan://`、`ss://`、`ssr://`、`hysteria://`、`tuic://` 节点 URI；
- URL 查询参数中的 `token`、`secret`、`password`、`api_key` 等；
- Docker inspect 不读取 `Config.Env`。

日志仍可能包含业务特有秘密，敏感服务应进一步缩小日志范围和容器允许清单。

## 8. 升级技能后继续原任务

当主人要求“先完善 Docker 技能、重载后继续修复 VPN”时，Agent 必须先调用 `checkpoint_task_continuation` 保存脱敏、带过期和幂等键的续跑检查点，再进入 autonomy/evolution/restart。

`make chat` 的启动顺序是：

1. 运行 `app/resume.py`，最多认领一个 `pending` 检查点；
2. 自动续跑一次；
3. 打开交互 CLI；
4. 原任务真实完成后，凭运维/测试证据标记 `completed`。

自动续跑不会继承新的远程变更授权；新的容器重启仍需精确审批。需要跳过一次自动续跑时：

```bash
AGENELF_SKIP_AUTO_RESUME=1 make chat
```

## 9. Compose 安全红线

以下配置在 Agent 和 Runner 两层拒绝：

- `privileged: true`；
- `network_mode: host`、`pid: host`、`ipc: host`、`userns_mode: host`；
- `cap_add: [ALL]`；
- `devices` 映射；
- `/var/run/docker.sock` 挂载；
- 宿主机根目录 `/` 挂载；
- 不在 `allowed_bind_roots` 中的绝对路径挂载。

## 10. 审计与故障排查

- Agent 请求：`data/ops-requests/`；
- 人类决定：`data/auth-decisions/`；
- Runner 结果：`data/ops-results/`；
- 续跑状态：`data/continuations/`；
- Agent 操作日志：`logs/operations.log`；
- Runner 日志：`logs/ops-runner.log`；
- 裁决日志：`logs/audit.log`。

```bash
make status
docker compose logs -f agenelf ops-runner
```

Runner 失败不会回退到任意 Shell。它会返回明确退出码和脱敏结果；修正配置或权限后，应重新提交结构化请求。
