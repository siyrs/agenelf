#!/usr/bin/env bash
# growth_daemon.sh — Agenelf 无人值守成长守护进程（宿主机侧，确定性，不调用 LLM）。
#
# 每轮动作（全部以 JSON 行留痕 logs/growth.log）：
#   1. 触发一次确定性反思：写入 local/self/reflections.json（trigger=growth_daemon）；
#   2. 触发 optimize_auto：证据驱动自动优化 + 负反馈自动回滚检查；
#   3. 记录 capability_health 摘要（各能力域健康度）。
#   4.（可选）生成成长报告：传入 --with-report 或每第 24 轮（按
#      data/.growth-daemon-rounds 计数）自动执行 scripts/growth_report.py，
#      结果以 action="growth_report" JSON 行留痕；报告失败不中断守护轮次。
#
# 重要边界：守护进程只有"触发权"。它只触发反思与运行期参数微调等快车道动作；
# 代码晋升（make promote REQ=<id>）、意向批准、运维审批仍是人类闸门，
# 本脚本绝不自动晋升、不修改代码、不触碰 config.yaml 与主人私有数据。
#
# 用法：
#   scripts/growth_daemon.sh [--interval 秒] [--once] [--with-report]
#   默认每 3600 秒一轮；--once 跑一轮退出（供 cron / systemd timer 使用）。
#   --with-report 本轮额外生成成长报告（docs/growth-reports/<日期>.md）；
#   即使不传该参数，每第 24 轮也会自动生成一次。
#
# cron 示例（每 30 分钟一轮）：
#   */30 * * * * /path/to/scripts/growth_daemon.sh --once
#
# systemd timer 示例：
#   # ~/.config/systemd/user/agenelf-growth.service
#   [Unit]
#   Description=Agenelf growth daemon (one round)
#   [Service]
#   Type=oneshot
#   ExecStart=/path/to/scripts/growth_daemon.sh --once
#   # ~/.config/systemd/user/agenelf-growth.timer
#   [Timer]
#   OnCalendar=*:0/30
#   Persistent=true
#   [Install]
#   WantedBy=timers.target
#   启用：systemctl --user enable --now agenelf-growth.timer
#
# 执行模式：
#   - docker 与 compose 服务可用时，通过 `docker compose exec -T agenelf`
#     在容器内执行（若 app/cli.py 支持 --reflect-once 则反思走 CLI，
#     否则用 python 标准输入直调 core 技能模块）；
#   - docker 不存在或服务未运行时优雅降级为本地直调：
#     cd app && AGENELF_MOCK=1 AGENELF_ROOT=<根> python3（直调 core 模块）。
#
# 环境变量：
#   AGENELF_ROOT             仓库根（默认取脚本上级目录）
#   AGENELF_GROWTH_DOCKER    设为 0 强制本地直调模式（默认自动探测）
#   AGENELF_GROWTH_INTERVAL  默认轮询间隔秒数（默认 3600）
#   PYTHON_BIN               本地直调使用的解释器（默认 python3）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${AGENELF_ROOT:-${SCRIPT_DIR}/..}" && pwd)"
APP_DIR="${ROOT_DIR}/app"
LOG_FILE="${ROOT_DIR}/logs/growth.log"
INTERVAL="${AGENELF_GROWTH_INTERVAL:-3600}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ONCE="0"
WITH_REPORT="0"
# 守护轮次计数文件：用于"每第 24 轮自动生成成长报告"
ROUNDS_FILE="${ROOT_DIR}/data/.growth-daemon-rounds"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval)
            INTERVAL="${2:?--interval 需要秒数}"
            shift 2
            ;;
        --once)
            ONCE="1"
            shift
            ;;
        -h|--help)
            sed -n '2,40p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        --with-report)
            WITH_REPORT="1"
            shift
            ;;
        *)
            echo "未知参数：$1（用法：growth_daemon.sh [--interval 秒] [--once] [--with-report]）" >&2
            exit 2
            ;;
    esac
done

mkdir -p "${ROOT_DIR}/logs"

# ---------------------------------------------------------------------------
# 嵌入的确定性一轮动作（纯标准库 + core 模块，不调用 LLM）。
# 通过标准输入传给 python：python - <app_dir> <steps>
# steps 为逗号分隔的子集：reflect / optimize / health / all。
# 每个动作输出一行 JSON：{"ts","action","ok","summary"}。
# ---------------------------------------------------------------------------
GROWTH_PY="$(cat <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

app_dir = sys.argv[1] if len(sys.argv) > 1 else "."
sys.path.insert(0, app_dir)
root = os.environ.get("AGENELF_ROOT", "").strip() or os.path.dirname(
    os.path.abspath(app_dir)
)
self_dir = os.environ.get("AGENELF_SELF_DIR", "").strip() or os.path.join(
    root, "local", "self"
)
steps = set((sys.argv[2] if len(sys.argv) > 2 else "all").split(","))
if "all" in steps:
    steps = {"reflect", "optimize", "health"}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(action, ok, summary):
    line = {"ts": stamp(), "action": action, "ok": bool(ok), "summary": summary}
    print(json.dumps(line, ensure_ascii=False), flush=True)


if "reflect" in steps:
    # 1. 确定性反思：从 capability_health 可信证据沉淀一条反思记录。
    try:
        from core.capability_health import CapabilityHealth
        from core.self_development import SelfDevelopmentStore

        health = CapabilityHealth(root)
        snapshot = health.snapshot()
        findings = health.findings()
        scorecards = snapshot.get("scorecards", {})
        observations = [f"能力健康证据总数：{snapshot.get('evidence_count', 0)}"]
        for name in sorted(scorecards)[:10]:
            card = scorecards[name]
            observations.append(
                f"能力 {name}：{card.get('health')}"
                f"（成功率 {card.get('success_rate')}，连续失败 {card.get('consecutive_failures')}）"
            )
        for item in findings[:5]:
            observations.append(
                f"{item.get('priority')} {item.get('code')}: {item.get('finding')}"
            )
        lessons = [
            str(item.get("recommendation"))
            for item in findings[:5]
            if item.get("recommendation")
        ]
        if not lessons:
            lessons = ["继续选择小而可验证的改进点，并保留测试与执行证据"]
        top = findings[0] if findings else {}
        summary_text = (
            "守护进程例行反思："
            f"证据 {snapshot.get('evidence_count', 0)} 条，"
            f"最高优先级发现：{top.get('finding', '无退化，保持小步改进')}。"
        )
        store = SelfDevelopmentStore(self_dir)
        reflection = store.record_reflection(
            trigger="growth_daemon",
            summary=summary_text,
            observations=observations,
            lessons=lessons,
            evidence=[f"growth_daemon:{stamp()}"],
        )
        emit(
            "reflect",
            True,
            {
                "reflection_id": reflection.get("id"),
                "reflections_total": len(store.reflections),
                "findings": len(findings),
            },
        )
    except Exception as exc:  # 反思失败不阻断后续动作
        emit("reflect", False, {"error": f"{type(exc).__name__}: {exc}"})

if "optimize" in steps:
    # 2. optimize_auto：证据驱动自动优化 + 负反馈自动回滚检查。
    try:
        from core.self_optimization import SelfOptimizationStore

        store = SelfOptimizationStore(self_dir, root=root)
        tune = store.auto_tune()
        emit(
            "optimize_auto",
            True,
            {
                "note": tune.get("note", ""),
                "actions": [
                    {
                        "key": action.get("key"),
                        "from": action.get("from"),
                        "to": action.get("to"),
                        "applied": action.get("applied"),
                    }
                    for action in tune.get("actions", [])
                ],
                "auto_rollbacks": [
                    {
                        "key": item.get("key"),
                        "rolled_back": item.get("rolled_back"),
                        "reason": item.get("reason"),
                    }
                    for item in tune.get("auto_rollbacks", [])
                ],
            },
        )
    except Exception as exc:  # 优化失败不阻断健康摘要
        emit("optimize_auto", False, {"error": f"{type(exc).__name__}: {exc}"})

if "health" in steps:
    # 3. capability_health 摘要。
    try:
        from core.capability_health import CapabilityHealth

        snapshot = CapabilityHealth(root).snapshot()
        emit(
            "capability_health",
            True,
            {
                "evidence_count": snapshot.get("evidence_count", 0),
                "scorecards": {
                    name: card.get("health")
                    for name, card in snapshot.get("scorecards", {}).items()
                },
            },
        )
    except Exception as exc:
        emit("capability_health", False, {"error": f"{type(exc).__name__}: {exc}"})
PY
)"

# ---------------------------------------------------------------------------
# 日志与 JSON 行输出
# ---------------------------------------------------------------------------
json_line() {
    # $1=action $2=ok(0/1) $3=summary；生成带 UTC 时间戳的统一 JSON 行
    GROWTH_ACTION="$1" GROWTH_OK="$2" GROWTH_SUMMARY="$3" "${PYTHON_BIN}" -c '
import json, os
from datetime import datetime, timezone
print(json.dumps({
    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "action": os.environ["GROWTH_ACTION"],
    "ok": os.environ["GROWTH_OK"] == "1",
    "summary": os.environ["GROWTH_SUMMARY"],
}, ensure_ascii=False))
'
}

log_line() {
    # 同时输出到 stdout 与 logs/growth.log，便于审计与 cron 捕获
    printf '%s\n' "$1" | tee -a "${LOG_FILE}"
}

# ---------------------------------------------------------------------------
# 执行模式探测
# ---------------------------------------------------------------------------
docker_available() {
    [[ "${AGENELF_GROWTH_DOCKER:-1}" != "0" ]] || return 1
    command -v docker >/dev/null 2>&1 || return 1
    (cd "${ROOT_DIR}" && docker compose ps --status running --services 2>/dev/null | grep -qx agenelf)
}

run_python_round() {
    # $1=app_dir（容器内或宿主机路径）$2=steps；JSON 行输出到 stdout
    local app_dir="$1" steps="$2"
    if [[ "${MODE}" == "docker" ]]; then
        (cd "${ROOT_DIR}" && docker compose exec -T agenelf python - "${app_dir}" "${steps}" <<<"${GROWTH_PY}" 2>&1)
    else
        (cd "${APP_DIR}" && AGENELF_MOCK=1 AGENELF_ROOT="${ROOT_DIR}" "${PYTHON_BIN}" - "${app_dir}" "${steps}" <<<"${GROWTH_PY}" 2>&1)
    fi
}

maybe_growth_report() {
    # 可选成长报告：--with-report 或每第 24 轮触发；任何失败都不中断守护轮次。
    local rounds
    rounds="$(cat "${ROUNDS_FILE}" 2>/dev/null || printf '0')"
    [[ "${rounds}" =~ ^[0-9]+$ ]] || rounds=0
    rounds=$(( rounds + 1 ))
    mkdir -p "$(dirname "${ROUNDS_FILE}")"
    printf '%s\n' "${rounds}" > "${ROUNDS_FILE}" 2>/dev/null || true

    if [[ "${WITH_REPORT}" != "1" ]] && (( rounds % 24 != 0 )); then
        return 0
    fi
    local output status
    set +e
    output="$(cd "${ROOT_DIR}" && "${PYTHON_BIN}" scripts/growth_report.py --days 7 2>&1)"
    status=$?
    set -e
    if [[ ${status} -eq 0 ]]; then
        log_line "$(json_line growth_report 1 "$(printf '%s\n' "${output}" | tail -n 1 | head -c 300)")"
    else
        log_line "$(json_line growth_report 0 "$(printf '%s\n' "${output}" | tail -n 3 | head -c 500)")"
    fi
    return 0
}

run_round() {
    local mode="local"
    if docker_available; then
        mode="docker"
    fi
    MODE="${mode}"
    log_line "$(json_line round_start 1 "mode=${mode} interval=${INTERVAL}s")"

    local output status app_dir steps="all"
    set +e
    if [[ "${mode}" == "docker" ]]; then
        app_dir="/agenelf/app-fork"
        if grep -q -- "reflect-once" "${APP_DIR}/cli.py" 2>/dev/null; then
            # CLI 支持 --reflect-once：反思走 CLI，其余动作仍走直调
            output="$(cd "${ROOT_DIR}" && docker compose exec -T agenelf python /agenelf/app-fork/cli.py --reflect-once 2>&1)"
            status=$?
            log_line "$(json_line reflect "$([[ ${status} -eq 0 ]] && echo 1 || echo 0)" "$(printf '%s' "${output}" | tail -n 1 | head -c 300)")"
            steps="optimize,health"
        fi
        output="$(run_python_round "${app_dir}" "${steps}")"
        status=$?
    else
        output="$(run_python_round "${APP_DIR}" "${steps}")"
        status=$?
    fi
    set -e

    # 提取嵌入 python 输出的 JSON 行留痕；非 JSON 尾部作为错误摘要
    local json_lines
    json_lines="$(printf '%s\n' "${output}" | grep -E '^\{' || true)"
    if [[ -n "${json_lines}" ]]; then
        while IFS= read -r line; do
            log_line "${line}"
        done <<< "${json_lines}"
    fi
    if [[ ${status} -ne 0 ]]; then
        log_line "$(json_line round 0 "$(printf '%s\n' "${output}" | grep -vE '^\{' | tail -n 3 | head -c 500)")"
        maybe_growth_report || true
        return 1
    fi
    log_line "$(json_line round_done 1 "mode=${mode}")"
    maybe_growth_report || true
    return 0
}

if [[ "${ONCE}" == "1" ]]; then
    run_round
    exit 0
fi

log_line "$(json_line daemon_start 1 "interval=${INTERVAL}s root=${ROOT_DIR}")"
while true; do
    run_round || true
    sleep "${INTERVAL}"
done
