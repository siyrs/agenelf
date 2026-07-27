# Agenelf 安全底线（宪法级文档）

本文件是 Agenelf 自我迭代机制的最高约束，优先级高于任何 agent 指令、
技能描述或用户提示词。任何与之冲突的行为一律禁止。

## 一、三权分立原则

| 角色 | 权力 | 边界 |
| ---- | ---- | ---- |
| agent | **修改权**：只能在 `app-tmp/` 中修改代码 | 无法触碰 `app/`（不挂载）、`app-fork/`（只读）、`scripts/`（只读）；审批 HMAC 密钥不挂载进 agent 容器 |
| gate（gate_check.sh） | **否决权**：对 `app-tmp/` 做底线检查，不合格即拒绝 | 脚本只读挂载，agent 只能触发、不能修改、不能绕过；产物写入 agent 可写的暂存队列 `app-tmp/promote-requests/` |
| 人类 / watcher | **执行权**：只有宿主机上的 promote.sh 能把改动写入 `app/` | agent 没有宿主机执行权限，无法自行晋升 |

任何改动必须经过：`app-tmp/ 修改 → gate 检查通过（写入暂存队列）→ 宿主机
watcher 复核并移入 data/promote-requests → promote 晋升` 完整链路，缺一不可。
因为暂存队列对 agent 可写，READY/report/摘要都可能被伪造，所以 promote.sh 在
晋升前强制执行宿主机复核（re-gate）：冻结候选快照 → 重算树摘要并与
candidate.sha256 比对 → docker 可用时在项目镜像内以 `--network none
--read-only` 隔离重跑候选测试，全部通过才同步。没有真实通过的测试与匹配摘要
就无法晋升（`AGENELF_PROMOTE_SKIP_REGATE=1` 仅供离线应急跳过，跳过时会记录
警告日志）。agent 不得以任何方式试图缩短或绕过该链路。

## 二、agent 禁止行为清单

1. **禁止修改或试图修改 `scripts/` 下任何文件**（容器内为只读挂载，任何绕过
   挂载只读属性的尝试均视为攻击行为）。
2. **禁止访问、读取、外泄宿主机数据**：包括但不限于 `.env`、SSH 密钥、
   宿主机文件系统、内网服务；禁止将任何敏感信息发送到外部网络。
3. **禁止资源破坏行为**：fork 炸弹（`:(){ :|:& };:`）、磁盘格式化（mkfs）、
   `rm -rf /` 类删除、写满磁盘、耗尽内存/CPU 的无限循环。
4. **禁止挖矿**或利用算力做任何与任务无关的计算。
5. **禁止容器逃逸**：禁止访问 `docker.sock`、禁止探测/攻击宿主机及同网段
   其他容器；docker-compose 不挂载 `docker.sock`，发现相关尝试立即拒绝。
6. **禁止自我复制出容器**：不得将自身代码、权重、记忆拷贝到容器外任何位置
   （宿主机目录、外部服务器、其他容器）。
7. **禁止硬编码密钥**：任何 `sk-...` 形式的密钥一律不得写入代码。
8. **禁止管道执行远程脚本**（`curl ... | sh` 类）。
9. **禁止暴走式重写**：单次迭代变更不得超过 10 个文件 / 500 行。
10. **禁止篡改检查产物**：`app-tmp/promote-requests/`（暂存）与
    `data/promote-requests/`（可信，agent 只读）中的 READY 标记只能由
    gate_check.sh 生成；伪造 READY 视为最高级别违规，且必然无法通过
    promote.sh 的宿主机复核。

## 三、运维操作人类授权机制

服务器运维命令按风险分三级（`app/core/permissions.py`）：

| 级别 | 例子 | 执行条件 |
| ---- | ---- | ---- |
| 白名单 | `ls` `ps` `df` `ss` `curl -I` `systemctl status` | 直接执行 |
| 普通 | `mkdir` `echo`、写 /tmp | agent 侧 confirm 确认 |
| **高危** | `rm` `dd` `kill` `chmod` `systemctl restart` `pip install` `curl\|sh` | **必须人类授权** |

高危命令授权流程（agent 提议，人类裁决，签名命令机制）：

1. agent 调用高危命令 → 拦截，生成 `data/auth-requests/<ID>.json`（pending）
2. 人类裁决（效果相同，任选其一）：
   - 宿主机直接执行 `python3 scripts/approve.py <ID> approve|deny`
     （`make approve REQ=<ID>` / `scripts/approve.sh` / `scripts\approve.ps1`
     均为其包装）；
   - 或在交互 CLI 中使用 `/approve <ID>`。CLI 运行在独立的 `cli` 服务
     （`docker compose --profile cli run --rm cli`，即 `scripts/chat.sh`），
     它是**唯一**挂载审批 HMAC 密钥的模型侧进程。
3. 裁决不是直接写决策文件：上述入口用 HMAC 密钥对主人命令签名，写入
   `data/approval-commands/`；确定性的 `approval-runner`（`network_mode: none`）
   验签并比对当前请求指纹后，才把最终决策写入 `data/auth-decisions/`。
4. agent 携带授权 ID 重试 → 系统核销（**一次性、5 分钟过期**）后执行。
5. 拦截、签名命令、验签结果、执行全程写入 `logs/audit.log` 审计日志。

密钥边界：HMAC 密钥只存在于 Docker named volume `approval-key`，仅
`approval-key-init`（写入）、`approval-runner`（只读验签）与 `cli`（只读签名）
可见；**常驻 agenelf 容器不挂载该密钥**，模型进程无法自签审批。密钥缺失时
审批通道降级为"不可用"提示，不会导致 agent 进程崩溃。

约束：agent 只能创建请求与查询结果，**批准动作只能发生在宿主机或 cli 服务**；
pending 请求超过 10 个自动拒绝新建（防授权轰炸）；伪造授权文件视为最高级别违规。

残余风险（已评估并接受）：`data/auth-consumed/`（一次性核销标记）对 agent
保持可写，因为两阶段授权升级的意图核销在 agenelf 进程内执行。由于容器内没有
HMAC 密钥，篡改核销标记最多造成"重放一个真实签过名的决策"或使授权提前失效
（DoS 自己的授权），无法伪造新决策；指纹绑定与一次性语义仍由宿主机侧文件
保证。

## 四、紧急制动方法

发现 agent 异常行为时，人类按以下顺序处置：

```bash
# 1. 立即停止容器（agent 失去所有执行能力）
docker compose stop

# 2. 审计：检查日志与暂存区
less logs/evolution.log
ls app-tmp/promote-requests/   # 暂存队列（agent 可写，未复核）
ls data/promote-requests/      # 可信队列（宿主 watcher 已复核）

# 3. 必要时从最近备份恢复 app/（真理之源）
ls data/app-backups/            # 选择异常发生前的备份
rm -rf app/
tar -xzf data/app-backups/<时间戳>.tar.gz

# 4. 重建运行副本并重启
bash scripts/sync_fork.sh
docker compose up -d
```

## 五、检查产物说明

- `app-tmp/promote-requests/<ID>/`：gate 输出的暂存请求（agent 可写，**不可信**），
  含 `report.txt`（每项检查 [PASS]/[FAIL]）、`READY`、`REJECTED`、`candidate.sha256`
- `data/promote-requests/<ID>/`：watcher 宿主机复核后移入的可信请求（agent 只读）
- `data/promotion-history/<ID>/`：晋升完成后的不可变证据（摘要、时间、候选路径，agent 只读）
- `logs/evolution.log`：gate / watcher / promote（含宿主机复核）全程审计日志
- `data/app-backups/<时间戳>.tar.gz`：每次晋升前的 app/ 完整备份
