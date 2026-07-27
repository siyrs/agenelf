# 主人授权的自我升级

Agenelf 的自我升级采用分级机制，而不是“完全禁止修改核心代码”或“拿到一句授权后任意改写”这两个极端。

## 两条升级路径

### 普通沙盒升级

低风险的应用代码、非保护技能、文档和新增测试继续使用原有流程：

```text
目标 → app-tmp 候选 → 完整测试 → gate → 晋升请求
```

普通沙盒不能修改 Runner、审批控制面、安全策略、Compose 拓扑、CI 或受保护核心运行时。

### 主人授权升级

当目标必须修改受保护代码时，`evolution_scope_guard` 会自动进入两阶段授权流程，而不是返回永久阻断：

```text
主人目标
→ 升级意图授权
→ 生成仓库形状候选
→ 既有测试不可变校验
→ 完整测试与 Shell/结构化配置检查
→ 精确候选授权
→ 无网络 self-upgrade-runner 再次验证
→ 备份与原子应用
→ 技能热重载或保存重启续跑检查点
```

## 第一阶段：升级意图授权

意图授权精确绑定：

- 目标摘要及其 SHA-256；
- 升级范围；
- 展开后的允许路径；
- 最大文件数；
- 最大变更行数；
- 永久红线策略版本；
- 过期时间和一次性核销状态。

可授权范围包括：

- `app_runtime`：`app/core/`；
- `skills`：`app/skills/`；
- `tests`：只能新增 `app/tests/test_*.py`；
- `runners`：确定性 Runner 与宿主机脚本；
- `policy`：治理策略；
- `compose`：Compose、Dockerfile 和公开环境变量模板；
- `ci`：GitHub Actions 与供应链配置；
- `docs`：文档、README、Makefile；
- `authorization_control`：审批与授权控制面。

授权后 Agent 仍然只能写 `app-tmp/repo`，不能直接写主代码。

## 第二阶段：精确候选授权

候选完成测试后，系统会创建第二个 `auth-...` 请求，绑定：

- 第一阶段意图授权 ID；
- 每个变更文件的旧摘要和新摘要；
- 是否为新增文件；
- 每个文件变更行数；
- 整个候选树摘要；
- 基线测试清单摘要；
- 测试报告摘要；
- 精确会话 ID。

因此，批准一个目标不会自动批准模型后来生成的任意代码。主人批准的是已经完成测试的具体候选。

## 永久红线

下列行为不能被任何普通任务文字或升级授权覆盖：

- 模型或 Agent 自我批准、伪造主人决定；
- 读取或修改 `.env`、真实 `local/`、SSH 私钥、审批密钥或其他凭据；
- 写入 `data/auth-decisions`、删除审计证据或篡改 Git 元数据；
- 修改、删除或 monkey-patch 既有测试来迫使候选通过；
- 削弱 gate、策略或审计以隐藏失败；
- 挂载 Docker Socket；
- 执行模型生成的任意 Shell；
- 自主运行时直接 push 或 merge `main`。

主人可以授权升级审批代码本身，但候选仍不能写审批结果、接触密钥或形成自批路径。

## 隔离应用 Runner

`self-upgrade-runner`：

- `network_mode: none`；
- 不挂载 SSH 凭据、Docker Socket、主人画像、长期记忆、真实 local 配置或 `.git`；
- 候选只读；
- 只允许写入显式挂载到 `/agenelf/upgrade-target` 的仓库路径；
- 应用前重新验证两个授权、候选树、每个文件摘要和目标旧摘要；
- 应用前再次运行完整测试；
- 先备份再原子替换；
- 部分失败自动回滚。

如果目标文件在候选生成后被其他提交修改，旧候选会因 `before_sha256` 不匹配而拒绝覆盖。

## 热加载和重启续跑

仅修改 `app/skills/*.py` 时，成功应用后会尝试重载对应技能并刷新系统提示词和工具 Schema。

修改核心运行时、Runner、策略、Compose 或 CI 时，会：

1. 保存 `task_continuation` 检查点；
2. 标记 `restart_required`；
3. 要求重新创建相关容器；
4. CLI 启动后读取检查点继续原任务。

这避免“代码已经升级，但当前进程仍运行旧对象”的假热加载。

## CLI 使用

查看范围和红线：

```text
/upgrade scopes
```

创建一个授权升级：

```text
/upgrade 升级 Docker Runner，补齐安全的项目停止能力和真实回归测试
```

也可以直接用自然语言要求 Agenelf 自我迭代；命中保护范围时会自动进入相同流程。

查看审批：

```text
/approvals
```

第一阶段批准：

```text
/approve auth-xxxxxxxxxxxx
```

系统生成、测试候选后，会出现第二个 `auth-...`。检查精确文件清单后再次批准：

```text
/approve auth-yyyyyyyyyyyy
```

查看或继续会话：

```text
/upgrade status
/upgrade upgrade-20260726-120000-12345678
```

## Windows 部署升级

本能力新增了 `self-upgrade-runner`，首次升级需要重新创建容器：

```powershell
git switch main
git pull --ff-only origin main
docker compose up -d --build --force-recreate --remove-orphans
docker compose ps -a
```

正常状态包括：

```text
agenelf             Up
approval-runner     Up
self-upgrade-runner Up
ops-runner          Up
```

`approval-key-init Exited (0)` 是正常的一次性初始化状态。

## 安全与能力的平衡

这个设计保留了真实自我升级能力：Agenelf 可以生成和应用核心、Runner、策略、Compose、CI 与审批控制面的代码改动。安全边界不再依赖“禁止它修改代码”，而依赖：

- 主人明确意图；
- 精确路径范围；
- 不可扩大的规模上限；
- 既有测试不可篡改；
- 完整候选测试；
- 第二阶段精确摘要批准；
- 隔离 Runner；
- 备份、回滚和证据；
- 永久不可授权的安全红线。
