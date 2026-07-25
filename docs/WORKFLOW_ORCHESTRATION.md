# Agenelf Workflow Orchestration

## 目标

将 Agenelf 从单次工具调用 Agent 演进为可管理长期任务的私人助理。

核心原则：

- Agent 负责理解目标和规划；
- Executor 负责执行已经授权的步骤；
- Validator 负责验证结果；
- Evidence 负责证明完成。

## 标准流程

```text
主人目标
  ↓
Task Plan
  ↓
Capability Selection
  ↓
Authorization Check
  ↓
Execution
  ↓
Validation
  ↓
Evidence Archive
  ↓
Learning / Reflection
```

## 任务状态

- proposed：发现任务
- planned：完成计划
- running：执行中
- waiting_approval：等待主人授权
- verifying：验证中
- completed：完成
- failed：失败
- cancelled：取消

## 与未来能力组合

Workflow 将连接：

- server.operations
- software.validation
- code.repair
- software.release
- self_development

## 安全要求

所有渠道必须复用同一控制面：

- CLI
- HTTP
- Web
- Mobile
- Voice

禁止语音或移动端绕过权限系统。

## 未来阶段

V1:
- 任务拆解
- 状态跟踪
- 证据链

V2:
- 多服务器自动化流程
- 代码修复流程
- 发布流程

V3:
- 手机语音管家
- 主动提醒
- 长周期任务管理
