# 私人助理基础能力验收矩阵

本矩阵覆盖长期任务、多模型路由和多端命令信封。任何一项失败都不能把本批次标记为完成。

## 自动化命令

```bash
python scripts/validate_governance.py
python -m compileall -q app scripts
cd app && python -m unittest discover -s tests -v
bash -n ../scripts/*.sh
```

CI 还会在干净环境中执行：

```bash
cp .env.example .env
cp .ops-runner.env.example .ops-runner.env
python scripts/init_local.py --no-migrate
docker compose config
```

## Task Engine

- [ ] 创建任务必须包含 `owner_goal`、验收和证据计划；
- [ ] 变更任务缺少回滚计划时拒绝；
- [ ] 步骤依赖未完成时不能启动；
- [ ] 高风险步骤等待授权时必须关联 `op-/auth-`；
- [ ] 成功步骤必须有关联证据；
- [ ] `completed` 只能从 `verifying` 进入；
- [ ] 普通 note 不能作为可信完成证据；
- [ ] `op-/val-/test/promotion` 证据可以通过完成门；
- [ ] 暂停、恢复、取消和失败重试符合状态机；
- [ ] revision 冲突会拒绝多端静默覆盖；
- [ ] 主人取消后未来步骤全部停止。

## 模型路由

- [ ] 支持 DeepSeek/GPT/GLM/Ollama 别名；
- [ ] 路由按能力、成本、隐私和主人顺序确定；
- [ ] 没有凭据的外部模型标记为未就绪；
- [ ] 本地 Ollama 可作为 privacy 路由；
- [ ] 配置中出现内联 API Key/Token 时拒绝；
- [ ] 返回目录和路由结果不包含 Key 值或环境变量内容；
- [ ] 没有就绪候选时明确失败，不静默降级。

## 多端命令信封

- [ ] CLI/HTTP/Web/Mobile/Voice 使用同一 schema；
- [ ] actor、session、channel 和 idempotency key 绑定；
- [ ] 同键同载荷只返回原请求；
- [ ] 同键不同载荷拒绝；
- [ ] 输入中的凭据在持久化前脱敏；
- [ ] 只允许安全客户端元数据；
- [ ] 授权只能引用已有 ID，不能携带 Bearer Token；
- [ ] 语音文本不能独立构成不可逆授权。

## Docker 与私有数据

- [ ] `make init` 创建 `local/models.yaml` 且不覆盖主人修改；
- [ ] Agent 只读挂载 `local/models.yaml`；
- [ ] ops-runner 和 validation-runner 看不到模型配置；
- [ ] `local/models.yaml` 被 Git 忽略；
- [ ] 实际 Key 只来自环境变量或外部 Secret。

## 诚实交付

本轮验收通过只能说明以下基础完成：

- 长期任务控制面；
- 多模型选择策略；
- 多端统一请求协议。

不得据此声称手机 APP、Voice Gateway、后台 Scheduler、通用 code.repair 或动态多 SDK 模型调用已经完成。
