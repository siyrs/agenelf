# Agenelf 个性化数据层设计

## 目标

Agenelf 必须同时满足两个要求：

1. 通用代码能够持续升级、自主迭代并被所有使用者复用；
2. 每位主人的身份、偏好、服务器、凭据和记忆保持私有，不进入 Git，也不被升级覆盖。

因此仓库采用：

```text
app/   = 通用代码与能力
local/ = 当前主人的私有数据
```

## local 数据契约

| 路径 | 内容 | Agent 权限 | Runner 权限 | 是否进入模型 |
|---|---|---:|---:|---:|
| `profile.yaml` | 姓名、称呼、时区、角色、沟通风格 | 只读 | 不可见 | 是，脱敏后 |
| `preferences.yaml` | 爱好、兴趣、工作和交付偏好 | 只读 | 不可见 | 是，脱敏后 |
| `context/` | 补充资料 | 只读 | 不可见 | 是，限量脱敏后 |
| `servers.yaml` | 服务器连接元数据与允许操作 | 只读 | 只读 | 仅别名与允许清单 |
| `secrets/` | SSH 私钥、known_hosts | **不可见** | 只读 | 否 |
| `memory/memory.json` | 对话事实、偏好、片段 | 读写 | 不可见 | 是，限量脱敏后 |

## 明确加载而不是扫描整个 local

Agent 不会递归读取整个 `local/`。它只读取：

- `profile.yaml`
- `preferences.yaml`
- `context/` 下允许的文本/YAML/JSON 文件
- `servers.yaml` 的服务器别名和允许操作摘要
- `memory/memory.json`

`secrets/` 从未挂入 Agent 容器，因此即使模型要求读取也没有文件系统权限。

## 隐私防护

`core/privacy.py` 对画像、补充资料和记忆执行两层脱敏：

1. 键名脱敏：`password`、`token`、`api_key`、`private_key` 等字段值直接替换为 `[REDACTED]`；
2. 文本模式脱敏：常见 `sk-...`、GitHub Token、AWS Key、Bearer Token 和键值形式密码被替换。

脱敏是最后一道防护，不应代替正确的数据放置方式：真正凭据仍必须进入 `local/secrets/` 或 `.ops-runner.env`。

## 长期记忆

旧版本默认把记忆写入 `app/memory_store/`。Docker 中 `app-fork/` 是只读挂载，可能导致聊天结束时落盘失败。

新版本固定写入：

```text
local/memory/memory.json
```

同时具备：

- 原子写入；
- 凭据脱敏；
- 连续重复去重；
- 最大条目限制；
- 提示词条数与字符上限。

## 迁移

运行：

```bash
make init
```

会在目标文件不存在时迁移：

```text
app/persona/persona.yaml       -> local/profile.yaml
config/servers.yaml            -> local/servers.yaml
secrets/*                      -> local/secrets/*
app/memory_store/memory.json   -> local/memory/memory.json
```

迁移不会覆盖已经存在的 `local/` 文件。

## 热重载

主人修改画像、兴趣或补充资料后，可以：

```text
/local-reload
```

或：

```bash
curl -X POST http://127.0.0.1:8000/local/reload \
  -H "X-Agenelf-Token: $AGENELF_API_TOKEN"
```

每次普通聊天前也会重新读取安全 local 上下文，因此外部修改无需重建镜像。

## 安全关键边界

以下模块不能由 Agenelf 的自主补丁修改：

- `core/configuration.py`
- `core/local_context.py`
- `core/privacy.py`
- `core/memory.py`
- `skills/local_context.py`

宿主机 `gate_check.sh` 会将这些文件与 `app-fork` 基线逐字节比较。候选代码只要触碰其中任意文件，晋升立即被拒绝。
