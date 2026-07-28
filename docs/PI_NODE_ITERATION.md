# Pi 研究落地与 Node.js 迁移迭代

## 1. 采用范围

Agenelf 不复刻 Pi，也不把自身变成默认拥有完整宿主权限的代码代理。本轮采用与长期个人智能体目标相容的部分：

- Markdown Prompt Templates；
- 可发现的斜杠命令与 CLI 补全；
- 内置资源与主人资源分层；
- 小型、可组合、零运行时 npm 依赖的 Node Core；
- 继续复用 Agent Event Protocol、Session Ledger、Skill Registry、Policy 与独立 Runner。

Node Validation Runner 已在前一批进入 `main`，本轮以它为已验证基线，不重复重写。

## 2. Markdown Prompt Templates

### 2.1 目录与优先级

- 内置模板：`node/prompts/*.md`；
- 主人模板：`local/prompts/*.md`；
- 主人模板按名称覆盖内置模板；
- `local/prompts/` 默认被 Git 忽略，并只读挂载到 Agent/CLI。

### 2.2 调用

- `/plan <目标>`
- `/review <对象>`
- `/test <功能>`
- `/prompt:<name> <参数>`

支持 `{{input}}`、`{{args}}` 以及 `{{1}}` 到 `{{9}}`。没有占位符时，用户输入会以明确区块追加到模板后面。

### 2.3 安全约束

- 只读取普通 `.md` 文件；
- 拒绝符号链接；
- 单模板最大 64 KiB；
- 最多 100 个模板；
- frontmatter 仅允许 `name`、`description`；
- 不加载 JavaScript/TypeScript 扩展；
- 不执行命令；
- API/Tool 目录不暴露本地文件路径；
- Agent 仍不挂载 `local/secrets/` 或审批 key。

## 3. 为什么不直接采用任意扩展

Pi 的扩展模型适合受信任的本地开发环境，但 Agenelf 还承担服务器管理、自我升级、长期运行和主人私有数据处理。任意扩展代码若默认拥有宿主权限，会绕开 Agenelf 已建立的 Policy、审批、最小权限和 Runner 证据链。

未来若引入扩展，必须先完成：

1. manifest 与能力声明；
2. 来源、版本和完整性校验；
3. 主人显式授权；
4. 最小文件/网络/进程权限；
5. 隔离执行；
6. 可撤销与审计；
7. 不得绕过现有 Runner。

## 4. 运行时集成

### Agent

Agent 启动时发现内置与主人模板，将只读元数据放入 system prompt，并提供 `prompt_template_catalog`、`expand_prompt_template` 两个纯读取工具。

### API

- `GET /prompts`
- `POST /prompts/:name/expand`

除既有公开健康接口外，继续要求 `X-Agenelf-Token`。

### CLI

readline completer 会把固定命令与动态模板合并。输入 `/` 或前缀即可补全。模板先在本地确定性展开，再将完整提示词交给 Agent，不直接产生副作用。

## 5. 测试与供应链

本轮新增：

- 模板发现、参数、主人覆盖测试；
- 未知/可执行 frontmatter 拒绝测试；
- API catalog 与 expansion 测试；
- Compose 私有只读挂载测试；
- JavaScript/TypeScript CodeQL，与 Python CodeQL 并行；
- 继续运行全部 Node/Python/Validation/安全回归。

## 6. Node.js 后续迁移顺序

当前默认 Node：Agent、API、CLI、Validation Runner。

后续严格按风险递增：

1. read-only Ops 查询与状态聚合；
2. Approval Broker；
3. Repair Runner；
4. change/privileged Ops；
5. Self-upgrade Runner；
6. 清除剩余 legacy API 路由；
7. 归档 Python Runtime。

每批必须具备协议兼容、最小权限、真实 E2E、失败关闭和显式回滚；不能把“换成 Node”本身当作完成标准。
