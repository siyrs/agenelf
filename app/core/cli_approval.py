"""Deterministic raw-terminal approval handling for the interactive CLI.

Only text read directly from ``Console.input`` is parsed. Operation requests and
owner-authorized self-upgrade requests share this control plane, while model output and
tool calls can never become an owner decision.
"""
from __future__ import annotations

import json
import os
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core import approval_catalog, operations, owner_approval


def _config_value(config: dict[str, Any], name: str, default: Any) -> Any:
    cli = config.get("cli", {}) if isinstance(config, dict) else {}
    return cli.get(name, default) if isinstance(cli, dict) else default


def _pending_table(rows: list[dict[str, Any]]) -> Table:
    table = Table(title="等待主人审批的请求")
    table.add_column("请求 ID", style="cyan")
    table.add_column("类型", style="blue")
    table.add_column("操作", style="magenta")
    table.add_column("目标")
    table.add_column("摘要")
    for item in rows:
        table.add_row(
            str(item.get("id", "")),
            "授权" if item.get("kind") == "authorization" else "运维",
            str(item.get("operation", "")),
            str(item.get("target", ""))[:50],
            str(item.get("summary", ""))[:120],
        )
    return table


def show_pending(console: Console, *, root: str | None = None) -> None:
    rows = approval_catalog.list_pending_requests(root)
    if not rows:
        console.print(
            Panel("当前没有等待主人审批的请求。", title="审批", border_style="cyan")
        )
        return
    console.print(_pending_table(rows[:20]))
    console.print(
        "[dim]输入 /approve <op-id|auth-id>、/deny <id>，或在只有一个待审批载荷时输入“审批通过”。[/dim]"
    )


def _fallback_text(request_id: str, action: str, kind: str) -> str:
    ps_action = "approve" if action == "approve" else "deny"
    services = (
        "approval-runner ops-runner"
        if kind == "operation"
        else "approval-runner self-upgrade-runner"
    )
    return (
        "审批代理没有在限定时间内响应。可在 Windows PowerShell 中执行：\n\n"
        f"  .\\scripts\\approve.ps1 {request_id} {ps_action}\n\n"
        "或使用跨平台 Python：\n\n"
        f"  py -3 .\\scripts\\approve.py {request_id} {ps_action}\n\n"
        f"然后运行 `docker compose up -d {services}`。"
    )


def _decision_panel(result: dict[str, Any], request: dict[str, Any]) -> Panel:
    decision = result.get("decision", {}) if isinstance(result, dict) else {}
    superseded = decision.get("superseded_duplicates") or []
    kind = "授权" if request.get("kind") == "authorization" else "运维"
    lines = [
        f"请求：{request.get('id')}",
        f"类型：{kind}",
        f"操作：{request.get('operation')}",
        f"目标：{request.get('target')}",
        f"裁决：{decision.get('decision', result.get('status'))}",
    ]
    if superseded:
        lines.append(
            "已自动拒绝同载荷重复请求："
            + ", ".join(str(item) for item in superseded)
        )
    lines.append("批准绑定当前请求指纹；参数、范围或候选摘要变化后必须重新申请。")
    return Panel("\n".join(lines), title="主人审批已记录", border_style="green")


def _find_upgrade_session(request_id: str) -> dict[str, Any] | None:
    try:
        from core import authorized_upgrade

        for session in authorized_upgrade.list_sessions(limit=100):
            if request_id in {
                str(session.get("intent_auth_id", "")),
                str(session.get("candidate_auth_id", "")),
            }:
                return session
    except Exception:
        return None
    return None


def _advance_upgrade_after_approval(
    *,
    agent: Any,
    request_id: str,
    console: Console,
    wait_seconds: float,
) -> dict[str, Any] | None:
    """Advance the exact upgrade session without asking the model to rediscover it."""

    session = _find_upgrade_session(request_id)
    if session is None:
        return None
    try:
        from core import authorized_upgrade

        with console.status("Agenelf 正在按已批准的精确升级会话继续..."):
            updated = authorized_upgrade.advance_session(
                agent,
                str(session["id"]),
                wait_seconds=max(2.0, min(float(wait_seconds), 30.0)),
            )
        public = authorized_upgrade.public_status(updated)
    except Exception as exc:
        public = {
            "id": session.get("id"),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    console.print(
        Panel(
            json.dumps(public, ensure_ascii=False, indent=2),
            title="主人授权升级进度",
            border_style=(
                "green"
                if public.get("status") in {"succeeded", "restart_required"}
                else "cyan"
            ),
        )
    )
    return public


def handle_owner_decision(
    *,
    agent: Any,
    raw_input: str,
    console: Console,
    config: dict[str, Any],
) -> bool:
    """Handle one explicit owner decision. Return False for ordinary chat text."""

    parsed = owner_approval.parse_owner_decision(raw_input)
    if parsed is None:
        return False
    action = parsed["action"]
    try:
        selected, _duplicates = approval_catalog.resolve_pending_request(
            parsed.get("request_id") or None
        )
    except owner_approval.AmbiguousApprovalError as exc:
        console.print(
            Panel(str(exc), title="需要明确请求 ID", border_style="yellow")
        )
        if exc.pending:
            console.print(_pending_table(exc.pending))
        return True
    except owner_approval.ApprovalError as exc:
        console.print(Panel(str(exc), title="审批失败", border_style="red"))
        return True

    request_id = str(selected["id"])
    kind = str(
        selected.get("kind")
        or ("operation" if request_id.startswith("op-") else "authorization")
    )
    wait_seconds = float(
        os.environ.get(
            "AGENELF_APPROVAL_WAIT_SECONDS",
            _config_value(config, "approval_wait_seconds", 8),
        )
    )
    try:
        command = owner_approval.submit_owner_command(
            request_id,
            action,
            parsed.get("reason", ""),
            owner_approval.default_actor(),
            ttl_seconds=max(15, int(wait_seconds) + 30),
        )
        result = owner_approval.wait_for_command_result(
            str(command["id"]),
            timeout_seconds=max(1.0, min(wait_seconds, 30.0)),
        )
    except (owner_approval.ApprovalError, OSError, ValueError) as exc:
        console.print(
            Panel(
                f"{exc}\n\n{_fallback_text(request_id, action, kind)}",
                title="审批通道不可用",
                border_style="red",
            )
        )
        return True

    if result.get("status") != "succeeded":
        console.print(
            Panel(
                f"{result.get('error', '审批代理未返回成功结果')}\n\n"
                + _fallback_text(request_id, action, kind),
                title="审批失败",
                border_style="red",
            )
        )
        return True

    console.print(_decision_panel(result, selected))
    if action != "approve":
        return True

    if kind == "operation":
        try:
            state = operations.wait_for_result(request_id, timeout_seconds=2.0)
        except (TypeError, ValueError):
            state = {}
        if state.get("result") is not None:
            console.print(
                Panel(
                    json.dumps(state, ensure_ascii=False, indent=2),
                    title="运维执行结果",
                    border_style="cyan",
                )
            )

    auto_continue = str(
        os.environ.get(
            "AGENELF_APPROVAL_AUTO_CONTINUE",
            _config_value(config, "approval_auto_continue", True),
        )
    ).strip().lower() not in {"0", "false", "off", "no"}
    if not auto_continue:
        return True

    if kind == "authorization":
        upgrade = _advance_upgrade_after_approval(
            agent=agent,
            request_id=request_id,
            console=console,
            wait_seconds=max(wait_seconds, 8.0),
        )
        if upgrade is not None:
            status = str(upgrade.get("status", ""))
            # First approval normally ends at awaiting_candidate_approval. The second
            # approval ends at apply_queued/succeeded/restart_required. None of these
            # states should ask the model to recreate or rediscover the request.
            if status not in {"succeeded"}:
                return True
            continuation = (
                f"主人授权升级 {upgrade.get('id')} 已由确定性流程成功应用。"
                "请使用最新技能和可信结果继续原始任务，不要重复创建升级或审批请求。"
            )
            status_text = "Agenelf 正在使用已升级能力继续原任务..."
        else:
            continuation = (
                f"主人已在交互终端通过确定性审批通道批准精确授权请求 {request_id}。"
                "请读取该授权绑定的任务状态并继续；不得扩大范围或重复创建相同授权。"
            )
            status_text = "Agenelf 正在继续已授权的任务..."
    else:
        continuation = (
            f"主人已在交互终端通过确定性审批通道批准运维请求 {request_id}。"
            "请查询该请求的可信执行结果，并继续批准前尚未完成的原始任务；"
            "不要重复创建相同载荷的请求。只有验证目标状态后才能宣称完成。"
        )
        status_text = "Agenelf 正在执行已批准的操作并继续原任务..."
    with console.status(status_text):
        reply = agent.chat(continuation, subject="cli")
    console.print(Panel(reply, title="Agenelf", border_style="green"))
    return True
