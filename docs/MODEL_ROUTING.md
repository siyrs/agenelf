# Agenelf 多模型治理与路由

## 目标

模型是可替换、不可信的规划器，不是权限源或执行证据源。Agenelf 的任务、授权、Runner、验证和审计规则必须在更换 DeepSeek、GPT、GLM 或 Ollama 后保持不变。

本轮新增：

```text
app/core/model_router.py
app/skills/model_routing.py
local/models.example.yaml
policy/model-routing-constraints.v1.yaml
```

## 私有配置

复制：

```bash
cp local/models.example.yaml local/models.yaml
```

`local/models.yaml` 已被 Git 忽略。文件内只能写 `api_key_env`，真实 Key 放在 `.env` 或宿主机密钥系统：

```env
DEEPSEEK_API_KEY=...
GPT_API_KEY=...
GLM_API_KEY=...
```

禁止：

```yaml
api_key: plaintext-secret
```

## 路由维度

路由器按以下顺序过滤：

1. 主人定义的任务类型路由顺序；
2. 提供商是否启用；
3. 所需能力，如 `tools/coding/vision/privacy`；
4. 成本上限 `local/low/medium/high`；
5. 本地隐私偏好；
6. 凭据或本地运行时是否就绪。

没有就绪模型时返回明确失败和候选链，不会静默切换到未配置提供商。

## 推荐初期策略

```text
routine / voice     -> DeepSeek 优先
reasoning           -> GPT -> DeepSeek -> GLM
coding              -> DeepSeek -> GPT -> GLM -> Ollama
privacy             -> Ollama only
vision              -> 支持视觉且已审核的模型
```

低成本默认并不表示低权限。所有工具调用仍经过同一 Policy、Task Engine、审批和 Runner。

## 工具

```text
list_model_profiles
route_model_task
```

目录和路由结果只返回：模型别名、模型名、协议、能力、成本、隐私类别和是否就绪。不会返回：

- API Key 值；
- Token；
- 环境变量内容；
- 聊天中写入的凭据。

## 当前边界

本轮完成的是**确定性选择策略**，尚未把一个任务动态迁移到多个不同 SDK 客户端。后续实现统一 `ModelClient` 时，应消费路由结果，并在审计中记录：

- 任务 ID；
- 选中别名与模型；
- 回退链；
- 超时与重试；
- 输入输出 Token 和费用；
- 是否使用本地模型。
