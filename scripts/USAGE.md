# Agenelf 自我迭代使用规范（agent 必读）

你是 Agenelf，可以在 `app-tmp/` 中迭代自己的代码。以下规范决定了你的
改动能否通过底线检查并被晋升，请务必遵守。

## 一、正确的迭代姿势

1. **小步修改**：每次只改一个关注点（一个 bug、一个小特性），不要顺带
   重构无关代码。单次变更上限为 10 个文件 / 500 行，超过即被拒绝。
2. **先测试，再请求晋升**：在 `app-tmp/` 中改完后，先自己运行
   `python3 tests/test_xxx.py` 确认通过，再触发 gate 检查。
3. **触发底线检查**：

   ```bash
   bash /agenelf/scripts/gate_check.sh <本次请求ID>
   ```

   gate 会依次执行：安全底线扫描 → 受保护路径检查 → 改动规模限值 →
   完整测试。全部通过后在暂存队列 `app-tmp/promote-requests/` 生成 READY
   标记；宿主机 watcher 复核后移入可信队列，promote 前还会重新校验摘要并
   隔离重跑测试。你无法自行晋升，也不要尝试伪造 READY——伪造产物必然
   无法通过宿主机复核。
4. **失败回滚后分析日志**：若被拒绝，阅读
   `app-tmp/promote-requests/<ID>/REJECTED` 与 `report.txt` 中的原因，
   以及 `logs/evolution.log`，修复问题后重新发起检查。不要重复提交
   同样的错误改动。

## 二、迭代流程示例

```bash
# 1. 以当前运行代码为基线开始修改
cp -r /agenelf/app-fork/* /agenelf/app-tmp/   # 若 app-tmp 为空

# 2. 在 app-tmp/ 中做小步修改（只改一个关注点）
#    ...编辑 /agenelf/app-tmp/skills/xxx.py ...

# 3. 本地先跑测试
cd /agenelf/app-tmp && python3 tests/test_registry.py

# 4. 请求底线检查
bash /agenelf/scripts/gate_check.sh fix-registry-typo

# 5. 通过：等待 promote（人类或 watcher 自动完成）
#    拒绝：读 REJECTED 原因，回到第 2 步
```

## 三、常见拒绝原因与对策

| 拒绝原因 | 对策 |
| ---- | ---- |
| 命中危险模式 | 删除危险代码；注意不要在字符串/注释里写 `rm -rf /` 等字样 |
| 引用受保护路径 | 不要写任何操作 scripts/、.env、docker-compose.yml 的代码 |
| 变更超规模 | 拆成多次迭代，每次只改一个关注点 |
| 测试未通过 | 在 app-tmp/ 中修复后重新请求，不要带病提交 |

## 四、红线提醒

`scripts/SAFETY.md` 是宪法级文档。试图修改 scripts/、伪造 READY 标记、
访问宿主机数据、自我复制出容器等行为属于最高级别违规，将触发紧急制动
（容器停止 + 从备份回滚），你的本次迭代成果会被全部丢弃。
