# Agenelf 软件验证能力

## 目标

`software.validation` 是 Agenelf 的第一个独立质量能力域。它负责运行主人预先配置的 HTTP/TCP 检查、保存可信证据，并把失败反馈给持续自我改进系统。

它不接受模型生成的 URL、主机、端口或断言规则。Agent 只能选择 `local/validation.yaml` 中已经存在的别名。

## 分离执行

```text
用户/Agent
   │ 选择 check 或 suite 别名
   ▼
data/validation-requests/         Agent 可写
   │
   ▼
validation-runner                 不调用 LLM
   │ 读取 local/validation.yaml
   │ 执行固定 HTTP/TCP 检查
   ▼
data/validation-results/          Agent 只读
```

`validation-runner`：

- 不挂载 `local/secrets/`；
- 不读取主人画像或长期记忆；
- 不接受自由 Shell；
- 不允许请求文件覆盖 URL、Host、Port 或断言；
- 重新校验请求指纹和只读风险级别；
- 限制超时、响应体大小和断言数量。

## 配置

运行：

```bash
make init
```

会创建：

```text
local/validation.yaml
```

示例：

```yaml
checks:
  api-health:
    type: http
    description: API 健康检查
    url: https://service.example.com/health
    method: GET
    expected_status: [200]
    json_equals:
      status: ok
    contains: [ready]
    max_latency_ms: 2000
    timeout_seconds: 5

  database-port:
    type: tcp
    description: 数据库端口可达性
    host: 10.0.0.20
    port: 5432
    timeout_seconds: 5

suites:
  production-smoke:
    description: 生产环境基础冒烟
    checks: [api-health, database-port]
```

### HTTP 检查

支持：

- `GET` / `HEAD`；
- 期望状态码；
- 响应正文包含断言；
- JSON 点路径相等断言；
- 最大延迟；
- 1–30 秒超时；
- 最多读取 1 MB 响应体。

当前版本不支持在 `validation.yaml` 中保存认证 Header。需要认证的验证应在后续版本通过 Runner 专属凭据引用实现，不能把 Token 写进模型可读配置。

### TCP 检查

支持固定 Host/Port 的连接性和最大延迟断言。

## 使用

CLI 对话中可以直接说：

```text
列出可用验证检查。
运行 agenelf-health。
运行 agenelf-smoke 套件。
查询 val-xxxxxxxxxxxxxxxx 的结果。
```

对应工具：

```text
list_validation_checks
run_validation_check
run_validation_suite
get_validation_result
```

宿主机状态：

```bash
make validation
```

## 可信证据

结果示例：

```json
{
  "id": "val-...",
  "capability": "software.validation",
  "operation": "run_suite",
  "target": "production-smoke",
  "status": "failed",
  "summary": "1/2 个检查通过，1 个失败",
  "checks": [
    {
      "name": "api-health",
      "type": "http",
      "passed": true,
      "latency_ms": 32.1,
      "assertions": []
    }
  ]
}
```

结果不保存配置中的 URL、Host 或 Port，只保存别名、类型、观测值和断言结论。

## 与自我完善结合

`core/capability_health.py` 从可信结果计算能力健康度：

- `healthy`：已有观测且全部成功；
- `watch`：出现过失败，但没有连续退化；
- `degraded`：连续失败至少两次，或有三次以上观测且成功率低于 60%；
- `unknown`：尚无可信结果。

软件验证失败后，Agent 会：

1. 将失败纳入可观测自我模型；
2. 创建或复用一个 P1 改进意向；
3. 执行确定性反思沉淀；
4. 要求通过后续验证证据证明修复有效；
5. 仍然不能自动修改主分支或绕过晋升 Gate。

## 安全边界

以下模块属于安全关键代码，不能由自主补丁修改：

```text
core/validation.py
core/capability_health.py
skills/software_validation.py
```

`validation-runner` 位于宿主机只读 `scripts/`，同样不能由 Agent 修改。
