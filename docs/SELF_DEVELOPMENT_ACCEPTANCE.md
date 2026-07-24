# 自我沉淀与完善意向验收矩阵

本文件记录 `agent.self_development` 在合并前必须通过的验收项。这里的“自我”只表示可观测的软件运行状态和跨会话连续性，不表示主观意识；状态文件必须持续保留 `consciousness_claim: false`。

## 自动化验收

```bash
python -m compileall -q app scripts
cd app && python -m unittest discover -s tests -v
bash -n ../scripts/gate_check.sh
```

覆盖范围：

- `local/self/state.json` 的稳定连续性 ID、原子写入和脱敏；
- 反思日志限量保存、去重和敏感信息清洗；
- P0–P3 完善意向的创建、排序、状态转换和验收条件；
- 达到对话阈值后的确定性自动沉淀，且自动沉淀不修改代码；
- 深度反思输出校验与失败时的确定性回退；
- 意向推进只能进入 `app-tmp → tests → gate → promotion request`；
- 只有存在不可变宿主机晋升证据时，意向才能自动标记为 `completed`；
- CLI、HTTP API 和技能协议；
- Docker 中 Agent 对 `local/self` 的读写挂载，以及 Runner 不可见；
- `make init` 对 `local/self` 数据文件的幂等初始化；
- 自主候选修改自我沉淀、隐私、权限或运维控制模块时，宿主机 Gate 必须拒绝。

## 人工验收语义

1. `/mind` 能展示连续性、最近反思和开放意向，但不能声称拥有情感或主观意识。
2. `/reflect` 只沉淀观察；`/reflect --deep` 可使用模型分析，但其 JSON 输出必须经过本地校验和脱敏。
3. `/intend P1 <目标>` 只创建意向，不应直接修改代码。
4. `/pursue <id>` 只生成计划；`/pursue <id> --apply` 才能进入受控沙盒。
5. 任何自动触发逻辑默认不追求意向、不提交 Git、不部署服务器、不绕过人工晋升。

## 软件验证与能力健康增量验收

- Agent 提交的验证请求只含 allowlist 别名，不含 URL、Host 或 Port；
- validation-runner 重新校验请求指纹、只读风险和配置别名；
- HTTP/TCP 检查有超时、响应体和断言上限；
- Agent 对 validation result 只读，Runner 看不到 SSH 密钥、主人画像、记忆或 `local/self`；
- 能力健康评分来自可信结果，连续失败能形成反思和去重意向；
- `/scorecard`、`/roadmap`、验证 API、CLI 与技能协议均有回归测试；
- 自主候选修改验证队列、能力健康或验证技能边界时，宿主机 Gate 必须拒绝。
