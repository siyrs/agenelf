# 服务器运维能力

## 当前能做什么

Agenelf 可以通过聊天把意图转换为以下结构化操作：

| 操作 | 风险 | 行为 |
|---|---|---|
| `inspect` | 只读 | 主机名、身份、系统、负载、磁盘、内存、Docker 状态 |
| `docker_ps` | 只读 | 查看全部容器 |
| `service_status` | 只读 | 查看允许清单中的 systemd 服务 |
| `apt_update` | 变更 | 执行 `apt-get update` |
| `compose_deploy` | 变更 | 校验、备份、部署 Compose；失败尝试恢复上一份 Compose |
| `service_restart` | 变更 | 重启允许清单中的 systemd 服务 |
| `docker_install` | 高权限 | 从 Ubuntu/Debian 仓库安装 Docker 与 Compose 插件 |

它**不能**执行任意远程 Shell，也不能管理未配置服务器、未允许服务或安全红线内的 Compose。

## 1. 创建运行文件

```bash
cp .env.example .env
cp .ops-runner.env.example .ops-runner.env
cp config/servers.example.yaml config/servers.yaml
mkdir -p secrets data/auth-decisions data/ops-requests data/ops-results data/ops-locks logs
```

生成 API Token：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

把结果填入 `.env` 的 `AGENELF_API_TOKEN`。同时用 `id -u` 和 `id -g` 确认宿主机账号 UID/GID，并填写 `.env` 的 `AGENELF_UID`、`AGENELF_GID`；两个容器将以该身份访问绑定目录和 SSH 私钥。

## 2. 创建服务器专用账号

建议在目标服务器创建专用用户，不要使用 root：

```bash
sudo useradd --create-home --shell /bin/bash agenelf
sudo install -d -o agenelf -g agenelf /srv/agenelf
```

Docker 已安装时，可让该账号加入 docker 组：

```bash
sudo usermod -aG docker agenelf
```

重新登录后生效。Docker 组权限接近 root，因此该账号仍应视为运维账号，并仅用于这台 Runner。

对于 `apt_update`、`docker_install` 和 `service_restart`，Runner 使用 `sudo -n`。请通过 `visudo` 只授权实际需要的命令，不要给该账号开放无条件 `NOPASSWD: ALL`。不同发行版的二进制路径不同，先用 `command -v apt-get systemctl env` 确认。

## 3. 配置 SSH

把私钥放到宿主机 `secrets/`，例如：

```bash
chmod 700 secrets
chmod 600 secrets/primary_ed25519
```

把目标主机公钥加入 `secrets/known_hosts`。应通过可信渠道核对主机指纹，不要只依赖未经核验的首次连接：

```bash
ssh-keyscan -H 服务器IP >> secrets/known_hosts
chmod 600 secrets/known_hosts
```

`allow_unknown_host_key` 默认是 `false`，生产环境不要改为 `true`。

## 4. 配置服务器清单

编辑 `config/servers.yaml`：

- `host / port / username`：SSH 目标。
- `auth`：只写私钥文件名或环境变量名，不写密码值。
- `managed_root`：Compose 受管根目录。
- `allowed_operations`：该服务器允许的能力。
- `allowed_services`：可查询/重启的 systemd 服务。
- `allowed_bind_roots`：Compose 可以绑定的绝对宿主机目录。

Agent 可以读取清单以理解服务器别名，但看不到 `secrets/` 和 `.ops-runner.env`。

## 5. 启动

```bash
bash scripts/sync_fork.sh
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

API 默认只绑定 `127.0.0.1:8000`。需要远程访问时，应放在带 TLS 和身份验证的反向代理后，不要直接把 8000 端口暴露到公网。

## 6. 对话示例

```text
列出我配置的服务器。
巡检 primary，告诉我磁盘、内存和 Docker 有没有异常。
在 primary 上执行 apt update。
把这份 Compose 部署成项目 demo。
查看 primary 上 nginx 的状态。
重启 primary 上的 nginx。
```

只读操作会进入队列并由 Runner 自动执行。变更操作返回类似：

```text
运维请求已创建：op-0123456789abcdef
批准命令：bash scripts/approve.sh op-0123456789abcdef approve
```

先检查请求内容：

```bash
cat data/ops-requests/op-0123456789abcdef.json
```

批准或拒绝：

```bash
bash scripts/approve.sh op-0123456789abcdef approve
bash scripts/approve.sh op-0123456789abcdef deny "当前不允许重启"
```

然后在聊天中说“查询刚才那个运维请求”，或调用：

```bash
curl -H "X-Agenelf-Token: $AGENELF_API_TOKEN" \
  http://127.0.0.1:8000/operations/op-0123456789abcdef
```

## Compose 安全红线

以下配置会在 Agent 和 Runner 两层被拒绝：

- `privileged: true`
- `network_mode: host`、`pid: host`、`ipc: host`、`userns_mode: host`
- `cap_add: [ALL]`
- `devices` 映射
- `/var/run/docker.sock` 挂载
- 宿主机根目录 `/` 挂载
- 不在 `allowed_bind_roots` 中的绝对路径挂载

Compose 中不接受明文 `.env` 内容。敏感环境变量应预先放在服务器受控目录或接入后续 Secret 能力。

## 审计与故障排查

- Agent 请求：`data/ops-requests/`
- 人类决定：`data/auth-decisions/`
- Runner 结果：`data/ops-results/`
- Agent 操作日志：`logs/operations.log`
- Runner 日志：`logs/ops-runner.log`
- 裁决日志：`logs/audit.log`

```bash
docker compose logs -f agenelf
docker compose logs -f ops-runner
```

Runner 失败不会回退到任意 Shell；它会输出 `failed` 结果和具体退出码。修正服务器配置、权限或 Compose 后，应重新提交一份新请求。
