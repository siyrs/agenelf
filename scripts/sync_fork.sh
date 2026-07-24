#!/usr/bin/env bash
# sync_fork.sh — 将 app/（代码真理之源）同步到 app-fork/（运行时副本）
#
# 用途：
#   1. 容器启动前调用，生成容器实际运行的代码；
#   2. promote.sh 晋升成功后调用，让运行副本与 app/ 保持一致。
#
# 行为：删除 app-fork/ 中多余文件（--delete），排除 __pycache__。
# 本脚本在宿主机执行（容器内 scripts/ 为只读挂载，agent 无法修改本脚本）。
set -euo pipefail

# 定位项目根目录（scripts/ 的上一级），保证在任意目录调用都可用
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC="${ROOT_DIR}/app"
DST="${ROOT_DIR}/app-fork"

if [[ ! -d "${SRC}" ]]; then
    echo "[sync_fork] 错误：源目录不存在：${SRC}" >&2
    exit 1
fi
mkdir -p "${DST}"

echo "[sync_fork] 同步 ${SRC}/ -> ${DST}/（删除多余文件，排除 __pycache__）"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' "${SRC}/" "${DST}/"
else
    # rsync 不可用时的兜底：先清空再拷贝（保持与 --delete 等价语义）
    echo "[sync_fork] 提示：未找到 rsync，使用 cp 兜底同步"
    find "${DST}" -mindepth 1 -delete
    (cd "${SRC}" && tar cf - --exclude='__pycache__' --exclude='*.pyc' .) | (cd "${DST}" && tar xf -)
fi

echo "[sync_fork] 同步完成"
