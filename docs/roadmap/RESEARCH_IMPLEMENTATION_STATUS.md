# 深度研究报告实施状态

本文件用于防止把“规划完成”误报成“产品完成”。每轮迭代必须更新状态和证据。

## 已落地

| 研究方向 | 当前实现 | 主要证据 |
|---|---|---|
| 安全治理 | 五级风险、精确授权、永久禁止项、策略校验 | `policy/safety-constraints.v1.yaml`、CI |
| 自我认知与沉淀 | `local/self`、反思、意向、能力健康、路线图 | `docs/SELF_DEVELOPMENT.md` |
| 自主进化 | `app-tmp -> tests -> gate -> promotion` | `docs/AUTONOMY.md` |
| 多服务器运维 | Agent/审批/Runner 三权分离 | `docs/SERVER_OPERATIONS.md` |
| 软件验证 | 独立 HTTP/TCP validation-runner | `docs/VALIDATION.md` |
| 长期任务 | 任务状态机、步骤依赖、暂停恢复、证据门、revision | `docs/TASK_ENGINE.md` |
| 多模型治理 | DeepSeek/GPT/GLM/Ollama 脱敏路由目录 | `docs/MODEL_ROUTING.md` |
| 多端入口基础 | CLI/HTTP/Web/Mobile/Voice 命令信封、防重放 | `docs/CHANNELS.md` |
| 私人数据分层 | 通用 `app/` 与私有 `local/` 分离 | `docs/PERSONALIZATION.md` |

## 部分落地

| 方向 | 已有基础 | 尚缺 |
|---|---|---|
| 代码助手 | 项目自身的受控自主补丁和测试 Gate | 通用外部仓库工作区、PR 生命周期、语言构建矩阵 |
| 模型路由 | 配置、选择和回退策略 | 动态客户端切换、费用/质量/延迟在线评测 |
| 工作流 | 可恢复任务记录和下一步计算 | 后台调度、DAG 并行、补偿执行和限流 |
| 手机/语音 | 统一输入信封和授权引用 | Web/PWA、Android/iOS、ASR/TTS、设备认证与推送 |
| 主动管家 | 自动反思和改进意向 | 日历/提醒/通知、主动任务触发、安静时段与频率控制 |

## 未完成且不能声称完成

- 真实手机 APP；
- 真实语音识别和语音合成服务；
- 全自动生产环境回滚；
- 任意外部代码仓库自动修复并合并；
- 无人监督执行不可逆操作；
- 完全自主修改安全策略或主分支。

## 下一批优先级

1. `code.repair`：隔离 Git 工作区、补丁、测试、审查证据和 PR；
2. Workflow Scheduler：定时、重试预算、超时、取消和恢复；
3. Unified ModelClient：真正消费模型路由结果并记录成本质量；
4. Web/PWA：任务、审批、证据、模型路由和通知；
5. Voice Gateway：ASR/TTS，但高风险确认继续走可视化授权面板。

## 合并规则

每次标记一项“已落地”前必须同时存在：

- 通用代码；
- 自动化测试；
- 安全策略或约束；
- 对应文档；
- GitHub Actions 全绿；
- 主分支真实提交 SHA。
