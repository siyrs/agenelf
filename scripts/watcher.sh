#!/usr/bin/env bash
# watcher.sh — 宿主机守护进程：自动执行通过底线检查的晋升请求
#
# 每 10 秒扫描 data/promote-requests/*/READY，发现即调用 promote.sh。
#
# 启动（宿主机，二选一）：
#   1) nohup：
#        nohup bash scripts/watcher.sh >> logs/watcher.log 2>&1 &
#        echo $! > data/watcher.pid
#   2) systemd 单元示例：
#        [Service]
#        ExecStart=/usr/bin/bash /path/to/project/scripts/watcher.sh
#        Restart=always
#
# 停止：
#   kill $(cat data/watcher.pid)    # 或：pkill -f 'watcher.sh'
#
# 注意：本脚本只运行在宿主机；容器内 agent 无法触碰（scripts/ 只读挂载，
# 且 agent 没有宿主机执行权限），从而形成“人类/守护进程掌握执行权”的制衡。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQUESTS_DIR="${ROOT_DIR}/data/promote-requests"
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
INTERVAL=10

mkdir -p "${REQUESTS_DIR}" "${ROOT_DIR}/logs"

log() {
    local m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${LOG_FILE}"
}

log "[watcher] 启动，扫描目录：${REQUESTS_DIR}，间隔 ${INTERVAL}s"

while true; do
    # 遍历所有 READY 标记（nullglob 避免无匹配时字面量展开）
    shopt -s nullglob
    for ready in "${REQUESTS_DIR}"/*/READY; do
        req_id="$(basename "$(dirname "${ready}")")"
        log "[watcher] 发现就绪请求：${req_id}，调用 promote.sh"
        if bash "${SCRIPT_DIR}/promote.sh" "${req_id}"; then
            log "[watcher] 请求 ${req_id} 晋升成功"
        else
            log "[watcher] 请求 ${req_id} 晋升失败（详见上方日志），保留请求目录待人工处理"
        fi
    done
    shopt -u nullglob
    sleep "${INTERVAL}"
done
