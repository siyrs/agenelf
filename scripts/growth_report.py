#!/usr/bin/env python3
"""Agenelf 成长报告生成器（确定性，不调用 LLM，仅标准库）。

把分散的成长证据（反思、意向生命周期、晋升历史、参数优化、能力健康、
审计/守护日志）聚合成一份人类可读的 Markdown 报告，让主人一眼看清
"它最近成长了什么"。

用法：
    python3 scripts/growth_report.py [--days 7] [--out docs/growth-reports/] [--root 仓库根]

数据源（全部容错：缺失即标注"无数据"，不崩）：
    local/self/state.json              连续性 ID / 原则摘要
    local/self/reflections.json        期内反思数、最新教训摘要 top5
    local/self/intentions.json         按状态统计 + 期内 completed/blocked 清单
    data/promotion-history/*/          期内晋升：ID、时间、候选摘要前 12 位
    local/self/optimizations.json      active 优化项 + 期内 history 动作（含负反馈回滚）
    app/core/capability_health.py      可 import 则取 scorecard 摘要
    logs/audit.log、logs/growth.log    期内事件计数：授权、锻造、守护轮次

输出：docs/growth-reports/<UTC日期>.md（同日覆盖），stdout 打印报告路径与一行摘要。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 日志行时间戳的两种已知格式：
#   [2026-07-25T01:38:17+00:00] [event] ...   （core 模块，ISO UTC）
#   [2026-07-25 09:32:03] [approve] ...       （shell 脚本，本地朴素时间）
_LOG_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s*\[(?P<event>[^\]]+)\]\s*(?P<detail>.*)$")

# 审计事件归类：授权 / 技能锻造 / 参数优化
_AUDIT_CATEGORIES = {
    "授权": {"approve", "deny", "auth_request", "auth_consumed"},
    "技能锻造": {"skill_forge"},
    "参数优化": {"optimization_apply", "optimization_rollback", "optimization_auto_rollback"},
}

# 守护日志 action 归类：守护轮次
_GROWTH_ACTIONS = {"round_start", "round_done"}

# 开放（非终态）意向状态
_OPEN_STATUSES = {"proposed", "planned", "active", "awaiting_promotion", "blocked"}


def _read_json(path: Path) -> Any | None:
    """容错读取 JSON；文件缺失/损坏一律返回 None，绝不让报告崩溃。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _parse_ts(value: object) -> datetime | None:
    """解析 ISO 时间戳；朴素时间视为本地时区，统一返回 aware datetime。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # 朴素时间按本地时区解释
    return parsed


def _in_period(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts <= end


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "未知"
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _cell(value: object, limit: int = 120) -> str:
    """表格单元格安全化：去换行、转义竖线、截断。"""
    text = str(value if value is not None else "").replace("\n", " ").strip()
    text = text.replace("|", "\\|")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _no_data(lines: list[str], reason: str) -> list[str]:
    lines.append(f"_无数据（{reason}）_")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 各小节采集
# ---------------------------------------------------------------------------

def section_state(root: Path, lines: list[str]) -> dict[str, Any]:
    """连续性状态：ID 与原则摘要。"""
    lines.append("## 自我连续性")
    lines.append("")
    data = _read_json(root / "local" / "self" / "state.json")
    if not isinstance(data, dict):
        _no_data(lines, "local/self/state.json 缺失或损坏")
        return {}
    identity = data.get("operational_identity") or {}
    principles = identity.get("principles") or []
    lines.append(f"- 连续性 ID：`{_cell(data.get('continuity_id'), 64)}`")
    lines.append(f"- 创建时间：{_fmt_ts(_parse_ts(data.get('created_at')))}")
    lines.append(f"- 最近沉淀：{_fmt_ts(_parse_ts(data.get('last_reflection_at')))}")
    if principles:
        lines.append("- 固定原则：")
        for item in principles:
            lines.append(f"  - {_cell(item, 200)}")
    lines.append("")
    return {"principles": len(principles)}


def section_reflections(
    root: Path, start: datetime, end: datetime, lines: list[str]
) -> dict[str, Any]:
    """反思：期内反思数 + 最新教训摘要 top5。"""
    lines.append("## 反思沉淀")
    lines.append("")
    data = _read_json(root / "local" / "self" / "reflections.json")
    if not isinstance(data, list):
        _no_data(lines, "local/self/reflections.json 缺失或损坏")
        return {}
    in_period = [r for r in data if isinstance(r, dict) and _in_period(_parse_ts(r.get("at")), start, end)]
    lines.append(f"- 历史反思总数：{len(data)}")
    lines.append(f"- 本周期内新增：{len(in_period)}")
    # 最新教训：优先取期内最新一条反思，期内没有则取历史最新
    pool = in_period or [r for r in data if isinstance(r, dict)]
    lessons: list[str] = []
    if pool:
        latest = max(pool, key=lambda r: str(r.get("at", "")))
        lines.append(f"- 最新反思：`{_cell(latest.get('id'), 64)}`（{_fmt_ts(_parse_ts(latest.get('at')))}，触发：{_cell(latest.get('trigger'), 32)}）")
        raw = latest.get("lessons") or []
        lessons = [str(item) for item in raw if item][:5]
    if lessons:
        lines.append("- 最新教训（top5）：")
        for lesson in lessons:
            lines.append(f"  - {_cell(lesson, 300)}")
    lines.append("")
    return {"total": len(data), "in_period": len(in_period)}


def section_intentions(
    root: Path, start: datetime, end: datetime, lines: list[str]
) -> dict[str, Any]:
    """意向：按状态统计 + 期内 completed/blocked 清单。"""
    lines.append("## 改进意向")
    lines.append("")
    data = _read_json(root / "local" / "self" / "intentions.json")
    if not isinstance(data, list):
        _no_data(lines, "local/self/intentions.json 缺失或损坏")
        return {}
    items = [i for i in data if isinstance(i, dict)]
    by_status: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
    if by_status:
        lines.append("| 状态 | 数量 |")
        lines.append("|---|---:|")
        for status in sorted(by_status):
            lines.append(f"| {status} | {by_status[status]} |")
        lines.append("")
    # 期内到达 completed/blocked 的意向（按 updated_at 判定）
    finished = [
        i
        for i in items
        if str(i.get("status")) in {"completed", "blocked"}
        and _in_period(_parse_ts(i.get("updated_at")), start, end)
    ]
    finished.sort(key=lambda i: str(i.get("updated_at", "")))
    lines.append(f"本周期内 completed/blocked：{len(finished)} 条")
    lines.append("")
    if finished:
        lines.append("| 意向 | 优先级 | 终态 | 时间 |")
        lines.append("|---|---|---|---|")
        for item in finished:
            lines.append(
                f"| {_cell(item.get('title'), 80)} | {_cell(item.get('priority'), 8)}"
                f" | {item.get('status')} | {_fmt_ts(_parse_ts(item.get('updated_at')))} |"
            )
        lines.append("")
    open_p0p1 = [
        i
        for i in items
        if str(i.get("status")) in _OPEN_STATUSES and str(i.get("priority")) in {"P0", "P1"}
    ]
    return {"total": len(items), "by_status": by_status, "open_p0p1": open_p0p1}


def _promotion_time(directory: Path) -> datetime | None:
    """晋升时间：优先 promoted_at 文件，其次 evo-ID 内嵌 UTC 时间戳，最后目录 mtime。"""
    stamp = (directory / "promoted_at").read_text(encoding="utf-8", errors="replace").strip() if (directory / "promoted_at").is_file() else ""
    parsed = _parse_ts(stamp)
    if parsed is not None:
        return parsed
    match = re.match(r"evo-(\d{8})-(\d{6})", directory.name)
    if match:
        try:
            return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def section_promotions(
    root: Path, start: datetime, end: datetime, lines: list[str]
) -> dict[str, Any]:
    """晋升历史：期内晋升的 ID、时间、候选摘要前 12 位。"""
    lines.append("## 晋升历史")
    lines.append("")
    base = root / "data" / "promotion-history"
    if not base.is_dir():
        _no_data(lines, "data/promotion-history/ 不存在")
        return {}
    rows: list[tuple[datetime | None, str, str]] = []
    for directory in sorted(base.iterdir()):
        if not directory.is_dir():
            continue
        promoted = _promotion_time(directory)
        if not _in_period(promoted, start, end):
            continue
        digest_file = directory / "candidate.sha256"
        digest = ""
        if digest_file.is_file():
            try:
                digest = digest_file.read_text(encoding="utf-8", errors="replace").strip()[:12]
            except OSError:
                digest = ""
        rows.append((promoted, directory.name, digest or "未知"))
    total_dirs = sum(1 for p in base.iterdir() if p.is_dir())
    lines.append(f"- 历史晋升总数：{total_dirs}")
    lines.append(f"- 本周期内晋升：{len(rows)}")
    lines.append("")
    if rows:
        lines.append("| 晋升 ID | 时间 | 候选摘要（前 12 位） |")
        lines.append("|---|---|---|")
        for promoted, name, digest in rows:
            lines.append(f"| {name} | {_fmt_ts(promoted)} | `{digest}` |")
        lines.append("")
    return {"total": total_dirs, "in_period": len(rows)}


def section_optimizations(
    root: Path, start: datetime, end: datetime, lines: list[str]
) -> dict[str, Any]:
    """参数优化：active 项 + 期内 history 动作（含负反馈回滚标注）。"""
    lines.append("## 参数优化")
    lines.append("")
    data = _read_json(root / "local" / "self" / "optimizations.json")
    if not isinstance(data, dict):
        _no_data(lines, "local/self/optimizations.json 缺失或损坏")
        return {}
    active = data.get("active") or {}
    if active:
        lines.append("当前生效的优化项：")
        lines.append("")
        lines.append("| 参数 | 当前值 | 应用时间 |")
        lines.append("|---|---|---|")
        for key in sorted(active):
            entry = active.get(key) or {}
            lines.append(
                f"| `{_cell(key, 64)}` | {_cell(entry.get('value'), 32)}"
                f" | {_fmt_ts(_parse_ts(entry.get('at')))} |"
            )
        lines.append("")
    else:
        lines.append("当前无生效中的优化项。")
        lines.append("")
    history = data.get("history") or []
    in_period = [
        h
        for h in history
        if isinstance(h, dict) and _in_period(_parse_ts(h.get("at")), start, end)
    ]
    in_period.sort(key=lambda h: str(h.get("at", "")))
    lines.append(f"本周期内优化动作：{len(in_period)} 次")
    lines.append("")
    if in_period:
        lines.append("| 时间 | 动作 | 参数 | 结果 | 理由 |")
        lines.append("|---|---|---|---|---|")
        rollbacks = 0
        for item in in_period:
            action = str(item.get("action", ""))
            reason = str(item.get("reason", ""))
            if action == "rollback":
                rollbacks += 1
                if "负反馈" in reason:
                    action = "rollback（负反馈自动回滚）"
            lines.append(
                f"| {_fmt_ts(_parse_ts(item.get('at')))} | {_cell(action, 32)}"
                f" | `{_cell(item.get('key'), 64)}` | {_cell(item.get('value'), 32)}"
                f" | {_cell(reason, 120)} |"
            )
        lines.append("")
    else:
        rollbacks = 0
    return {"active": len(active), "actions_in_period": len(in_period), "rollbacks": rollbacks}


def section_capability_health(root: Path, lines: list[str]) -> dict[str, Any]:
    """能力健康：可 import 则取 scorecard 摘要；不可 import 标注不可用。"""
    lines.append("## 能力健康")
    lines.append("")
    app_dir = str(root / "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    try:
        from core.capability_health import CapabilityHealth  # noqa: PLC0415

        snapshot = CapabilityHealth(root).snapshot()
    except Exception as exc:  # 模块不可用/读取失败均降级为标注
        _no_data(lines, f"capability_health 不可用：{type(exc).__name__}: {exc}")
        return {}
    scorecards = snapshot.get("scorecards") or {}
    lines.append(f"- 可信证据总数：{snapshot.get('evidence_count', 0)}")
    lines.append("")
    if scorecards:
        lines.append("| 能力 | 健康度 | 成功率 | 连续失败 |")
        lines.append("|---|---|---|---:|")
        for name in sorted(scorecards):
            card = scorecards.get(name) or {}
            rate = card.get("success_rate")
            lines.append(
                f"| {_cell(name, 64)} | {_cell(card.get('health'), 16)}"
                f" | {rate if rate is not None else '—'} | {card.get('consecutive_failures', 0)} |"
            )
        lines.append("")
    return {"evidence_count": snapshot.get("evidence_count", 0), "scorecards": len(scorecards)}


def section_logs(
    root: Path, start: datetime, end: datetime, lines: list[str]
) -> dict[str, Any]:
    """日志事件计数：授权、锻造、守护轮次（期内）。"""
    lines.append("## 运行日志事件")
    lines.append("")
    counts = {"授权": 0, "技能锻造": 0, "参数优化": 0, "守护轮次": 0}
    found_any = False

    audit_path = root / "logs" / "audit.log"
    if audit_path.is_file():
        found_any = True
        try:
            for line in audit_path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = _LOG_LINE_RE.match(line)
                if not match:
                    continue
                ts = _parse_ts(match.group("ts"))
                if not _in_period(ts, start, end):
                    continue
                event = match.group("event")
                for category, events in _AUDIT_CATEGORIES.items():
                    if event in events:
                        counts[category] += 1
                        break
        except OSError:
            pass

    growth_path = root / "logs" / "growth.log"
    if growth_path.is_file():
        found_any = True
        try:
            for line in growth_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if str(record.get("action")) not in _GROWTH_ACTIONS:
                    continue
                if _in_period(_parse_ts(record.get("ts")), start, end):
                    counts["守护轮次"] += 1
        except OSError:
            pass

    if not found_any:
        _no_data(lines, "logs/audit.log 与 logs/growth.log 均不存在")
        return {}
    lines.append("| 事件类别 | 期内次数 |")
    lines.append("|---|---:|")
    for category in ("授权", "技能锻造", "参数优化", "守护轮次"):
        lines.append(f"| {category} | {counts[category]} |")
    lines.append("")
    return counts


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------

def build_report(root: Path, days: int, now: datetime) -> tuple[str, dict[str, Any]]:
    end = now
    start = now - timedelta(days=days)
    lines: list[str] = []
    date_str = now.strftime("%Y-%m-%d")

    lines.append(f"# Agenelf 成长报告（{date_str}）")
    lines.append("")
    lines.append(
        f"- 统计周期：{start.strftime('%Y-%m-%d %H:%M UTC')} ~ {end.strftime('%Y-%m-%d %H:%M UTC')}"
        f"（近 {days} 天）"
    )
    lines.append("- 生成方式：`scripts/growth_report.py`（确定性聚合，未调用 LLM）")
    lines.append("- 说明：本报告只聚合可核查的文件证据，`consciousness_claim: false`")
    lines.append("")

    stats: dict[str, Any] = {}
    stats["state"] = section_state(root, lines)
    stats["reflections"] = section_reflections(root, start, end, lines)
    stats["intentions"] = section_intentions(root, start, end, lines)
    stats["promotions"] = section_promotions(root, start, end, lines)
    stats["optimizations"] = section_optimizations(root, start, end, lines)
    stats["health"] = section_capability_health(root, lines)
    stats["logs"] = section_logs(root, start, end, lines)

    # 下一步建议：P0/P1 开放意向 top3，无则"保持当前节奏"
    lines.append("## 下一步建议")
    lines.append("")
    open_p0p1 = stats["intentions"].get("open_p0p1") or []
    open_p0p1.sort(key=lambda i: (str(i.get("priority")), str(i.get("updated_at", ""))))
    if open_p0p1:
        for item in open_p0p1[:3]:
            lines.append(
                f"- **{_cell(item.get('priority'), 8)}** [{_cell(item.get('status'), 24)}]"
                f" {_cell(item.get('title'), 120)}（`{_cell(item.get('id'), 64)}`）"
            )
    else:
        lines.append("- 当前无 P0/P1 开放意向：保持当前节奏，继续小步、可验证的改进。")
    lines.append("")
    return "\n".join(lines), stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agenelf 成长报告生成器（确定性，不调 LLM）")
    parser.add_argument("--days", type=int, default=7, help="统计周期天数（默认 7）")
    parser.add_argument(
        "--out",
        default="docs/growth-reports",
        help="报告输出目录（相对 --root 解析，默认 docs/growth-reports）",
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="仓库根（默认取脚本上级目录）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    days = max(1, int(args.days))
    now = datetime.now(timezone.utc)

    body, stats = build_report(root, days, now)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{now.strftime('%Y-%m-%d')}.md"
    report_path.write_text(body, encoding="utf-8")

    # stdout：报告路径 + 一行摘要，供守护进程/cron 留痕
    reflections = stats.get("reflections") or {}
    promotions = stats.get("promotions") or {}
    intentions = stats.get("intentions") or {}
    open_count = sum(
        (intentions.get("by_status") or {}).get(status, 0) for status in _OPEN_STATUSES
    )
    print(f"报告已生成：{report_path}")
    print(
        f"摘要：近 {days} 天反思 {reflections.get('in_period', 0)} 条，"
        f"晋升 {promotions.get('in_period', 0)} 次，开放意向 {open_count} 个"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
