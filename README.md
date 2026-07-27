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
- [隔离代码修复](docs/CODE_REPAIR.md)
- [受控自主迭代](docs/AUTONOMY.md)
- [证据驱动自我优化](docs/SELF_OPTIMIZATION.md)
- [能力快车道](docs/APP_SPACE.md)（含测试门禁）
- [任务板](docs/TASKS.md)
- [成长报告](docs/GROWTH_REPORT.md)

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

完整斜杠命令目录（catalog 唯一来源：`app/core/interactive_prompt.py`，`/help` 可随时查看）：

| 命令 | 作用 |
|---|---|
| `/help` | 显示全部命令、用途和参数（别名 `/commands`） |
| `/doctor` | 检查运行时、Runner、队列、挂载与技能健康 |
| `/self` | 查看可观测自我模型 |
| `/assess` | 评估当前能力与缺口（P0/P1/P2/P3） |
| `/scorecard` | 查看可信能力健康评分 |
| `/roadmap` | 查看证据驱动改进路线图 |
| `/mind` | 查看持续成长状态 |
| `/reflect [--deep]` | 执行反思与沉淀（`--deep` 为 LLM 辅助结构化复盘） |
| `/intentions [status]` | 列出改进意向 |
| `/intend [P0-P3] <目标>` | 创建改进意向 |
| `/pursue <intent-id> [--apply]` | 推进指定改进意向（`--apply` 进入沙盒、测试和晋升申请） |
| `/validate [check\|suite\|result] ...` | 运行软件验证 |
| `/autonomy [--plan-only] [目标]` | 运行受控自主改进 |
| `/upgrade [status\|scopes\|upgrade-id\|目标]` | 查看或继续主人授权升级 |
| `/local` | 查看本地个性化配置状态 |
| `/local-reload` | 重新加载本地上下文 |
| `/remember <fact\|preference> <内容>` | 记录主人事实或偏好 |
| `/recall <关键词>` | 检索主人记忆 |
| `/ops [op-id]` | 查看运维请求或指定请求结果 |
| `/approvals` | 列出等待主人审批的运维与授权请求 |
| `/approve [op-id\|auth-id]` | 批准精确绑定的请求 |
| `/deny [op-id\|auth-id] [原因]` | 拒绝精确绑定的请求 |
| `/reload <技能名>` | 重载指定技能 |
| `/newskill <描述>` | 生成新技能候选 |
| `/memory` | 查看长期记忆摘要 |
| `/evolve <目标>` | 执行受控自主迭代（`/autonomy` 的兼容入口） |
| `/skills` | 列出已加载技能 |
| `/capabilities` | 列出能力域与操作风险 |
| `/quit` | 退出交互终端（别名 `/exit`） |

`operational_commitment` 是优先级映射，不是情绪强度。开放意向可以影响规划，但不能凌驾于主人当前指令、安全规则或审批。

### HTTP API

鉴权规则：`/health` 为无鉴权存活探针（只返回 `status` 与 `version`）；其余端点都要求
`X-Agenelf-Token` 头匹配 `AGENELF_API_TOKEN`。未配置 token 时 API fail-closed，
受保护端点一律返回 503（仅本地开发可显式设 `AGENELF_API_ALLOW_INSECURE=1` 绕过）。

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|:--:|
| GET | `/` | 重定向到 `/ui/`（仅重定向，不含数据） | 否 |
| GET | `/ui/*` | 内嵌 Web 控制台静态资源（`web/`，见下文托管说明） | 否 |
| GET | `/health` | 存活探针（status + version） | 否 |
| GET | `/status` | 详细运行状态（技能数、模型、能力健康、意向统计等） | 是 |
| POST | `/chat` | 对话（channel：`cli/http/web/mobile/voice`，`mobile_device` 为废弃别名；可选 `session_id` 选择会话桶，缺省为默认桶） | 是 |
| POST | `/chat/stream` | SSE 流式对话（请求体同 `/chat`，含可选 `session_id`；事件序列 `status` → `message` 增量 ×N → `done`，异常为 `error` 事件） | 是 |
| GET | `/chat/history` | 会话历史最近 N 条（`limit` 默认 50、上限 200；可选 `session_id` 按桶读取，历史按 session_id 分桶实现多会话隔离） | 是 |
| DELETE | `/chat/history` | 清空指定会话桶（可选 `session_id`；无参清默认桶，不影响其它桶） | 是 |
| GET | `/tasks` | 只读合并列出任务板（`board`，workspace/tasks/board.json）与治理引擎（`engine`，data/tasks/）任务，可 `status` 过滤 | 是 |
| GET | `/tasks/{task_id}` | 单任务完整记录（engine 任务含 events/evidence 审计历史；非法 ID 400、不存在 404） | 是 |
| GET | `/approvals` | 只读列出待审批操作/授权请求（含 hint；决策只能走 CLI `/approve` 或宿主机 `scripts/approve.sh`） | 是 |
| GET | `/capabilities` | 能力目录 | 是 |
| GET | `/validation/catalog` | 验证检查/套件目录 | 是 |
| POST | `/validation/checks/{check}` | 运行单个验证检查 | 是 |
| POST | `/validation/suites/{suite}` | 运行验证套件 | 是 |
| GET | `/validation/results/{validation_id}` | 查询验证结果 | 是 |
| GET | `/code-repair/catalog` | 代码修复仓库目录 | 是 |
| POST | `/code-repair/requests` | 提交隔离补丁修复请求 | 是 |
| GET | `/code-repair/requests/{repair_id}` | 查询修复请求结果 | 是 |
| GET | `/local/status` | 本地个性化上下文加载状态 | 是 |
| POST | `/local/reload` | 热重载 local 上下文 | 是 |
| POST | `/memory` | 记录主人事实或偏好 | 是 |
| GET | `/memory/search` | 检索主人长期记忆 | 是 |
| GET | `/self` | 可观测自我模型 | 是 |
| GET | `/self/assessment` | 能力自评估 | 是 |
| GET | `/self/capability-health` | 能力健康评分卡 | 是 |
| GET | `/self/roadmap` | 证据驱动改进路线图 | 是 |
| GET | `/self/development` | 持续成长/自我发展状态 | 是 |
| POST | `/self/reflections` | 执行反思与沉淀 | 是 |
| GET | `/self/reflections` | 列出历史反思 | 是 |
| GET | `/self/intentions` | 列出改进意向 | 是 |
| POST | `/self/intentions` | 创建改进意向 | 是 |
| GET | `/self/intentions/{intention_id}` | 查看指定改进意向 | 是 |
| POST | `/self/intentions/{intention_id}/pursue` | 推进改进意向 | 是 |
| GET | `/self/optimization` | 自我优化状态 | 是 |
| POST | `/self/optimization/apply` | 应用白名单参数微调 | 是 |
| POST | `/self/optimization/rollback` | 回滚优化参数 | 是 |
| POST | `/self/optimization/auto` | 证据驱动自动调优 | 是 |
| POST | `/autonomy/cycles` | 运行一次受控自主循环 | 是 |
| GET | `/autonomy/cycles` | 列出自主循环 | 是 |
| GET | `/autonomy/cycles/{cycle_id}` | 查询指定自主循环 | 是 |
| GET | `/operations/{operation_id}` | 查询运维操作状态 | 是 |
| GET | `/evolution/status` | 进化会话与晋升管道状态（合并 `app-tmp/promote-requests` 候选区与 `data/promote-requests` 已晋升区，每条标注 `source: candidate\|promoted`） | 是 |

`session_id`（`/chat`、`/chat/stream` 请求体与 `/chat/history` 查询参数可选）用于把对话历史按会话分桶隔离：只能包含字母、数字、点、下划线、连字符，以字母或数字开头，长度 1-64；省略或传空白时使用默认桶（与旧版单一历史行为一致）。

### 内嵌 Web 控制台托管

仓库根目录的 `web/`（`index.html` + `assets/`）由 API 进程通过 `StaticFiles` 托管在 `/ui`（`html=True`），`GET /` 直接 307 重定向到 `/ui/`。目录查找顺序：`$AGENELF_ROOT/web` → `app/../web` → `/agenelf/web`，取第一个存在的；都不存在时只记 warning，不影响 API 启动。

静态资源本身不鉴权，但控制台调用的所有数据端点仍要求 `X-Agenelf-Token`。

容器部署：compose 中 `./app` 挂在 `/agenelf/app-fork`，因此容器内预期路径为 `/agenelf/web`；需要把 `./web` 以**只读**方式挂进 Agent 容器（由部署侧在 compose 中配置 `./web:/agenelf/web:ro`）。

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

已落地并可持续运行：

- 证据驱动自我优化快车道：白名单参数微调 + **负反馈自动回滚**（优化后健康恶化即自动回退，见 [SELF_OPTIMIZATION.md](docs/SELF_OPTIMIZATION.md)）；
- 无人值守成长守护 `scripts/growth_daemon.sh`：周期性触发确定性反思、optimize_auto 与健康摘要留痕；只有触发权，代码晋升仍是人工闸门（见 [AUTONOMY.md](docs/AUTONOMY.md)）。

后续规划：

- `software.validation` 后续：认证检查、浏览器验证、日志断言与分布式验收；
- `code.repair`：任务沙盒、补丁、测试和代码审查证据；
- `software.release`：构建、版本、发布单、流量切换和回滚；
- Workflow：把意向、修复、验证、运维和发布编排成 DAG；
- 数据库备份、恢复与迁移；
- 受控 Secret 生命周期管理。

这些能力会继续作为独立能力域接入，而不是退化成任意 Shell。
