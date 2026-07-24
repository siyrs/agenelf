# Agenelf — 自我迭代型个人智能体

> `agenelf = agent + self`：一个能够理解自然语言、调用真实能力、保存个性化记忆，并在安全边界内自我迭代的数字助手。

Agenelf 现在有三个彼此分离但可组合的层次：

1. **通用能力代码 `app/`**：所有使用者共享的 Agent、技能、权限、运维和自主迭代逻辑。
2. **主人私有数据 `local/`**：主人画像、兴趣、补充资料、服务器策略、凭据和长期记忆。
3. **可信执行控制面**：宿主机审批脚本和不调用 LLM 的确定性 `ops-runner`。

## 核心原则

- 通用功能只进入 `app/`，升级代码不会覆盖 `local/`。
- LLM 负责理解和规划，不直接获得 SSH 私钥、密码、Token 或 Docker Socket。
- 只读操作可自动执行；系统变更必须绑定目标与参数后由人类一次性批准。
- 安全红线不能被聊天指令、长期记忆、`local/` 文件或自我迭代绕过。
- 只有可信 Runner 返回成功证据后，Agent 才能声称任务完成。

## 架构

```text
                                local/profile + preferences + context
                                              │ 只读、脱敏
                                              ▼
用户 ── CLI / HTTP ─────▶ Agent + Capability Catalog ─────▶ structured requests
                              │         │                         │
                              │         └─ local/memory (rw)      ├─ read: 自动
                              │                                   └─ change: 人类批准
                              │                                            │
                              │                                            ▼
                              │                                    deterministic ops-runner
                              │                                    local/servers + secrets
                              │                                            │
                              ▼                                            ▼
                         app-tmp sandbox                              trusted results
                              │
                        tests + gate
                              │
                        host promotion
```

详细文档：

- [个性化数据与 local 目录](docs/PERSONALIZATION.md)
- [总体架构](docs/ARCHITECTURE.md)
- [服务器运维](docs/SERVER_OPERATIONS.md)
- [受控自主迭代](docs/AUTONOMY.md)

## 快速开始

```bash
git clone https://github.com/siyrs/agenelf.git
cd agenelf
make init
```

`make init` 会：

- 创建 `.env` 与 `.ops-runner.env`；
- 创建 `local/profile.yaml`、`preferences.yaml`、`servers.yaml`；
- 创建 `local/context/`、`memory/`、`secrets/`；
- 自动迁移旧版 `app/persona`、`config/servers.yaml`、`secrets/` 和旧记忆；
- 创建全部运行队列目录。

随后编辑：

```text
.env
.ops-runner.env
local/profile.yaml
local/preferences.yaml
local/servers.yaml
local/context/
local/secrets/
```

启动：

```bash
make start
make status
make chat
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## local 个性化数据层

```text
local/
├── profile.yaml          # 主人基本信息、称呼、时区、沟通方式
├── preferences.yaml      # 爱好、兴趣、工作和交付偏好
├── context/              # 主人补充的 Markdown/TXT/YAML/JSON 资料
├── servers.yaml          # 服务器别名、允许操作和安全策略
├── secrets/              # SSH 私钥与 known_hosts，仅 Runner 可见
└── memory/memory.json    # Agent 写入的脱敏长期记忆
```

Docker 使用选择性挂载：

- Agent 可读取画像、偏好、补充资料和服务器别名，可读写 `local/memory/`；
- Agent **看不到** `local/secrets/`；
- `ops-runner` 只读取 `local/servers.yaml` 和 `local/secrets/`，看不到主人画像和记忆。

实际个性化文件均被 Git 忽略，仓库只提交模板和说明。

## 个性化与记忆命令

| 命令 | 作用 |
|---|---|
| `/local` | 查看 local 加载状态、服务器别名、警告和记忆统计 |
| `/local-reload` | 主人编辑 local 文件后热重载 |
| `/remember fact <内容>` | 保存一条脱敏事实 |
| `/remember preference <内容>` | 保存一条脱敏偏好 |
| `/recall <关键词>` | 检索主人长期记忆 |
| `/memory` | 查看将进入提示词的脱敏记忆摘要 |

记忆写入会自动：

- 隐去常见 API Key、Token、密码和私钥内容；
- 合并连续重复内容；
- 按 `memory_max_entries` 限制总量；
- 保存在可写的 `local/memory/`，不再写入只读 `app-fork/`。

## HTTP 个性化 API

```text
GET  /local/status
POST /local/reload
POST /memory
GET  /memory/search?q=关键词&limit=5
```

所有接口除 `/health` 外均建议使用：

```text
X-Agenelf-Token: <AGENELF_API_TOKEN>
```

## 服务器运维能力

| 工具意图 | 风险 | 是否需批准 |
|---|---:|---:|
| 服务器巡检 | read | 否 |
| 查看 Docker 容器 | read | 否 |
| 查询 systemd 服务 | read | 否 |
| `apt update` | change | 是 |
| Compose 部署 | change | 是 |
| 重启允许清单中的服务 | change | 是 |
| 安装 Docker | privileged | 是 |

变更请求产生后，在宿主机检查并裁决：

```bash
cat data/ops-requests/op-xxxxxxxxxxxxxxxx.json
bash scripts/approve.sh op-xxxxxxxxxxxxxxxx approve
```

批准文件与请求载荷指纹绑定，不能换命令、换服务器或换参数复用。

## 自主迭代安全边界

- `app/`：通用代码真理之源，不挂入 Agent 容器。
- `app-fork/`：当前运行副本，只读。
- `app-tmp/`：自主迭代暂存区。
- `local/`：主人数据，不会被代码晋升覆盖。
- `scripts/`：宿主机控制面，只读。
- Agent 不挂载 Docker Socket、Runner 环境文件或 `local/secrets/`。
- 隐私、权限、运维执行和个性化边界模块均属于安全关键代码，自主补丁不能修改。

## 开发与测试

```bash
cd app
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

GitHub Actions 会执行：

- 全部 Python 编译检查；
- 完整单元测试；
- Shell 脚本语法检查；
- Docker 个性化挂载隔离测试；
- local 初始化迁移测试；
- 凭据脱敏和记忆容量测试；
- 自主修改安全关键模块的拒绝测试。

## 后续能力方向

- 日志诊断与根因分析
- 代码 Bug 修复和仓库变更
- 测试、冒烟与验收编排
- 发布、流量切换和回滚
- 数据库备份、恢复与迁移
- 受控 Secret 生命周期管理

这些能力将继续作为独立能力域接入，而不是退化成任意 Shell。
