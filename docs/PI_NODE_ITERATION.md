# Pi 研究落地与 Node.js 迁移迭代

## 1. 本轮目标

本轮不是复刻 Pi，也不是把 Agenelf 改成一个无限制的代码代理，而是选择与 Agenelf 目标相容的能力：

1. 用 Markdown Prompt Templates 将常用工作流沉淀成可发现的斜杠命令；
2. 保持小型、可组合、零运行时 npm 依赖的 Node 核心；
3. 继续使用已落地的生命周期事件、Session Ledger、资源目录和工具注册表；
4. 将 Validation 控制面与 Runner 迁移到 Node，同时保留安全域、证据链和 Python 回滚。

## 2. 从 Pi 采用的设计

### 2.1 Markdown Prompt Templates

- 内置模板：`node/prompts/*.md`；
- 主人模板：`local/prompts/*.md`，默认被 Git 忽略；
- 主人模板可按名称覆盖内置模板；
- CLI 以 `/name` 或 `/prompt:name` 调用；
- 支持 `{{input}}`、`{{args}}` 与 `{{1}}` 到 `{{9}}` 参数；
- API 与 Agent Tool 只提供目录和文本展开。

模板元数据严格限制为 `name` 与 `description`，拒绝命令、代码入口和未知 frontmatter 字段。只读取普通 Markdown 文件，拒绝符号链接并限制文件大小和模板数量。

### 2.2 可发现的斜杠命令

Node CLI 使用 readline completer。输入 `/` 或命令前缀后，可补全内置命令与动态 Prompt Templates。默认模板为：

- `/plan`：形成可执行、可验证、可回滚的计划；
- `/review`：审查正确性、安全边界、兼容性与证据；
- `/test`：形成单元、集成、安全、兼容和 UAT 测试。

### 2.3 分层资源

已有 `ResourceLoader`、Skill Registry、Agent Event Protocol 与 Session Ledger 继续保留。Prompt Template 是一种新的只读资源层，不与 Skill、Tool、Runner 混为一体。

## 3. 明确不采用的部分

Pi 扩展适合受信任的本地开发环境，但 Agenelf 的目标包含长期运行、服务器管理、自我升级与主人数据，因此不直接引入“扩展代码获得宿主完整权限”的默认模型。

Agenelf 保留以下约束：

- Node Agent 不读取 `local/secrets/`；
- Node Agent 不持有审批 HMAC key；
- Prompt Template 不执行代码；
- 外部副作用只能经过 Policy、精确请求、审批和独立 Runner；
- Runner 使用最小化挂载和显式网络边界；
- 完成声明必须绑定 Runner、Validation 或晋升证据。

未来若引入扩展，也必须先建立 manifest、能力声明、签名/来源、权限授予和隔离执行，不直接加载任意本地 TypeScript。

## 4. Node Validation Runner

### 4.1 控制面

Node Agent/API 新增：

- 验证目录；
- 提交单个检查；
- 提交套件；
- 查询可信结果。

Agent 只能选择 `local/validation.yaml` 中的别名。目录响应不包含 URL、Host、端口或具体断言。

### 4.2 不可变协议

请求继续使用既有 `val-*` 文件协议：

- 固定 `schema_version`；
- 固定 `software.validation` 能力；
- 仅 `run_check` / `run_suite`；
- 空自由参数；
- `risk=read`；
- canonical payload fingerprint；
- 原子创建且不覆盖。

Runner 在执行前重新构造 canonical payload 并校验 fingerprint，文件被篡改时写入失败证据，不执行网络检查。

### 4.3 严格 YAML 子集

为了维持零运行时 npm 依赖，Node Runtime 实现了验证配置所需的严格 YAML 子集，支持：

- mapping、sequence；
- string、number、boolean、null；
- inline array；
- 合法 JSON inline object；
- 注释和基础引号。

明确拒绝：

- anchor、alias、merge key、tag；
- multiline scalar；
- tab 缩进；
- 重复 key；
- 过大文件、过多行、过深嵌套。

### 4.4 Runner 安全域

默认 Compose 通过 `docker-compose.override.yml` 将 `validation-runner` 切换到 Node 镜像。其权限为：

- `local/validation.yaml:ro`；
- `data/validation-requests:ro`；
- `data/validation-results:rw`；
- `data/validation-locks:rw`；
- `data/runner-health:rw`；
- 不挂载主人 secrets；
- 不挂载审批 key；
- 不挂载 Docker Socket；
- 不挂载代码修改目录。

Validation Runner 必须访问主人配置的 HTTP/TCP 目标，因此不像 Approval/Repair Runner 那样设置 `network_mode: none`。

## 5. 回滚与兼容

- 默认 `docker compose ...` 自动加载 override，运行 Node Validation Runner；
- `docker compose -f docker-compose.python.yml ...` 不加载默认 override，保留 Python Validation Runner；
- 请求与结果文件协议保持兼容；
- Python legacy API 仍是无宿主端口的内部兼容服务；
- Approval、Ops、Repair、Self-upgrade 暂时继续使用既有 Python Runner。

## 6. 验收门禁

本轮 CI 必须覆盖：

1. Node 原生 TypeScript syntax check；
2. Prompt 发现、覆盖、展开与非法 frontmatter；
3. 严格 YAML 正常解析与危险语法拒绝；
4. Validation check/suite 成功；
5. fingerprint 篡改失败关闭；
6. API、SSE、历史与 legacy proxy 回归；
7. Compose 最小权限结构化检查；
8. Node 镜像独立启动；
9. Node API + Node Validation Runner + Python legacy 联合冒烟；
10. Python 完整回滚拓扑；
11. npm/pip audit、SBOM、gitleaks、ShellCheck 与 CodeQL。

## 7. 后续迁移顺序

Node 迁移继续按风险从低到高推进：

1. read-only Ops 查询与状态聚合；
2. Approval Broker；
3. Repair Runner；
4. change/privileged Ops；
5. Self-upgrade Runner；
6. 清除剩余 legacy API 路由；
7. 归档 Python Runtime，只保留必要迁移工具。

每一批都必须保留协议兼容、回滚路径、最小权限和完整测试，不以语言替换本身作为完成标准。
