# Agenelf 个性化数据与成长连续性设计

## 目标

Agenelf 必须同时满足三个要求：

1. 通用代码能够持续升级、自主迭代并被所有使用者复用；
2. 每位主人的身份、偏好、服务器、凭据和记忆保持私有，不进入 Git，也不被升级覆盖；
3. Agent 的反思、教训和改进意向能够跨会话持续，但不能退化成不可审计的“人格宣称”。

因此仓库采用：

```text
app/        = 通用代码与能力
local/      = 当前主人的私有数据、记忆和成长连续性
scripts/    = 宿主机可信控制面
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
| `self/state.json` | 连续性 ID、原则和沉淀游标 | 读写 | 不可见 | 是，摘要 |
| `self/reflections.json` | 观察、教训和证据 | 读写 | 不可见 | 是，最新摘要 |
| `self/intentions.json` | 改进意向与生命周期 | 读写 | 不可见 | 是，开放意向摘要 |

`local/self/` 的详细契约见 [SELF_DEVELOPMENT.md](SELF_DEVELOPMENT.md)。

## 明确加载而不是扫描整个 local

Agent 不会递归读取整个 `local/`。它只通过受保护模块访问：

- `profile.yaml`
- `preferences.yaml`
- `context/` 下允许的文本/YAML/JSON 文件
- `servers.yaml` 的服务器别名和允许操作摘要
- `memory/memory.json`
- `self/state.json`
- `self/reflections.json`
- `self/intentions.json`

`secrets/` 从未挂入 Agent 容器，因此即使模型要求读取也没有文件系统权限。

## Docker 选择性挂载

Agent：

```text
profile/preferences/context/servers   read-only
memory/                               read-write
self/                                 read-write
secrets/                              not mounted
```

Ops Runner：

```text
servers.yaml                          read-only
secrets/                              read-only
profile/preferences/context/memory/self  not mounted
```

因此：

- Agent 能理解主人并保持记忆和成长连续性，但拿不到 SSH 凭据；
- Runner 能连接服务器，但看不到主人画像、聊天记忆和自我反思；
- 两个进程都不能把各自权限拼成一个任意特权执行面。

## 隐私防护

`core/privacy.py` 对画像、补充资料、记忆和成长记录执行两层脱敏：

1. 键名脱敏：`password`、`token`、`api_key`、`private_key` 等字段值直接替换为 `[REDACTED]`；
2. 文本模式脱敏：常见 `sk-...`、GitHub Token、AWS Key、Bearer Token 和键值形式密码被替换。

脱敏是最后一道防护，不应代替正确的数据放置方式：真正凭据仍必须进入 `local/secrets/` 或 `.ops-runner.env`。

## 长期记忆

长期记忆固定写入：

```text
local/memory/memory.json
```

具备：

- 原子写入；
- 凭据脱敏；
- 连续重复去重；
- 最大条目限制；
- 提示词条数与字符上限。

## 成长连续性

成长状态固定写入：

```text
local/self/
```

具备：

- 稳定连续性 ID；
- 自动反思对话游标；
- 反思记录数量上限；
- 意向去重；
- P0/P1/P2/P3 优先级；
- 操作性承诺度；
- 计划、执行、待晋升、阻塞和完成状态；
- 以宿主机 `promotion-history` 作为完成证据；
- `consciousness_claim: false`。

自动反思可以主动提出目标，但默认 `auto_pursue: false`，不会自行修改代码。

## 初始化与迁移

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

并创建：

```text
local/self/state.json
local/self/reflections.json
local/self/intentions.json
```

初始化和重复执行都不会覆盖已经存在的主人文件或成长记录。

## 热重载与查看

主人修改画像、兴趣或补充资料后：

```text
/local-reload
```

查看成长状态：

```text
/mind
/reflect
/intentions
```

宿主机查看文件是否就绪但不打印内容：

```bash
make local
make mind
```

## 安全关键边界

以下模块不能由 Agenelf 的自主补丁修改：

```text
core/configuration.py
core/local_context.py
core/privacy.py
core/memory.py
core/self_development.py
skills/local_context.py
skills/self_development.py
```

宿主机 `gate_check.sh` 会将这些文件与 `app-fork` 基线逐字节比较，并拒绝候选代码新增对 `local/profile`、`preferences`、`servers`、`memory`、`self` 或 `secrets` 的直接写入逻辑。
