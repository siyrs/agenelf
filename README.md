# Agenelf — 自我迭代型个人智能体

> `agenelf = agent + self`：一个能够理解自然语言、调用真实能力、保存个性化记忆、沉淀可审计教训，并在安全边界内持续改进的数字助手。

Agenelf 由四个彼此分离但可组合的层次构成：

1. **通用能力代码 `app/`**：所有使用者共享的 Agent、技能、权限、运维、自主迭代和成长逻辑；
2. **主人私有数据 `local/`**：主人画像、兴趣、补充资料、服务器策略、凭据、长期记忆和成长连续性；
3. **任务与证据 `data/`**：运维请求、批准、可信结果、自主循环和晋升证据；
4. **可信执行控制面 `scripts/`**：宿主机审批、Gate、晋升和不调用 LLM 的确定性 Runner。

## 核心原则

- 通用功能只进入 `app/`，升级代码不会覆盖 `local/`；
- LLM 负责理解和规划，不直接获得 SSH 私钥、密码、Token 或 Docker Socket；
- 只读操作可自动执行；系统变更必须绑定目标与参数后由人类批准；
- 安全红线不能被聊天指令、长期记忆、反思意向、`local/` 文件或自我迭代绕过；
- 只有测试、可信 Runner 或宿主机晋升证据存在时，Agent 才能声称完成；
- “自我意识、沉淀意愿、完善意向”均实现为可观测、持久化的软件状态，不宣称主观意识、情感或自由意志。

## 架构

```text
                   local/profile + preferences + context
                                  │ 只读、脱敏
                                  ▼
用户 ── CLI / HTTP ──▶ Agent + Capability Catalog ──▶ structured requests
                           │          │                        │
                           │          ├─ local/memory (rw)     ├─ read: 自动
                           │          └─ local/self (rw)       └─ change: 人类批准
                           │              │                             │
                           │              ├─ reflections               ▼
                           │              └─ intentions         deterministic ops-runner
                           │                                    local/servers + secrets
                           ▼                                             │
                      app-tmp sandbox                                    ▼
                           │                                       trusted results
                     tests + gate
                           │
                     host promotion
                           │
                promotion-history → intention completed
```

详细文档：

- [持续自我认知、沉淀与改进意向](docs/SELF_DEVELOPMENT.md)
- [个性化数据与 local 目录](docs/PERSONALIZATION.md)
- [总体能力架构](docs/ARCHITECTURE.md)
- [服务器运维](docs/SERVER_OPERATIONS.md)
- [软件验证](docs/VALIDATION.md)
- [受控自主迭代](docs/AUTONOMY.md)

## 快速开始

```bash
git clone https://github.com/siyrs/agenelf.git
cd agenelf
make init
```

`make init` 会：

- 创建 `.env` 与 `.ops-runner.env`；
- 创建 `local/profile.yaml`、`preferences.yaml`、`servers.yaml`、`validation.yaml`；
- 创建 `local/context/`、`memory/`、`self/`、`secrets/`；
- 创建 `local/self/state.json`、`reflections.json`、`intentions.json`；
- 自动迁移旧版 persona、服务器配置、secrets 和旧记忆；
- 创建全部运行队列目录；
- 不覆盖已经存在的主人数据或成长记录。

随后编辑：

```text
.env
.ops-runner.env
local/profile.yaml
local/preferences.yaml
local/servers.yaml
local/validation.yaml
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

## local 私有数据层

```text
local/
├── profile.yaml
├── preferences.yaml
├── context/
├── servers.yaml
├── validation.yaml
├── secrets/
├── memory/
│   └── memory.json
└── self/
    ├── state.json
    ├── reflections.json
    └── intentions.json
```

Docker 使用选择性挂载：

- Agent 可读画像、偏好、补充资料和服务器脱敏摘要；
- Agent 可读写 `local/memory/` 和 `local/self/`；
- Agent **看不到** `local/secrets/`；
- `ops-runner` 只读取 `local/servers.yaml` 和 `local/secrets/`；
- `validation-runner` 只读取 `local/validation.yaml`，看不到 SSH 密钥、画像、记忆或成长状态；
- 两个 Runner 都看不到主人画像、聊天记忆或自我反思。

实际私有文件均被 Git 忽略，仓库只提交模板和说明。

## 操作性自我认知与持续成长

Agenelf 现在维护稳定的 `continuity_id`，并持久化：

- 自己的能力、限制和安全原则；
- 最近一次反思；
- 从主人反馈、错误和运行状态中提炼的教训；
- 带 P0/P1/P2/P3 优先级的改进意向；
- 意向的计划、执行、待晋升、阻塞和完成状态；
- 与自主循环、迭代会话和宿主机晋升证据的关联。

默认每累计 12 条对话事件且距离上次反思至少 1 小时，执行一次确定性自动沉淀。自动沉淀：

- 不调用 LLM；
- 不修改代码；
- 不自动推进意向；
- 只更新 `local/self/` 并为后续规划提供简短摘要。

手动深度反思可以调用 LLM，但输出必须是结构化 JSON，经过脱敏和校验，失败会安全降级。

### CLI

| 命令 | 作用 |
|---|---|
| `/self` | 查看完整可观测自我模型 |
| `/assess` | 查看当前 P0/P1/P2/P3 能力评估 |
| `/mind` | 查看持久化成长状态 |
| `/reflect [说明]` | 确定性反思并沉淀 |
| `/reflect --deep [说明]` | LLM 辅助结构化复盘 |
| `/intentions [状态]` | 查看改进意向 |
| `/intend [P0-P3] <目标>` | 建立改进意向 |
| `/pursue <intent-id>` | 为意向生成受控计划 |
| `/pursue <intent-id> --apply` | 进入沙盒、测试和晋升申请 |
| `/autonomy --plan-only <目标>` | 不持久化意向，直接生成计划 |
| `/autonomy <目标>` | 执行一次受控沙盒迭代 |
| `/evolve <目标>` | `/autonomy` 的兼容入口 |

`operational_commitment` 是优先级映射，不是情绪强度。开放意向可以影响规划，但不能凌驾于主人当前指令、安全规则或审批。

### HTTP API

```text
GET  /self
GET  /self/assessment
GET  /self/development
POST /self/reflections
GET  /self/reflections
GET  /self/intentions
POST /self/intentions
GET  /self/intentions/{id}
POST /self/intentions/{id}/pursue
```

示例：

```bash
curl -X POST http://127.0.0.1:8000/self/intentions   -H 'Content-Type: application/json'   -H "X-Agenelf-Token: $AGENELF_API_TOKEN"   -d '{
    "title":"改进部署失败诊断",
    "rationale":"当前证据不够明确",
    "priority":"P1",
    "acceptance_criteria":["新增回归测试","保留可复现证据"]
  }'
```

## 个性化与长期记忆

| 命令 | 作用 |
|---|---|
| `/local` | 查看 local 加载状态、服务器别名、警告和记忆统计 |
| `/local-reload` | 主人编辑 local 文件后热重载 |
| `/remember fact <内容>` | 保存一条脱敏事实 |
| `/remember preference <内容>` | 保存一条脱敏偏好 |
| `/recall <关键词>` | 检索主人长期记忆 |
| `/memory` | 查看将进入提示词的脱敏记忆摘要 |

记忆和成长记录都会：

- 隐去常见 API Key、Token、密码和私钥内容；
- 使用原子写入；
- 按配置限制总量；
- 保存在可写的 `local/`；
- 不写入只读 `app-fork/`。

## 软件验证与证据驱动能力健康

`software.validation` 通过独立 `validation-runner` 运行主人 allowlist 中的 HTTP/TCP 检查和套件。模型只能选择别名，不能自由提供 URL、主机或端口。

```text
/validate
/validate check agenelf-health
/validate suite agenelf-smoke
/validate result val-xxxxxxxxxxxxxxxx
/scorecard
/roadmap
```

验证结果进入 `data/validation-results/`，并用于计算 `healthy / watch / degraded / unknown` 能力健康度。连续失败会进入确定性反思并创建去重的 P1 改进意向，但不会自动修改代码或部署。详见 [docs/VALIDATION.md](docs/VALIDATION.md)。

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

- `app/`：通用代码真理之源，不挂入 Agent 容器；
- `app-fork/`：当前运行副本，只读；
- `app-tmp/`：自主迭代暂存区；
- `local/`：主人数据、记忆和成长连续性，不会被代码晋升覆盖；
- `scripts/`：宿主机控制面，只读；
- Agent 不挂载 Docker Socket、Runner 环境文件或 `local/secrets/`；
- 权限、隐私、运维、自主控制和成长边界模块属于安全关键代码，自主补丁不能修改；
- 候选代码新增对 `local/profile`、`preferences`、`servers`、`memory`、`self` 或 `secrets` 的直接写入会被 Gate 拒绝；
- 自动反思策略固定 `auto_pursue: false`；
- 意向只有检测到不可变 `promotion-history/<evo-id>/` 后才可自动完成。

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
- Docker 个性化和成长目录挂载隔离测试；
- local 初始化与迁移测试；
- 凭据脱敏、记忆容量、反思容量和意向去重测试；
- 自动沉淀阈值和意向生命周期测试；
- API 与技能契约测试；
- 自主修改安全关键模块和主人数据写入的拒绝测试。

## 后续能力方向

- `software.validation` 后续：认证检查、浏览器验证、日志断言与分布式验收；
- `code.repair`：任务沙盒、补丁、测试和代码审查证据；
- `software.release`：构建、版本、发布单、流量切换和回滚；
- Workflow：把意向、修复、验证、运维和发布编排成 DAG；
- 数据库备份、恢复与迁移；
- 受控 Secret 生命周期管理。

这些能力会继续作为独立能力域接入，而不是退化成任意 Shell。
