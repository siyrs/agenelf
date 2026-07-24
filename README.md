# Agenelf — 自我进化型个人智能体

> **agenelf = agent + self**——巧的是读起来还藏着一只 elf（小精灵）🧝：
> 一个住在你电脑里、以你为原型、会自己长大的数字分身。

一个以你为"数字原型"的智能体项目：它读取你的画像（persona），拥有可热插拔的技能系统，
能够**为自己编写新技能**，甚至**在 Git 沙盒中修改自身核心代码**——测试通过则合并，失败则自动回滚。

## 架构

```
                ┌────────────────────────────────────────────┐
                │                 cli.py（对话入口）          │
                └──────────────────┬─────────────────────────┘
                                   │
                        ┌──────────▼──────────┐
                        │   core/agent.py     │  至多 8 轮 tool-call 主循环
                        │   （Agent 大脑）     │
                        └──┬───┬───┬───┬──────┘
            ┌──────────────┘   │   │   └──────────────┐
   ┌────────▼───────┐ ┌────────▼───▼┐ ┌───────────────▼┐
   │ core/llm.py    │ │core/registry│ │ core/memory.py │
   │ LLMClient /    │ │ 技能注册表   │ │ 持久化记忆      │
   │ MockLLM        │ │（热插拔）    │ │（JSON 落盘）   │
   └───────┬────────┘ └──┬──────┬──┘ └────────────────┘
           │             │      │
   OpenAI 兼容 API   ┌───▼──┐ ┌─▼───────────┐
   （Kimi/DS/GPT…）  │skills│ │evolution/   │
                     │内置+自写│ │engine.py   │
                     └──────┘ │自我进化引擎 │
                              └─────────────┘
```

## 快速开始

**方式一：本地开发模式**
```bash
cd app && pip install -r requirements.txt

# 无 API Key？直接 mock 模式体验完整流程
python cli.py --mock

# 接入真实模型（OpenAI 兼容端点，默认示例为 Kimi）
export OPENAI_API_KEY="sk-..."   # 编辑 app/config.yaml 改 base_url / model
python cli.py
```

**方式二：Docker 安全运行机制（推荐，完整自迭代管道）**
```bash
cp .env.example .env            # 填入你的 OPENAI_API_KEY
bash scripts/sync_fork.sh       # app/ → app-fork/（首次启动）
docker compose up -d --build    # 启动 agent（HTTP API :8000）

bash scripts/chat.sh            # 入口1：CLI 对话
curl -X POST localhost:8000/chat -H 'Content-Type: application/json' \
     -d '{"message":"你好"}'     # 入口2：HTTP API

nohup bash scripts/watcher.sh & # 宿主机守护：自动执行晋升（可选，也可人工跑 promote.sh）
```

## 让它成为"你"

编辑 `persona/persona.yaml`：姓名、角色、技能、沟通风格、偏好、价值观——
这是 Agent 的人格来源，会注入每一轮对话的系统提示。随着使用，`memory_store/memory.json`
会持续积累它观察到的关于你的事实与偏好（fact / preference / episode 三类记忆）。

## 对话内命令

| 命令 | 作用 |
|---|---|
| `/skills` | 列出已加载技能与工具 |
| `/newskill <描述>` | **让 Agent 为自己编写一个新技能**并热加载 |
| `/reload <名称>` | 热重载某个技能 |
| `/memory` | 查看长期记忆 |
| `/evolve <目标>` | **触发核心进化**：修改自身源码 → 测试 → 合并/回滚 |
| `/quit` | 退出 |

## 内置技能

| 技能 | 能力 |
|---|---|
| `code_writer` | 写代码文件（限制项目目录内）、子进程运行 Python（30s 超时） |
| `ai_tools` | 通用 AI 能力：嵌套调用 LLM 提问、总结 |
| `server_ops` | 运维服务器：白名单只读 shell（ls/ps/df/ss 等）、其他命令需 confirm、端口检测、磁盘状态 |
| `task_handler` | 处理简单需求：待办清单、笔记存取（落盘 memory_store/） |

## 技能开发指南（也是 Agent 自写技能的协议）

在 `skills/` 下新建 `.py` 文件，定义三件套：

```python
SKILL_META = {"name": "my_skill", "description": "给 LLM 看的中文描述", "version": "0.1.0"}

TOOLS = [{  # OpenAI function-calling schema
    "type": "function",
    "function": {"name": "my_tool", "description": "...", "parameters": {...}},
}]

def execute(tool_name: str, args: dict) -> str:
    ...  # 内部捕获所有异常，返回字符串
```

保存后 `/reload my_skill` 即可使用；`/newskill` 命令会让 LLM 按此协议自动生成、
语法校验、沙盒导入验证后才注册——失败不会污染注册表。

## 自我进化机制与安全边界（Docker 安全运行机制）

Agenelf 的自迭代采用**三权分立**管道：

```
 agent（修改权）          脚本（否决权）            人类/宿主机（执行权）
 ┌─────────────┐   触发   ┌──────────────┐  READY  ┌─────────────────┐
 │ app-tmp/     │ ──────▶ │ gate_check.sh │ ──────▶ │ promote.sh       │
 │ 改代码+自测  │ 只读挂载 │ 底线/限值/测试 │         │ 备份→更新app→重启 │
 └─────────────┘          └──────────────┘         └─────────────────┘
        │                                                   │
        └──── app/（真理之源）永不挂进容器；docker.sock 不挂载 ────┘
```

### 目录职责
| 目录 | 容器挂载 | 职责 |
|---|---|---|
| `app/` | **不挂载** | 代码真理之源，只有 promote.sh 能更新 |
| `app-fork/` | 只读 | 容器实际运行的副本（sync_fork.sh 生成） |
| `app-tmp/` | 可写 | agent 自我迭代的唯一代码修改区 |
| `scripts/` | 只读 | 底线脚本与规范，**agent 只能触发，只有人类能改** |
| `app-space/` | 可写 | agent 的能力资产：skills / mcp / plugins |
| `workspace/` | 可写 | 给 agent 指派的任务保存目录（tasks/） |
| `data/ logs/` | 可写 | 晋升/授权请求、备份、审计与进化日志 |

### 自迭代流程
1. `docker compose up` → sync_fork（app→app-fork）→ 启动 API（运行 app-fork 代码）
2. agent 用 `evolution_ops` 技能：`evolution_begin` → `evolution_write_file` → `evolution_run_tests`
3. `evolution_request_promotion` 触发 `scripts/gate_check.sh`：
   危险模式扫描 → 受保护路径写入意图 → 规模限值（≤10 文件 / ≤500 行）→ 完整测试
4. 通过 → 写 READY 到 `data/promote-requests/`；宿主机 `watcher.sh`（或手动）执行 `promote.sh`
5. promote：校验 → 备份 app/ → app-tmp→app → 刷新 app-fork → `docker compose restart`；失败自动从备份回滚

### 运维高危命令人类授权（server_ops 沙盒）

shell 命令三级管控（`app/core/permissions.py`）：

| 级别 | 例子 | 执行条件 |
|---|---|---|
| 白名单 | `ls` `ps` `df` `ss` `curl -I` `systemctl status` | 直接执行 |
| 普通 | `mkdir`、写 /tmp | agent 侧 confirm 确认 |
| **高危** | `rm` `dd` `kill` `chmod` `systemctl restart` `pip install` | **必须人类授权** |

高危流程：agent 触发 `rm` 等高危命令 → 拦截并生成 `data/auth-requests/<ID>.json` →
人类在宿主机执行 `bash scripts/approve.sh <ID> approve` → agent 带授权 ID 重试 →
**一次性核销**（5 分钟过期，pending 超 10 个拒绝新建防轰炸）→ 执行。
全程写 `logs/audit.log` 审计日志（拦截/批准/核销/执行四段留痕）。

### GitHub 自动化

```bash
bash scripts/github_setup.sh git@github.com:you/agenelf.git   # 配置远程
bash scripts/github_backup.sh "里程碑备份"                     # 提交 app/+scripts/ → 打 backup/<ts> 标签 → 推送
bash scripts/github_release.sh 0.2.0                          # 注解标签 v0.2.0（附进化日志摘要）+ 推送 + gh release
# .env 中设 GITHUB_AUTO_BACKUP=1 后，每次晋升成功自动备份推送
```

日常开发完成后的安全同步（包含暂存检查、远端合并、推送和 HEAD 一致性校验）：

```bash
bash shared/scripts/git-sync.sh "feat: 简述本次改动"
```

### 交互入口 ×2
- **CLI**：`bash scripts/chat.sh`（compose exec 进容器对话）
- **HTTP API**：`POST /chat`、`GET /health`、`GET /evolution/status`（默认 :8000）

### 紧急制动
`docker compose stop` → 从 `data/app-backups/<时间戳>.tar.gz` 恢复 app/ → `bash scripts/sync_fork.sh` → 重启。
底线全文见 `scripts/SAFETY.md`，迭代规范见 `scripts/USAGE.md`。

<details><summary>开发模式：git 沙盒进化引擎（app/evolution/engine.py）</summary>

`/evolve <目标>` 走开发态轻量流程：切 `evolve/<时间戳>` 分支 → LLM 整文件覆盖式修改 →
禁止动 engine 自身/config/persona，单次 ≤3 文件 → 测试通过合并回 main，失败自动回滚。
适用于本地开发；容器内请使用上面的 evolution_ops 管道。

</details>

## 运行测试

```bash
python tests/test_registry.py    # 技能协议（19 项）
python tests/test_agent_mock.py  # mock 模式完整回路
python tests/test_evolution.py   # 进化引擎：成功合并 / 失败回滚 / 安全约束
```
（以上命令在 `app/` 目录下执行；也可 `cd app && python -m unittest discover -s tests` 一次跑完）

## 运维与验收沉淀

发布前检查项、Windows 本地开发兼容性、Docker 运行验收和已知边界见
[`docs/复审与运维验证.md`](docs/复审与运维验证.md)。其中的“最小发布验收”是每次
修改 `app/`、`scripts/` 或容器编排后应保留的证据清单。

## 目录结构

```
├── app/                     # ★ 代码真理之源（容器不挂载，只有 promote.sh 能更新）
│   ├── cli.py               #   对话入口
│   ├── api.py               #   HTTP API 入口（FastAPI）
│   ├── config.yaml          #   LLM 配置（OpenAI 兼容）
│   ├── core/                #   内核：agent loop / LLM / 技能注册 / 记忆 / 系统提示
│   ├── skills/              #   技能插件（内置 5 个 + Agent 自写）
│   ├── evolution/           #   开发态 git 进化引擎
│   ├── persona/persona.yaml #   你的数字画像（人格来源）
│   └── tests/               #   45+ 项测试
├── app-fork/                # 运行时副本（sync_fork.sh 生成，容器只读挂载）
├── app-tmp/                 # 自迭代暂存区（容器内唯一可写代码区）
├── scripts/                 # ★ 安全底线（容器只读挂载，agent 只能触发）
│   ├── gate_check.sh        #   底线检查门：危险模式/受保护路径/规模限值/测试
│   ├── promote.sh           #   晋升：备份→更新app→刷新fork→重启（宿主机执行）
│   ├── watcher.sh           #   宿主机守护：自动执行晋升
│   ├── sync_fork.sh / chat.sh
│   └── SAFETY.md / USAGE.md #   安全底线宪法 + agent 使用规范
├── app-space/               # agent 能力资产：skills / mcp / plugins
├── workspace/               # 给 agent 指派的任务保存目录（tasks/）
├── data/                    # 晋升/授权请求、app 备份、会话状态
├── logs/                    # evolution.log、audit.log、github.log
├── docker-compose.yml       # 容器描述（挂载=安全边界）
├── Dockerfile  .env.example  Makefile
└── README.md
```

## 路线图建议

- [ ] 记忆升级为向量检索（chroma / sqlite-vec）
- [ ] 进化引擎接入代码 diff 模式 + 人工确认闸门
- [ ] server_ops 增加 SSH 远程运维（paramiko）
- [ ] Web 界面 / 接入 IM（飞书、Telegram bot）
- [ ] 定时任务：让 Agent 自主安排自检与进化
