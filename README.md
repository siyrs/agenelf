# Agenelf — 自我迭代型个人智能体

> `agenelf = agent + self`：一个能够理解自然语言、调用能力、保存记忆，并在安全边界内自我迭代的数字助手。

Agenelf 现在包含两个相互独立的核心方向：

1. **自我迭代**：在沙盒中修改自身、测试、等待宿主机晋升。
2. **真实任务能力**：第一个正式能力域是服务器运维，后续可接入代码修复、验证、发布并组合使用。

## 核心原则

- LLM 负责理解和规划，不直接获得服务器凭据或任意命令执行权。
- 只读操作可自动执行；所有变更绑定目标、操作和参数后由人类一次性批准。
- 安全红线永久阻断，不能被聊天指令、记忆或自我迭代绕过。
- 只有可信 Runner 返回成功证据后，Agent 才能声称任务完成。

## 架构

```text
                         ┌────────────────────────┐
用户 ── CLI / HTTP ─────▶│ Agent + recent history │
                         └───────────┬────────────┘
                                     │ tool calls
                         ┌───────────▼────────────┐
                         │ SkillRegistry           │
                         │ Capability Catalog      │
                         └──────┬───────────┬─────┘
                                │           │
                    self-evolve │           │ server.operations
                                │           ▼
                         app-tmp/      ops-requests/ (Agent 可写)
                                │           │
                                │    ┌──────┴───────────────┐
                                │    │ read: 自动            │
                                │    │ change: 人类批准      │
                                │    └──────┬───────────────┘
                                │           ▼
                                │      deterministic
                                │       ops-runner
                                │     (SSH secrets only)
                                │           │
                                │           ▼
                                │      trusted results
                                ▼
                     gate_check → promote
```

完整能力设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。服务器配置见 [docs/SERVER_OPERATIONS.md](docs/SERVER_OPERATIONS.md)。

## 快速开始

```bash
cp .env.example .env
cp .ops-runner.env.example .ops-runner.env
cp config/servers.example.yaml config/servers.yaml
# 填写 LLM、API Token 和服务器清单；SSH 私钥放到 secrets/

bash scripts/sync_fork.sh
docker compose up -d --build
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

聊天：

```bash
bash scripts/chat.sh
```

HTTP：

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -H "X-Agenelf-Token: $AGENELF_API_TOKEN" \
  -d '{"message":"巡检 primary，并告诉我 Docker 是否正常"}'
```

API 默认只监听宿主机回环地址。即使如此，仍建议配置 `AGENELF_API_TOKEN`。

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

## 对话内命令

| 命令 | 作用 |
|---|---|
| `/skills` | 查看技能和工具 |
| `/capabilities` | 查看能力域、风险和组合关系 |
| `/ops [ID]` | 查看最近运维请求或指定请求状态 |
| `/newskill <描述>` | 生成并注册新技能 |
| `/reload <名称>` | 热重载技能 |
| `/memory` | 查看长期记忆 |
| `/evolve <目标>` | 在自迭代暂存区修改核心代码并测试 |
| `/quit` | 退出 |

普通聊天会保留最近若干轮上下文，能够理解“刚才那台服务器”“查询上一个请求”等连续表达；重要交互仍会写入长期记忆。

## 技能与能力协议

传统技能协议保持不变：

```python
SKILL_META = {"name": "my_skill", "description": "...", "version": "0.1.0"}
TOOLS = [{"type": "function", "function": {"name": "my_tool", "parameters": {"type": "object", "properties": {}, "required": []}}}]

def execute(tool_name: str, args: dict) -> str:
    ...
```

建议新增 `CAPABILITY_META`，声明能力域、操作风险和组合关系。未声明的旧技能仍兼容加载。工具名必须全局唯一。

## 自我迭代安全边界

- `app/`：真理之源，不挂进 Agent 容器。
- `app-fork/`：当前运行副本，只读。
- `app-tmp/`：自我迭代暂存区。
- `scripts/`：宿主机底线脚本，只读。
- Agent 不挂载 `/var/run/docker.sock`。
- Agent 不挂载 `secrets/` 或 `.ops-runner.env`。
- 人类决定与 Runner 结果在 Agent 容器中为只读挂载。

## 开发与测试

```bash
cd app
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

主分支上的 GitHub Actions 会执行：

- 全部 Python 编译检查
- 全部单元测试
- 关键 Shell 脚本语法检查

## 当前能力边界

本版本已经建立服务器运维的完整安全执行链，但不会尝试覆盖所有 Linux 运维动作。以下需求应作为后续独立能力设计，而不是塞进通用 Shell：

- 数据库备份、恢复与迁移
- 日志诊断与自动根因分析
- 代码 Bug 修复和仓库变更
- 测试、冒烟与验收
- 发布编排、流量切换与回滚
- Secret 管理

这种边界是为了让后续能力可以组合，同时每一步仍有清晰权限、证据和回滚策略。
