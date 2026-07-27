# 可见推理内容与终端样式

Agenelf 的交互终端会显示模型供应商明确返回的 `reasoning_content`。它不再只显示 `Agenelf 思考中...`：当模型开始返回推理 token 后，终端会增加独立的“🧠 Agenelf 思考过程”面板，并实时更新。

## 显示效果

思考过程与最终答案使用不同的终端样式：

- 思考过程：青色边框、青色弱化斜体文字，标题包含当前模型调用轮次；
- 最终答案：保持现有绿色 `Agenelf` 面板和 Markdown 渲染；
- 一次任务包含多轮工具调用时，每次模型调用都有独立的思考面板；
- 模型或中转接口没有返回 `reasoning_content` 时，不伪造思考文本，继续保留原来的等待状态和最终答案。

终端只能控制颜色、粗体、斜体、明暗等字符样式，不能可靠地为单个面板切换操作系统字体文件。因此这里使用 Rich 的 `italic dim bright_cyan` 字符样式，实现和最终答案的清晰视觉区分。

## 安装方式（有序钩子管线）

推理捕获不再通过 `MethodType` 包装 `llm.chat`：`reasoning_trace` 技能在
`configure_runtime` 中通过 `Agent.add_llm_wrapper(priority=100)` 注册为显式
有序钩子；`zz_transport_resilience` 以 `priority=1000` 注册为**最外层**重试
包装器（数值越大越外层），保留旧的“最后加载=最外层”语义但不再依赖技能
文件名排序。同名注册覆盖旧实现，因此技能重复加载不会叠加包装层；
`Agent.list_hooks()` 可按应用顺序（最外层在前）列出全部钩子用于诊断。

## 默认配置

`app/config.yaml`：

```yaml
llm:
  stream_reasoning: true

cli:
  show_reasoning: true
  reasoning_max_chars: 60000
```

说明：

- 只有交互式 TTY 会自动安装可见推理面板；HTTP API 和后台 Runner 不会把推理打印到日志；
- `stream_reasoning` 只在有终端 listener 时启用流式调用，后台调用继续使用非流式接口；
- 超过 `reasoning_max_chars` 时，面板只保留最近部分，避免超长推理拖垮终端刷新。

## DeepSeek V4 配置

DeepSeek V4 默认支持 thinking mode，也可以显式配置：

```yaml
llm:
  base_url: "https://api.deepseek.com"
  model: "deepseek-v4-pro"
  stream_reasoning: true
  thinking: enabled
  reasoning_effort: high
```

Agenelf 会同时处理：

1. 非流式响应中的 `message.reasoning_content`；
2. 流式响应中的 `delta.reasoning_content`；
3. LiteLLM/OpenAI 兼容对象 `model_extra` 中的同名字段；
4. 推理模型发生工具调用时，把对应 `reasoning_content` 原样回传到下一次模型请求。

第 4 点很重要：DeepSeek thinking mode 的工具调用协议要求在后续请求中继续携带该推理字段。Agenelf 只在当前工具调用链的内存中缓存它，不会写入聊天历史、长期记忆或审计日志。

## 隐私保护

显示前会脱敏：

- 常见密码、Token、API Key 和 Bearer 值；
- `vmess://`、`vless://`、`trojan://`、`ss://`、`ssr://`、`hysteria://`、`tuic://` 等代理节点 URI；
- URL 查询参数中的 `token`、`secret`、`password`、`api_key` 和 `key`。

模型请求协议需要回传的原始 `reasoning_content` 只保留在当前进程的有界内存缓存中；面板接收的是脱敏副本。

## 临时关闭或调整

关闭当前终端的思考显示：

```bash
AGENELF_SHOW_REASONING=0 make chat
```

调整单轮面板最大字符数：

```bash
AGENELF_REASONING_MAX_CHARS=120000 make chat
```

在非 TTY 测试环境强制启用显示：

```bash
AGENELF_FORCE_REASONING_DISPLAY=1 python app/cli.py --mock
```

## 验收重点

回归测试覆盖：

- 流式 reasoning token 实时聚合；
- 非流式 `model_extra.reasoning_content` 兼容；
- 工具调用参数分片拼接；
- 工具调用下一轮的 reasoning 回传；
- 思考面板与最终答案的样式差异；
- 思考内容中的凭据和代理 URI 脱敏；
- 技能重复加载的幂等性；
- 环境变量关闭显示。
