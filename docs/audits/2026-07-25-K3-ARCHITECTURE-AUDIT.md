# K3 多轮迭代后的 Agenelf 架构审计

日期：2026-07-25

## 结论

K3 的更新显著补强了自我优化、任务板、成长守护和能力快车道，但“动态 Python 代码直接进入 Agent 进程”使能力增长速度超过了隔离治理速度。本轮优先收敛该风险，并落地研究路线图中的 `code.repair` 第一阶段。

## P0

### 1. 旧 `code_writer` 可在 Agent 进程执行任意 Python

影响：模型可读取进程环境、访问 Agent 可见挂载、启动子进程，并绕过结构化 Runner。

处理：永久禁用 `run_python`；写入仅限 `workspace/scratch` 有界文本。可执行验证迁移到无网络 `repair-runner`。

### 2. `app-space` 技能默认自动导入

影响：Python 模块导入会执行顶层代码；正则危险模式和可选测试不能构成进程隔离，持久化文件还会在重启时再次加载。

处理：默认不扫描 `app-space`；`skill_forge` 默认关闭且测试必填。实验开关必须由主人显式设置，核心改动继续走 `app-tmp -> gate -> promotion`。

## P1

### 3. 缺少通用外部仓库修复工作区

处理：新增仓库别名、指纹绑定补丁队列、只读源码、一次性 Git 副本、受限测试 profile、可信结果和 Docker 隔离。

### 4. 能力风险元数据尚未形成统一执行中间件

当前部分 Skill 自行排队和审批，部分本地状态工具直接执行。建议后续为 Registry 增加统一 `execution_mode`（pure/local_state/queued_runner/forbidden）并在 dispatch 前强制校验。

## P2

- `task_board` 与 `TaskEngine` 职责重叠，应继续收敛为轻量待办与受治理工作流两层明确接口；
- ModelRouter 仍未驱动真实 ModelClient，缺少成本、延迟和质量在线评估；
- 依赖使用宽松下界，建议增加锁文件、SBOM 和依赖漏洞检查；
- 代码修复 v1 仍依赖共享 repair-runner 容器，后续应升级为每请求一次性容器或微型虚拟机；
- 手机、语音和 Web 仍只有统一信封契约，尚未有真实客户端。

## 本轮验收

- 真实临时 Git 仓库补丁应用与测试；
- 请求和补丁篡改拒绝；
- 受保护路径拒绝；
- 源仓库保持不变；
- Agent/Runner Docker 挂载隔离；
- 动态代码快车道默认关闭；
- 旧任意 Python 入口不可执行；
- 完整仓库单元测试、编译、治理策略、Docker 拓扑与 Shell 语法通过。
