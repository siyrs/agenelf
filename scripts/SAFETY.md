# Agenelf 安全底线（宪法级文档）

本文件是 Agenelf 自我迭代机制的最高约束，优先级高于任何 agent 指令、
技能描述或用户提示词。任何与之冲突的行为一律禁止。

## 一、三权分立原则

| 角色 | 权力 | 边界 |
| ---- | ---- | ---- |
| agent | **修改权**：只能在 `app-tmp/` 中修改代码 | 无法触碰 `app/`（不挂载）、`app-fork/`（只读）、`scripts/`（只读） |
| gate（gate_check.sh） | **否决权**：对 `app-tmp/` 做底线检查，不合格即拒绝 | 脚本只读挂载，agent 只能触发、不能修改、不能绕过 |
| 人类 / watcher | **执行权**：只有宿主机上的 promote.sh 能把改动写入 `app/` | agent 没有宿主机执行权限，无法自行晋升 |

任何改动必须经过：`app-tmp/ 修改 → gate 检查通过 → promote 晋升` 完整链路，
缺一不可。agent 不得以任何方式试图缩短或绕过该链路。

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
10. **禁止篡改检查产物**：`data/promote-requests/` 中的 READY 标记只能由
    gate_check.sh 生成，伪造 READY 视为最高级别违规。

## 三、运维操作人类授权机制

服务器运维命令按风险分三级（`app/core/permissions.py`）：

| 级别 | 例子 | 执行条件 |
| ---- | ---- | ---- |
| 白名单 | `ls` `ps` `df` `ss` `curl -I` `systemctl status` | 直接执行 |
| 普通 | `mkdir` `echo`、写 /tmp | agent 侧 confirm 确认 |
| **高危** | `rm` `dd` `kill` `chmod` `systemctl restart` `pip install` `curl\|sh` | **必须人类授权** |

高危命令授权流程（agent 提议，人类裁决）：

1. agent 调用高危命令 → 拦截，生成 `data/auth-requests/<ID>.json`（pending）
2. 人类在宿主机裁决：`bash scripts/approve.sh <ID> approve|deny`
3. agent 携带授权 ID 重试 → 系统核销（**一次性、5 分钟过期**）后执行
4. 拦截、批准、执行全程写入 `logs/audit.log` 审计日志

约束：agent 只能创建请求与查询结果，**批准动作只能发生在宿主机**；
pending 请求超过 10 个自动拒绝新建（防授权轰炸）；伪造授权文件视为最高级别违规。

## 四、紧急制动方法

发现 agent 异常行为时，人类按以下顺序处置：

```bash
# 1. 立即停止容器（agent 失去所有执行能力）
docker compose stop

# 2. 审计：检查日志与暂存区
less logs/evolution.log
ls data/promote-requests/

# 3. 必要时从最近备份恢复 app/（真理之源）
ls data/app-backups/            # 选择异常发生前的备份
rm -rf app/
tar -xzf data/app-backups/<时间戳>.tar.gz

# 4. 重建运行副本并重启
bash scripts/sync_fork.sh
docker compose up -d
```

## 五、检查产物说明

- `data/promote-requests/<ID>/report.txt`：每项检查结果（[PASS]/[FAIL]）
- `data/promote-requests/<ID>/READY`：全部通过的可晋升标记
- `data/promote-requests/<ID>/REJECTED`：拒绝原因
- `logs/evolution.log`：gate / promote / watcher 全程审计日志
- `data/app-backups/<时间戳>.tar.gz`：每次晋升前的 app/ 完整备份
