# 服务器 `.env` 密钥席位管理

Agenelf 提供两种互补的管理入口：

1. **主人聊天明文模式**：主人在 Agenelf 聊天中明确要求时，可以直接查看完整 Key、输入新 Key，并让 Agenelf 立即更新服务器；
2. **本地 Secret Console 模式**：完整值只进入本地 TTY，适合不希望明文进入模型上下文的场景。

两种模式共用稳定席位 ID、固定目标清单、远程原子替换、Compose 校验、健康检查和失败回滚。

## 主人聊天明文模式

### 预期交互

```text
主人：把 relay-zhipu 配置的几个 Key 明文列出来
Agenelf：
zhipu-a = ...完整值...
zhipu-b = ...完整值...
zhipu-c = ...完整值...
zhipu-d = ...完整值...

主人：删除 zhipu-b，把 zhipu-c 改成 new-key-value，其他不动
Agenelf：
已更新：zhipu-b 删除，zhipu-c 替换，zhipu-a/zhipu-d 保持不变；
配置校验、服务重载和健康检查通过。
```

主人明确要求明文时，Agent 必须调用受控工具，不再以通用安全提示拒绝：

- `secret_env_targets`：列出目标和稳定席位 ID；
- `secret_env_read_plaintext`：读取一个目标的全部明文，或只读取一个席位；
- `secret_env_apply_plaintext`：按聊天中的明确指令直接更新、删除或保持席位。

`secret_env_apply_plaintext` 支持只列出发生变化的席位。未列出的席位自动转换为 `keep`，例如仅提交：

```json
{
  "env_target": "relay-zhipu",
  "confirm_target": "relay-zhipu",
  "changes": [
    { "seat_id": "zhipu-b", "action": "delete" },
    { "seat_id": "zhipu-c", "action": "set", "value": "new-key-value" }
  ]
}
```

真正执行时仍会生成包含 A/B/C/D 全部席位的完整 `keep/delete/set` 计划，避免数组移位或误删其它 Key。

### 明文的保存范围

聊天明文模式的目的就是让模型和当前聊天能够看到完整值，因此：

- 完整旧 Key 会进入当前工具结果和当前模型回合；
- 主人在聊天中输入的新 Key 会进入当前聊天和当前模型回合；
- 最终回复可以按主人要求完整展示；
- 当前聊天界面及敏感会话账本记录可能保留这些文字。

Agenelf 会把这一轮标记为 `sensitive`，并执行以下隔离：

- 工具生命周期事件只记录 `[SENSITIVE TOOL RESULT OMITTED]`；
- 不把该轮写入长期主人记忆；
- 后续模型历史回放会跳过敏感用户消息和敏感助手回复；
- Broker 审计只记录目标、席位动作和结果，不记录 Key；
- 修改结果只返回动作和修改前后短指纹，不回显新 Key。

这不是“明文永不落地”模式。完全不希望明文进入聊天或模型时，应使用下文的本地 Secret Console。

## 聊天明文模式的信任边界

```text
主人聊天
   │
   │ 固定 Tool + 内部 Token
   ▼
Agenelf Agent ── Compose 内网 ── Secret Chat Broker
                                  │
                                  │ SSH + 固定 Python 脚本
                                  ▼
                           服务器 .env / .env.secrets
```

关键边界：

- `secret-chat-broker` 不映射任何宿主机端口，只 `expose: 8097` 到 Compose 内网；
- Broker 要求与 Agenelf 相同的 `X-Agenelf-Token`；
- Broker 才挂载 `local/secrets/`、`local/servers.yaml` 和 `local/env-secrets.yaml`；
- Agent 本身仍不挂载 SSH 私钥、SSH 密码环境或 Secret target 配置；
- Broker 只允许 `local/env-secrets.yaml` 中声明的服务器、文件和席位；
- 不提供任意路径读取、任意环境变量读取或任意远程 Shell；
- Key 通过 SSH stdin 写入短期 stage 文件，不进入 SSH argv；
- Broker 使用只读根文件系统、`cap_drop: ALL`、`no-new-privileges`，且没有 Docker Socket、Approval key、主人 Memory 或 Self 目录。

聊天明文模式默认启用。需要关闭时，在 `.env` 中设置：

```dotenv
AGENELF_CHAT_PLAINTEXT_SECRETS=false
```

关闭后相关聊天工具不会注册，原本的 Secret Console 仍可使用。

## 配置目标和稳定席位

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
- `seats` 的键是永久稳定的席位 ID；
- 同一目标不能把两个席位映射到同一个环境变量；
- `local/env-secrets.yaml` 只保存目标元数据，不保存完整 Key。

目标服务可以拆分普通配置与秘密配置：

```yaml
services:
  new-api:
    env_file:
      - .env
      - .env.secrets
```

## 聊天中的推荐说法

读取全部明文：

```text
把 relay-zhipu 的所有席位和完整 Key 明文列出来。
```

只读取一个：

```text
显示 relay-zhipu 的 zhipu-b 完整明文。
```

删除一个，其它保持：

```text
删除 relay-zhipu 的 zhipu-b，其他席位保持不动，并更新服务器。
```

删除和替换同时执行：

```text
删除 relay-zhipu 的 zhipu-b，把 zhipu-c 改成 <完整新 Key>，其他不动，直接更新并检查服务。
```

模型不得编造新 Key。执行 `set` 时必须使用主人当前消息中提供的完整值。

## 本地 Secret Console 模式

列出目标：

```bash
make secret ARGS='targets'
```

查看脱敏清单：

```bash
make secret ARGS='list relay-zhipu'
```

在本地 TTY 查看单个完整值：

```bash
make secret ARGS='reveal relay-zhipu zhipu-b'
```

交互式修改：

```bash
make secret ARGS='patch relay-zhipu'
```

每个席位选择：

- `k`：保持；
- `d`：删除；
- `u`：更新，并通过隐藏输入输入两次新 Key。

Console 模式会创建精确绑定的 `op-...` 请求，再由主人审批：

```bash
make approve REQ=op-xxxxxxxxxxxxxxxx
make secret ARGS='status op-xxxxxxxxxxxxxxxx'
```

完整值只存在于本地 TTY 和 `0600` staging，不进入 Agent 或模型上下文。

## 原子写入和回滚

聊天 Broker 和 Secret Ops Runner 都复用同一套固定远程脚本。脚本不会 `source .env`，也不会让 Shell 解释 Key：

```text
flock
→ 读取并验证唯一受管环境变量
→ 核对修改前 inventory hash 和逐席位 SHA-256
→ 创建短期 0600 备份
→ 同目录写入临时文件
→ flush + fsync
→ chmod 600
→ os.replace 原子替换
→ fsync 目录
→ Compose config --quiet
→ Compose up -d --remove-orphans
→ 可选容器健康检查
→ 成功后删除短期备份
```

如果配置校验、重载或健康检查失败，会恢复修改前文件并重新部署旧配置。只有自动回滚本身失败时，服务器才保留一个 `0600` 恢复备份，并返回恢复路径。

## 并发保护

每次修改都会先读取当前清单，并把以下数据绑定到变更包：

- 整体 inventory hash；
- 每个稳定席位当前完整 SHA-256 指纹；
- 每个席位最终的 `keep/delete/set` 动作。

真正写入时会再次检查。若受管席位在主人查看后被其他程序修改，本次操作会拒绝覆盖。未受管的普通 `.env` 项变化不会造成误阻塞。

## 审计原则

普通审计、事件和修改结果保存：

- 目标和席位 ID；
- `keep/delete/set` 动作；
- 修改前后短指纹；
- inventory hash；
- 配置校验、重载、健康检查和回滚状态；
- 只有回滚失败时才保存恢复备份路径。

不保存：

- 完整 Key；
- Secret stage 内容；
- 远程脚本原始 stdout/stderr；
- SSH 密码、私钥或私钥口令。
