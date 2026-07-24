"""Interactive Agenelf CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from core import operations
from core.agent import Agent

console = Console()
_APP_DIR = Path(__file__).resolve().parent


def load_config(path: str | None = None) -> dict:
    config_path = Path(path).resolve() if path else _APP_DIR / "config.yaml"
    if not config_path.exists():
        console.print(f"[yellow]配置文件 {config_path} 不存在，使用默认配置[/yellow]")
        config: dict = {}
    else:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    llm = config.setdefault("llm", {})
    if os.environ.get("OPENAI_API_KEY"):
        llm["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("OPENAI_BASE_URL"):
        llm["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("AGENELF_MODEL"):
        llm["model"] = os.environ["AGENELF_MODEL"]
    if os.environ.get("AGENELF_MOCK") == "1":
        config["mock"] = True
    config.setdefault("skills_dir", str(_APP_DIR / "skills"))
    config.setdefault("persona_path", str(_APP_DIR / "persona" / "persona.yaml"))
    config.setdefault("memory_path", str(_APP_DIR / "memory_store" / "memory.json"))
    return config


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
        console.print("[red]以下技能加载失败：[/red]")
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


def cmd_operations(operation_id: str) -> None:
    if operation_id:
        try:
            state = operations.get_operation(operation_id)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        console.print(Panel(json.dumps(state, ensure_ascii=False, indent=2), title="运维请求"))
        return
    paths = operations.queue_paths()
    rows = []
    for request_path in sorted(
        paths["requests"].glob("op-*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )[:10]:
        try:
            rows.append(operations.get_operation(request_path.stem))
        except ValueError:
            continue
    console.print(Panel(json.dumps(rows, ensure_ascii=False, indent=2), title="最近运维请求"))


def cmd_reload(agent: Agent, name: str) -> None:
    if not name:
        console.print("[red]用法：/reload <技能名>[/red]")
        return
    if agent.registry.reload(name):
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


def cmd_evolve(agent: Agent, goal: str) -> None:
    if not goal:
        console.print("[red]用法：/evolve <进化目标>[/red]")
        return
    try:
        from evolution.engine import EvolutionEngine

        with console.status("进化引擎运行中..."):
            result = EvolutionEngine(agent).evolve(goal)
        console.print(Panel(str(result), title="进化结果"))
    except ImportError:
        console.print("[yellow]进化引擎未就绪[/yellow]")
    except Exception as exc:
        console.print(f"[red]进化引擎执行出错：{exc}[/red]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf CLI")
    parser.add_argument("--mock", action="store_true", help="强制使用 MockLLM")
    parser.add_argument("--config", default=None, help="配置文件路径")
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    if arguments.mock:
        config["mock"] = True
    agent = Agent(config)
    console.print(
        Panel(
            f"模型：[cyan]{agent.llm.model}[/cyan] | "
            f"技能：[green]{len(agent.registry.skills)}[/green] | "
            f"能力域：[green]{len(agent.registry.capability_catalog())}[/green]\n"
            "命令：/skills /capabilities /ops [ID] /reload /newskill /memory /evolve /quit",
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
            elif command == "/ops":
                cmd_operations(rest)
            elif command == "/reload":
                cmd_reload(agent, rest)
            elif command == "/newskill":
                cmd_newskill(agent, rest)
            elif command == "/memory":
                console.print(Panel(agent.memory.as_prompt_block(), title="长期记忆"))
            elif command == "/evolve":
                cmd_evolve(agent, rest)
            else:
                console.print(f"[red]未知命令 {command}[/red]")
            continue
        with console.status("Agenelf 思考中..."):
            reply = agent.chat(user_input)
        console.print(Panel(Markdown(reply), title="Agenelf", border_style="green"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
