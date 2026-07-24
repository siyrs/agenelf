"""Agenelf 命令行入口。

用法：
    python cli.py          # 进入对话循环（无 API Key 时自动启用 MockLLM）
    python cli.py --mock   # 强制使用 MockLLM

斜杠命令：
    /skills             列出已加载技能
    /reload <name>      热重载指定技能
    /newskill <描述>    让 LLM 生成并注册新技能
    /memory             查看长期记忆
    /evolve <目标>      调用进化引擎自我进化（引擎未合并时优雅提示）
    /quit               退出
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from core.agent import Agent

console = Console()


def load_config(path: str = "config.yaml") -> dict:
    """加载 YAML 配置；文件缺失时返回内置默认配置。"""
    if not os.path.exists(path):
        console.print(f"[yellow]配置文件 {path} 不存在，使用默认配置[/yellow]")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cmd_skills(agent: Agent) -> None:
    """列出所有已加载技能及其工具。"""
    table = Table(title="已加载技能")
    table.add_column("技能", style="cyan")
    table.add_column("版本", style="green")
    table.add_column("描述")
    table.add_column("工具", style="magenta")
    for name, module in agent.registry.skills.items():
        meta = getattr(module, "SKILL_META", {})
        tools = ", ".join(
            t.get("function", {}).get("name", "?")
            for t in getattr(module, "TOOLS", [])
        )
        table.add_row(
            name,
            str(meta.get("version", "?")),
            str(meta.get("description", "")),
            tools,
        )
    console.print(table)
    if agent.registry.errors:
        console.print("[red]以下技能加载失败：[/red]")
        for name, err in agent.registry.errors.items():
            console.print(f"  - {name}: {err.splitlines()[-1] if err else '未知错误'}")


def cmd_reload(agent: Agent, name: str) -> None:
    """热重载指定技能。"""
    if not name:
        console.print("[red]用法：/reload <技能名>[/red]")
        return
    if agent.registry.reload(name):
        console.print(f"[green]技能 {name} 重载成功[/green]")
    else:
        console.print(f"[red]技能 {name} 重载失败[/red]")


def cmd_newskill(agent: Agent, description: str) -> None:
    """让 LLM 按技能协议生成新技能并注册。"""
    if not description:
        console.print("[red]用法：/newskill <技能描述>[/red]")
        return
    with console.status("正在生成新技能..."):
        result = agent.evolve_skill(description)
    console.print(Panel(result, title="技能进化"))


def cmd_memory(agent: Agent) -> None:
    """展示长期记忆内容。"""
    console.print(Panel(agent.memory.as_prompt_block(), title="长期记忆"))


def cmd_evolve(agent: Agent, goal: str) -> None:
    """调用进化引擎；引擎模块尚未合并时优雅降级提示。"""
    if not goal:
        console.print("[red]用法：/evolve <进化目标>[/red]")
        return
    try:
        # import 放在函数体内：模块不存在时仅影响本命令
        from evolution.engine import EvolutionEngine

        engine = EvolutionEngine(agent)
        with console.status("进化引擎运行中..."):
            result = engine.evolve(goal)
        console.print(Panel(str(result), title="进化结果"))
    except ImportError:
        console.print("[yellow]进化引擎未就绪（evolution.engine 尚未合并），请稍后再试[/yellow]")
    except Exception as e:  # 引擎内部错误不应使 CLI 崩溃
        console.print(f"[red]进化引擎执行出错：{e}[/red]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agenelf CLI")
    parser.add_argument("--mock", action="store_true", help="强制使用 MockLLM")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.mock:
        config["mock"] = True

    agent = Agent(config)
    # MockLLM 的 model 固定为 "mock-llm"，直接展示即可
    llm_label = agent.llm.model
    console.print(
        Panel(
            f"模型：[cyan]{llm_label}[/cyan] | "
            f"技能数：[green]{len(agent.registry.skills)}[/green]\n"
            "输入 /skills /reload /newskill /memory /evolve /quit 使用命令",
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

        # 斜杠命令分发
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""
            if cmd == "/quit":
                console.print("[yellow]再见！[/yellow]")
                break
            elif cmd == "/skills":
                cmd_skills(agent)
            elif cmd == "/reload":
                cmd_reload(agent, rest)
            elif cmd == "/newskill":
                cmd_newskill(agent, rest)
            elif cmd == "/memory":
                cmd_memory(agent)
            elif cmd == "/evolve":
                cmd_evolve(agent, rest)
            else:
                console.print(f"[red]未知命令 {cmd}[/red]")
            continue

        with console.status("Agenelf 思考中..."):
            reply = agent.chat(user_input)
        console.print(Panel(Markdown(reply), title="Agenelf", border_style="green"))


if __name__ == "__main__":
    sys.exit(main())
