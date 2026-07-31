# 服务器 `.env` 密钥席位管理

## 目标

Agenelf 允许主人准确识别、删除或更新服务器上的某个密钥席位，同时满足以下约束：

- Agent 和模型永远看不到完整密钥；
- 普通聊天、HTTP API、普通 CLI、操作请求、审批文件、事件和日志中不保存完整密钥；
- 主人可以在独立的本地 Secret Console 中查看完整密钥或输入新密钥；
- 每个席位使用稳定 ID，删除中间席位不会导致其它席位重新编号；
- 修改绑定清单快照、单席位旧指纹、staging 哈希和操作请求指纹；
- 远程文件加锁、同目录临时文件、`fsync`、`chmod 600` 和原子替换；
- Compose 校验、重载或健康检查失败时自动恢复旧文件；
- 明文回滚备份仅在一次执行期间存在；只有回滚本身失败时才会以 `0600` 保留，便于人工恢复。

## 信任边界

```text
普通 Agent / Web / HTTP / 普通 CLI
        │
        │ 只能提交 inventory_env，或读取脱敏结果
        ▼
不可变 Operation Queue ── owner approval ── Secret Ops Runner
                                                │
                                                │ SSH + 固定脚本
                                                ▼
                                         服务器 .env.secrets

主人本地 Secret Console
  ├─ list：只显示 masked + fingerprint
  ├─ reveal：完整密钥仅显示在本地 TTY
  └─ patch：新密钥仅进入 0600 staging，再由审批绑定的 Runner 消费
```

`agenelf`、`legacy-agent` 和普通 `cli` 都不挂载：

- SSH 私钥目录 `local/secrets/`；
- Secret Ops staging；
- `local/env-secrets.yaml`。

只有：

- `secret-cli`：主人显式运行时，读取服务器连接秘密并写入 staging；
- `secret-ops-runner`：读取连接秘密、消费 staging、执行已审批修改。

Docker Compose 默认使用独立 Linux named volume `secret-staging`。这样即使宿主机是 Windows，也能可靠保持目录 `0700`、文件 `0600`，而不是依赖 NTFS bind mount 的权限语义。

## 配置

初始化后编辑：

```text
local/env-secrets.yaml
```

示例：

```yaml
schema_version: 1

targets:
  relay-zhipu:
    server: relay-prod
    env_file: /srv/new-api/.env.secrets
    seats:
      zhipu-a:
        env: ZHIPU_SEAT_A_API_KEY
        label: 智谱主席位
      zhipu-b:
        env: ZHIPU_SEAT_B_API_KEY
        label: 智谱备用席位 B
      zhipu-c:
        env: ZHIPU_SEAT_C_API_KEY
        label: 智谱备用席位 C
      zhipu-d:
        env: ZHIPU_SEAT_D_API_KEY
        label: 智谱备用席位 D
    reload:
      type: compose
      workdir: /srv/new-api
      compose_file: /srv/new-api/compose.yaml
      project: new-api
      health_container: new-api
```

约束：

- `server` 必须存在于 `local/servers.yaml`；
- `env_file`、`workdir` 和 `compose_file` 必须位于服务器 `managed_root` 下；
- `health_container` 必须位于服务器 `allowed_containers` 中；
- `seats` 的键是稳定席位 ID；
- 同一目标不能把两个席位映射到同一个环境变量；
- 此文件只能保存元数据，禁止写完整密钥。

目标服务使用两个 env 文件时，可采用：

```yaml
services:
  new-api:
    env_file:
      - .env
      - .env.secrets
```

普通配置写入 `.env`，API Key、Token 和密码写入 `.env.secrets`。

## 使用

列出目标：

```bash
make secret ARGS='targets'
```

查看脱敏清单：

```bash
make secret ARGS='list relay-zhipu'
```

输出包含：

- 稳定席位 ID；
- 标签与环境变量名；
- 是否存在；
- 前后少量字符构成的 masked 特征；
- SHA-256 的短指纹；
- 整体 inventory hash。

完整查看单个席位：

```bash
make secret ARGS='reveal relay-zhipu zhipu-b'
```

完整值只在本地 Secret Console 的 TTY 中显示。它不会进入 Agent、模型、operation request 或审计日志。终端自身的滚动区仍可能保留显示内容，因此查看后应关闭或清理终端。

交互式修改：

```bash
make secret ARGS='patch relay-zhipu'
```

Secret Console 会对每个席位要求一个明确决定：

- `k`：保持不变；
- `d`：删除；
- `u`：更新，随后在隐藏输入中输入两次新密钥。

例如：

```text
zhipu-a: keep
zhipu-b: delete
zhipu-c: set
zhipu-d: keep
```

确认后会创建一个 `op-...` 请求。请求只包含：

- 目标别名；
- staging 文件引用；
- staging SHA-256；
- 修改前 inventory hash；
- 操作请求自身的规范化指纹。

请求中没有完整密钥。

批准：

```text
/approve op-xxxxxxxxxxxxxxxx env-secret-patch
```

或：

```bash
make approve REQ=op-xxxxxxxxxxxxxxxx
```

查询：

```bash
make secret ARGS='status op-xxxxxxxxxxxxxxxx'
```

## 并发与防误改

真正写入前，Runner 会再次验证：

1. 操作请求指纹；
2. 审批指纹；
3. 请求 TTL；
4. staging 文件必须是非符号链接普通文件、权限为 `0600`；
5. staging 内容 SHA-256；
6. staging 必须包含全部稳定席位的 `keep/delete/set` 决定；
7. 服务器当前 inventory hash；
8. 每个席位当前完整 SHA-256 指纹。

如果主人查看清单后，其他人或程序修改了任意被管理席位，操作会失败，不会覆盖新内容。未被 Secret Ops 管理的普通 `.env` 项变化不会造成误阻塞。

## 写入和回滚

远程固定脚本不会 `source .env`，也不会通过 shell 解释密钥值。它执行：

```text
flock
→ 读取并验证唯一环境变量键
→ 生成短期 0600 备份
→ 在同一目录写入临时文件
→ flush + fsync
→ chmod 600
→ os.replace 原子替换
→ fsync 目录
→ Compose config --quiet
→ Compose up -d --remove-orphans
→ 可选容器健康检查
→ 成功后删除短期备份
```

任一校验、重载或健康检查失败时，会恢复旧文件并再次重载旧配置。回滚成功后短期备份会删除；回滚自身失败时，Runner 会保留服务器端 `0600` 备份并在结果中返回其路径，避免丢失最后的恢复副本。

## staging 生命周期

Docker Compose 模式下，新密钥短期保存在 named volume 内：

```text
secret-staging:/agenelf/local/secret-staging/secret-stage-*.json
```

直接运行 Node 程序、未使用 Compose 时，默认路径为：

```text
<AGENELF_ROOT>/local/secret-staging/secret-stage-*.json
```

目录权限为 `0700`，文件权限为 `0600`。普通 Agent 和普通 CLI 均看不到该存储。

以下情况 Runner 会删除 staging：

- 成功；
- 执行失败；
- 主人拒绝；
- 授权失效；
- 请求过期。

清理超过 24 小时的遗留 staging：

```bash
make secret ARGS='cleanup'
```

明确清空全部 staging（例如运维清理时）：

```bash
make secret ARGS='cleanup --all'
```

## 审计内容

结果和事件只保存：

- 目标和席位 ID；
- `keep/delete/set` 动作；
- 修改前后的短指纹；
- inventory hash；
- 校验、重载、健康检查和回滚状态；
- 只有回滚失败时才保存恢复备份路径，不保存备份内容。

不保存：

- 完整旧密钥；
- 完整新密钥；
- staging 内容；
- 远程脚本的原始 stdout/stderr。
