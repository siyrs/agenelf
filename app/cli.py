"""Interactive Agenelf CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from core import operations
from core.agent import Agent
from core.cli_approval import handle_owner_decision, show_pending
from core.configuration import load_config as load_shared_config
from resume import run_once as resume_pending_task

console = Console()
_APP_DIR = Path(__file__).resolve().parent


def load_config(path: str | None = None) -> dict:
    return load_shared_config(app_dir=_APP_DIR, config_path=path)


def _json_panel(data, title: str) -> None:
    console.print(Panel(json.dumps(data, ensure_ascii=False, indent=2), title=title))


def _dispatch_json(agent: Agent, tool_name: str, args: dict | None = None):
    text = agent.registry.dispatch(tool_name, args or {}, subject="cli")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": text}


def cmd_skills(agent: Agent) -> None:
    table = Table(title="已加载技能")
    table.add_column("技能", style="cyan")
    table.add_column("版本", style="green")
    table.add_column("描述")
    table.add_column("工具", style="magenta")
    for name, module in agent.registry.skills.items():
        metadata = getattr(module, "SKILL_META", {})
        tools = ", ".join(
            tool.get("function", {}).get("name", "?")
            for tool in getattr(module, "TOOLS", [])
        )
        table.add_row(
            name,
            str(metadata.get("version", "?")),
            str(metadata.get("description", "")),
            tools,
        )
    console.print(table)
    if agent.registry.errors:
        console.print("[red]以下技能加载或运行时绑定失败：[/red]")
        for name, error in agent.registry.errors.items():
            console.print(f"  - {name}: {error.splitlines()[-1] if error else '未知错误'}")


def cmd_capabilities(agent: Agent) -> None:
    table = Table(title="能力域")
    table.add_column("能力 ID", style="cyan")
    table.add_column("领域", style="blue")
    table.add_column("版本", style="green")
    table.add_column("操作与风险")
    table.add_column("可组合")
    for capability in agent.registry.capability_catalog():
        operation_text = "\n".join(
            f"{item['name']} [{item['risk']}]" for item in capability["operations"]
        )
        table.add_row(
            capability["id"],
            capability["domain"],
            capability["version"],
            operation_text,
            ", ".join(capability["composes_with"]),
        )
    console.print(table)


def cmd_validation(agent: Agent, arguments: str = "") -> None:
    parts = arguments.split()
    if not parts:
        _json_panel(_dispatch_json(agent, "list_validation_checks"), "验证检查与套件")
        return
    action = parts[0].lower()
    if action == "check" and len(parts) >= 2:
        value = _dispatch_json(
            agent,
            "run_validation_check",
            {"check": parts[1], "wait_seconds": 3},
        )
    elif action == "suite" and len(parts) >= 2:
        value = _dispatch_json(
            agent,
            "run_validation_suite",
            {"suite": parts[1], "wait_seconds": 5},
        )
    elif action == "result" and len(parts) >= 2:
        value = _dispatch_json(
            agent,
            "get_validation_result",
            {"validation_id": parts[1], "wait_seconds": 0},
        )
    else:
        value = {
            "error": "用法：/validate | /validate check <alias> | "
            "/validate suite <alias> | /validate result <val-id>"
        }
    _json_panel(value, "软件验证")


def cmd_operations(operation_id: str) -> None:
    if operation_id:
        try:
            state = operations.get_operation(operation_id)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        _json_panel(state, "运维请求")
        return
    paths = operations.queue_paths()
    rows = []
    request_paths = sorted(
        paths["requests"].glob("op-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:10]
    for request_path in request_paths:
        try:
            rows.append(operations.get_operation(request_path.stem))
        except ValueError:
            continue
    _json_panel(rows, "最近运维请求")


def cmd_reload(agent: Agent, name: str) -> None:
    if not name:
        console.print("[red]用法：/reload <技能名>[/red]")
        return
    if agent.registry.reload(name):
        agent.configure_skill_runtimes(name)
        agent._refresh_system_prompt()
        console.print(f"[green]技能 {name} 重载成功[/green]")
    else:
        console.print(f"[red]技能 {name} 重载失败[/red]")


def cmd_newskill(agent: Agent, description: str) -> None:
    if not description:
        console.print("[red]用法：/newskill <技能描述>[/red]")
        return
    with console.status("正在生成新技能..."):
        result = agent.evolve_skill(description)
    console.print(Panel(result, title="技能进化"))


def cmd_self(agent: Agent) -> None:
    _json_panel(agent.self_snapshot(), "Agenelf 可观测自我模型")


def cmd_assess(agent: Agent) -> None:
    _json_panel(agent.self_assess(), "Agenelf 当前能力评估")


def cmd_mind(agent: Agent) -> None:
    _json_panel(agent.self_development_status(), "Agenelf 持续成长状态")


def cmd_reflect(agent: Agent, arguments: str = "") -> None:
    text = arguments.strip()
    deep = False
    if text.startswith("--deep"):
        deep = True
        text = text[len("--deep") :].strip()
    with console.status("Agenelf 正在基于证据复盘并沉淀..."):
        result = agent.reflect_and_sediment(note=text, deep=deep)
    _json_panel(result, "自我反思与沉淀")


def cmd_intentions(agent: Agent, arguments: str = "") -> None:
    status = arguments.strip()
    _json_panel(
        agent.improvement_intentions(status=status, limit=50),
        "改进意向",
    )


def cmd_intend(agent: Agent, arguments: str) -> None:
    text = arguments.strip()
    if not text:
        console.print("[red]用法：/intend [P0|P1|P2|P3] <改进目标>[/red]")
        return
    parts = text.split(maxsplit=1)
    priority = "P2"
    title = text
    if parts[0].upper() in {"P0", "P1", "P2", "P3"}:
        priority = parts[0].upper()
        title = parts[1] if len(parts) == 2 else ""
    if not title.strip():
        console.print("[red]改进目标不能为空[/red]")
        return
    _json_panel(
        agent.create_improvement_intention(
            title=title,
            rationale="由主人或对话显式建立",
            priority=priority,
            acceptance_criteria=[
                "结果有自动化测试或其他可复现证据",
                "不绕过安全门、审批与主人私有数据边界",
            ],
        ),
        "新建改进意向",
    )


def cmd_pursue(agent: Agent, arguments: str) -> None:
    parts = arguments.split()
    if not parts:
        console.print("[red]用法：/pursue <intent-id> [--apply][/red]")
        return
    intention_id = next((item for item in parts if item.startswith("intent-")), "")
    if not intention_id:
        console.print("[red]必须提供 intent- 开头的意向 ID[/red]")
        return
    apply_changes = "--apply" in parts
    title = "意向沙盒推进结果" if apply_changes else "意向推进计划"
    with console.status("Agenelf 正在推进选定意向..."):
        result = agent.pursue_improvement_intention(
            intention_id,
            apply_changes=apply_changes,
        )
    _json_panel(result, title)


def cmd_autonomy(agent: Agent, arguments: str, *, force_apply: bool = False) -> None:
    text = arguments.strip()
    plan_only = text.startswith("--plan-only")
    if plan_only:
        text = text[len("--plan-only") :].strip()
    apply_changes = force_apply or not plan_only
    title = "自主迭代结果" if apply_changes else "自主改进计划"
    with console.status("Agenelf 正在观察、反思并生成受控改进..."):
        result = agent.run_autonomy_cycle(goal=text, apply_changes=apply_changes)
    _json_panel(result, title)


def cmd_remember(agent: Agent, arguments: str) -> None:
    parts = arguments.split(maxsplit=1)
    if len(parts) != 2 or parts[0] not in {"fact", "preference"}:
        console.print("[red]用法：/remember <fact|preference> <内容>[/red]")
        return
    _json_panel(agent.remember_owner(parts[0], parts[1]), "主人记忆")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf CLI")
    parser.add_argument("--mock", action="store_true", help="强制使用 MockLLM")
    parser.add_argument("--config", default=None, help="配置文件路径")
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    if arguments.mock:
        config["mock"] = True

    # Direct invocations such as
    # `docker compose exec agenelf python /agenelf/app-fork/cli.py` must preserve
    # the same restart-continuation semantics as `make chat`.
    if os.environ.get("AGENELF_SKIP_AUTO_RESUME", "0") != "1":
        resume_pending_task(
            config_loader=lambda **_: dict(config),
            emit=lambda text: console.print(str(text)),
        )

    agent = Agent(config)
    console.print(
        Panel(
            f"模型：[cyan]{agent.llm.model}[/cyan] | 技能：[green]{len(agent.registry.skills)}[/green] | 能力域：[green]{len(agent.registry.capability_catalog())}[/green]\n"
            "命令：/self /assess /scorecard /roadmap /mind /reflect [--deep] /intentions /intend /pursue /validate /autonomy /local /remember /recall /ops /approvals /approve /deny /skills /capabilities /quit",
            title="Agenelf",
        )
    )

    while True:
        try:
            user_input = console.input("[bold blue]你 > [/bold blue]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]再见！[/yellow]")
            break
        if not user_input:
            continue

        # This is evaluated before Agent.chat. Only raw terminal input can authorize an
        # exact request; assistant output and tool calls never reach this branch.
        if handle_owner_decision(
            agent=agent,
            raw_input=user_input,
            console=console,
            config=config,
        ):
            continue

        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            command = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""
            if command == "/quit":
                console.print("[yellow]再见！[/yellow]")
                break
            if command == "/skills":
                cmd_skills(agent)
            elif command == "/capabilities":
                cmd_capabilities(agent)
            elif command == "/local":
                _json_panel(agent.local_status(), "local 个性化配置")
            elif command == "/local-reload":
                _json_panel(agent.reload_local_context(), "local 重新加载")
            elif command == "/remember":
                cmd_remember(agent, rest)
            elif command == "/recall":
                _json_panel(
                    {"query": rest, "results": agent.recall_owner(rest)},
                    "主人记忆检索",
                )
            elif command == "/self":
                cmd_self(agent)
            elif command == "/assess":
                cmd_assess(agent)
            elif command == "/mind":
                cmd_mind(agent)
            elif command == "/scorecard":
                _json_panel(agent.capability_health(), "可信能力健康评分")
            elif command == "/roadmap":
                _json_panel(agent.improvement_roadmap(limit=20), "证据驱动改进路线图")
            elif command == "/reflect":
                cmd_reflect(agent, rest)
            elif command == "/intentions":
                cmd_intentions(agent, rest)
            elif command == "/intend":
                cmd_intend(agent, rest)
            elif command == "/pursue":
                cmd_pursue(agent, rest)
            elif command == "/autonomy":
                cmd_autonomy(agent, rest)
            elif command == "/validate":
                cmd_validation(agent, rest)
            elif command == "/ops":
                cmd_operations(rest)
            elif command == "/approvals":
                show_pending(console)
            elif command == "/reload":
                cmd_reload(agent, rest)
            elif command == "/newskill":
                cmd_newskill(agent, rest)
            elif command == "/memory":
                console.print(Panel(agent.memory.as_prompt_block(), title="长期记忆"))
            elif command == "/evolve":
                if not rest.strip():
                    console.print("[red]用法：/evolve <进化目标>[/red]")
                else:
                    cmd_autonomy(agent, rest, force_apply=True)
            else:
                console.print(f"[red]未知命令 {command}[/red]")
            continue
        with console.status("Agenelf 思考中..."):
            reply = agent.chat(user_input, subject="cli")
        console.print(Panel(Markdown(reply), title="Agenelf", border_style="green"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
