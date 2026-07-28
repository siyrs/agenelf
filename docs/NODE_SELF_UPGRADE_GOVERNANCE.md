# Node.js / TypeScript 主人授权自升级治理

> 状态：已落地  
> 工作流事实源：`app/core/authorized_upgrade.py`  
> Node 扩展：`app/core/node_upgrade_policy.py`  
> 最终红线：`app/core/upgrade_redlines.py`  
> 可信候选验证器：`scripts/run_authorized_upgrade_tests.py`  
> 控制面镜像：`Dockerfile.control-plane`

## 1. 定位

Agenelf 的 Node Agent、API、CLI、Skill、Runner、事件协议和 Session Ledger 已逐步成为
生产主路径，因此自升级系统必须能够安全修改 TypeScript 和 Node 构建文件。

本批没有另起一套 Node 自升级系统，也没有把审批、证据和回滚逻辑复制到 JavaScript。
现有 Python owner-authorized upgrade engine 仍是唯一的工作流和证据事实源；Node 扩展只
增加：

- Node 作用域与路径；
- TypeScript 语法验证；
- Node 测试保护；
- Node 永久红线；
- Node 候选完整测试；
- Python + Node 双运行时隔离控制面。

这意味着 Node 迁移不会产生两套不一致的授权协议。

## 2. 保留的主人授权流程

Node 候选继续遵循原有双阶段授权：

1. 主人批准升级目标、scope、允许路径、最大文件数和最大变更行数；
2. Agent 只能在 `app-tmp/repo` 中生成候选；
3. 可信测试验证候选；
4. 候选树摘要、精确文件哈希和测试报告绑定到第二次主人审批；
5. network-none Self-upgrade Runner 重新核验审批、候选和测试；
6. Runner 备份目标文件并应用候选；
7. 任意中途失败触发部分变更回滚；
8. 结果、备份、审批消费和 Runner heartbeat 形成可信证据。

模型不能生成、伪造或替代任何主人决定。

## 3. Node 授权 scopes

| scope | 路径 |
|---|---|
| `node_runtime` | `node/packages/core/`、Node API、Node CLI |
| `node_skills` | `node/packages/skills/` |
| `node_runners` | Node Runner 与 Validation Runner |
| `node_tests` | `node/tests/` |
| `node_build` | package、lockfile、Node version、tsconfig、Node Dockerfile |
| `contracts` | 语言无关 JSON Schema |
| `compose` | 默认 Compose、Python rollback、Python/Node/control-plane Dockerfile |

主人批准的是 scope 展开后的精确路径集合，不是模糊的“允许修改 Node 项目”。

永久禁止路径仍包括：

- `.git/`
- `local/`
- `data/`
- `logs/`
- `workspace/`
- `app-tmp/`
- `app-space/`
- `code-workspaces/`
- `repair-space/`
- `secrets/`
- `.env`
- `.ops-runner.env`

主人授权不能覆盖这些永久红线。

## 4. 测试不可由候选弱化

### 4.1 既有测试不可修改

候选生成层与可信 Runner 都会保护：

- `app/tests/`
- `node/tests/`

候选不得删除、替换为符号链接或修改任何既有测试。可信 Runner 使用主人批准前的基线
SHA-256 清单逐文件复核 Python 和 Node 测试。

### 4.2 生产变更必须新增测试

Node/Python 生产代码或控制面变更必须新增至少一个：

- `app/tests/test_*.py`
- `node/tests/*.test.ts`

仅修改已有测试无法满足要求。

### 4.3 候选完整测试

在 owner-authorized candidate workspace 中，受保护的 Python 合同测试会：

1. 将候选复制到新的临时目录；
2. 清除 `AGENELF_*`、模型密钥和代理等运行时环境变量；
3. 执行 `npm ci --ignore-scripts`；
4. 执行完整 `npm run test:node`。

可信候选验证器同时执行：

- Python 编译；
- YAML/JSON/TOML 解析；
- Shell 语法；
- 可信 Governance 校验；
- 完整 Python unittest；
- 由受保护合同测试触发的完整 Node 测试。

## 5. Node 永久红线

最终 diff-aware scanner 只检查候选新增行，并保护关键根约束不被删除。

Node 候选禁止新增：

- `child_process.exec/execSync`；
- `spawn/spawnSync` 配合 `shell: true`；
- `eval`、`Function`、`vm.runIn*`、`vm.compileFunction`；
- `NODE_TLS_REJECT_UNAUTHORIZED=0`；
- `preinstall`、`install`、`postinstall`、`prepare` 等 npm 生命周期脚本；
- Docker Socket；
- 凭据读取；
- 自我批准；
- 审计破坏；
- 测试或门禁绕过；
- 远程脚本管道执行；
- 直接 push/merge `main`；
- 明显 API key。

Node 依赖安装继续使用 `npm ci --ignore-scripts`。

## 6. 双运行时可信控制面

`Dockerfile.control-plane` 基于：

- Python 3.12；
- 官方 Node 24.18 Debian 发行版。

Node 运行时从官方 Node 镜像复制，不在构建过程中添加第三方 Node apt 源或执行远程安装
脚本。

Self-upgrade Runner 继续：

- `network_mode: none`；
- read-only root filesystem；
- 非 root 用户；
- 无 Docker Socket；
- 无 SSH/服务器 secrets；
- 无主人 profile、memory 或 self 数据；
- 无 Git 元数据；
- 只写主人审批范围内显式挂载的升级目标。

显式 Node 目标包括：

- `node/`
- `contracts/`
- `package.json`
- `package-lock.json`
- `.node-version`
- `Dockerfile.node`
- `Dockerfile.control-plane`
- `docker-compose.yml`
- `docker-compose.python.yml`

## 7. 语言迁移边界

本批完成的是：

- Node 生产代码可以进入主人授权自升级流程；
- TypeScript、Node tests、Node build 和 contracts 有正式治理；
- Self-upgrade 控制面能够真实运行 Python 与 Node 全量验证。

本批没有宣称：

- Self-upgrade Runner 本体已经改写为 Node；
- Python 授权/审批/回滚事实源已经退役；
- internal Python compatibility API 已移除。

在 Approval、Repair、Ops 与 Self-upgrade Runner 完成独立迁移前，Python 控制面仍是受保护
回滚路径。

## 8. 验收门禁

Node 自升级治理必须同时通过：

- Node Upgrade Governance CI；
- owner-authorized candidate 完整 Python + Node suite；
- Node Runtime CI；
- Node Validation Migration CI；
- Python CI 与 Windows 审批/撤销回归；
- Security & Supply Chain；
- CodeQL；
- Pi Prompt Templates CI。

任一门禁失败均不得合并。

## 9. 后续顺序

下一阶段按照风险从低到高继续：

1. read-only Ops Runner；
2. Approval Runner；
3. Repair Runner；
4. change/privileged Ops Runner；
5. Self-upgrade Runner 本体；
6. 移除 internal Python compatibility API；
7. 归档 Python runtime。
