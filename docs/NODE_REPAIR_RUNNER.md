# Node Repair Runner

> 执行域：独立、无网络 Node.js/TypeScript + Python/Git 控制面  
> 请求：`data/repair-requests/repair-*.json`  
> 可信结果：`data/repair-results/repair-*.json`  
> 隔离 artifact：`repair-space/repair-*/`  
> 回放事件：`data/repair-events/repair-*.jsonl`

## 1. 安全模型

Agent 只生成结构化补丁请求，不直接修改主人源码仓库。Node Repair Runner：

1. 重新验证 `repair-*` ID、capability、operation、risk、fingerprint；
2. 按原始 UTF-8 字节复核 patch SHA-256 与大小；
3. 重新加载主人维护的 repository alias、test profile、保护路径和限制；
4. 将只读源码通过 `git clone --local --no-hardlinks` 复制到独立 artifact；
5. 扫描逃逸符号链接并验证 expected base；
6. 仅在隔离副本执行 `git apply --check`、`git apply` 与主人配置 argv；
7. 写可信 result/evidence，但永不 commit、push、merge 或改写源仓库。

默认容器 `network_mode: none`，不挂载 SSH secrets、审批 key、Docker Socket、Memory、
Self、Policy 或 Agent 源码。Python Repair Runner 仍保留在显式 rollback 拓扑中。

## 2. 兼容协议

继续使用现有 Python 协议：

- `repair-*` ID；
- `code.repair / apply_patch_and_test`；
- `risk=read` 表示只写隔离 artifact，不表示补丁天然可信；
- patch SHA-256 对原始 UTF-8 字节计算；
- request fingerprint 对 canonical payload 计算；
- repository/test profile 使用主人配置 alias；
- results、locks 与 artifact 目录保持兼容；
- result 明确 `source_repository_modified=false`、`committed=false`、`pushed=false`、`merged=false`。

## 3. 测试 argv

允许的可执行文件为固定集合，例如：

- `python`, `python3`, `pytest`；
- `mvn`, `./mvnw`, `gradle`, `./gradlew`；
- `npm`, `pnpm`, `yarn`；
- `go`, `cargo`, `dotnet`；
- `bash`, `sh`，但禁止 `-c`。

Runner 始终使用 `spawn(..., shell:false)`。环境移除模型/API 密钥与代理，npm 默认
`ignore-scripts`，每个命令有超时和有界脱敏输出。

## 4. 永久保护

- `.git/`、`.github/workflows/`、`local/`、`secrets/`、`.env`、Policy；
- 主人配置的额外 `protected_paths`；
- 二进制补丁、重命名补丁、路径逃逸、疑似凭据；
- 超过 max patch bytes/files 的请求；
- 未允许的 test profile 或 executable；
- 直接修改源仓库、commit、push、merge；
- 任意网络、Docker Socket 或 secrets 挂载。

## 5. Pi 风格事件

每个 repair run 追加：

- `repair.runner.claimed`
- `repair.clone.started`
- `repair.command.completed`
- `repair.result.persisted`
- `repair.failed`

事件用于运行时间线与回放；完成/失败声明必须回到 `repair-results` 和 artifact 证据核验。

## 6. 回滚

```bash
docker compose -f docker-compose.python.yml up -d --build
```

显式 Python 拓扑继续使用原 `scripts/repair_runner.py`，不加载 Node Repair overlay。
