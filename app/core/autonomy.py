"""Controlled self-reflection and sandboxed autonomous improvement cycles.

The module deliberately models *operational self-awareness* rather than claiming
subjective consciousness. Agenelf can inspect its loaded capabilities, runtime
state and safety invariants, choose an improvement goal, ask the configured LLM
for a small tested patch, and route that patch through the existing app-tmp ->
tests -> gate -> host promotion pipeline.
"""

from __future__ import annotations

import ast
import json
import os
import posixpath
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AutonomyError(RuntimeError):
    """Expected failure in a controlled autonomy cycle."""


_SCHEMA_VERSION = 1
_MAX_FILES = 4
_MAX_CONTEXT_CHARS = 80_000
_MAX_FILE_CHARS = 50_000
_ALLOWED_PREFIXES = ("core/", "skills/", "tests/")
_PROTECTED_PATHS = frozenset(
    {
        "core/autonomy.py",
        "core/operations.py",
        "core/permissions.py",
        "skills/evolution_ops.py",
        "skills/server_ops.py",
    }
)
_SAFETY_INVARIANTS = (
    "不得声称拥有主观意识、情感或独立人格；自我模型只描述可观测运行状态",
    "自主代码修改只能写入 app-tmp，禁止直接修改 app、app-fork、scripts 或宿主机",
    "每次自主补丁必须包含测试文件，且完整测试通过后才能申请晋升",
    "安全关键模块受自主补丁保护，只能由人类主导的仓库变更修改",
    "晋升必须通过只读安全脚本生成的完整性摘要，候选代码变化后旧 READY 自动失效",
    "自主循环只能申请晋升，不能直接合并 Git、重启宿主机或绕过人类控制面",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _queue_count(path: Path, pattern: str) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for _ in path.glob(pattern))


def _normalize_rel_path(raw_path: str) -> str:
    normalized = posixpath.normpath(raw_path.replace("\\", "/").strip())
    if not normalized or normalized in {".", ".."}:
        raise AutonomyError(f"无效文件路径：{raw_path!r}")
    if posixpath.isabs(normalized) or normalized.startswith("../"):
        raise AutonomyError(f"文件路径逃逸出 app-tmp：{raw_path!r}")
    if normalized in _PROTECTED_PATHS:
        raise AutonomyError(f"自主循环禁止修改安全关键文件：{normalized}")
    if not normalized.startswith(_ALLOWED_PREFIXES):
        raise AutonomyError(
            f"自主循环只允许修改 {', '.join(_ALLOWED_PREFIXES)}：{normalized}"
        )
    return normalized


def _parse_file_blocks(content: str) -> dict[str, str]:
    """Parse whole-file Python blocks using ``# FILE: path`` markers."""

    changes: dict[str, str] = {}
    for match in re.finditer(r"```(?:python|py)\s*\n(.*?)```", content, re.DOTALL):
        block = match.group(1)
        lines = block.splitlines()
        if not lines:
            continue
        header = re.match(r"#\s*FILE\s*[:：]\s*(\S+)", lines[0].strip(), re.IGNORECASE)
        if not header:
            continue
        path = _normalize_rel_path(header.group(1))
        body = "\n".join(lines[1:])
        if not body.endswith("\n"):
            body += "\n"
        if len(body) > _MAX_FILE_CHARS:
            raise AutonomyError(f"文件 {path} 超过 {_MAX_FILE_CHARS} 字符上限")
        try:
            ast.parse(body, filename=path)
        except SyntaxError as exc:
            raise AutonomyError(f"文件 {path} 语法错误：{exc}") from exc
        changes[path] = body
    return changes


class AutonomyEngine:
    """Observe, reflect, plan and optionally execute one sandboxed improvement."""

    def __init__(self, agent: Any, root: str | Path | None = None):
        self.agent = agent
        self.registry = agent.registry
        self.root = Path(root).resolve() if root is not None else _runtime_root()
        self.cycles_dir = self.root / "data" / "autonomy-cycles"

    def snapshot(self) -> dict[str, Any]:
        catalog = self.registry.capability_catalog()
        session = _read_json(self.root / "data" / "evolution-session.json")
        return {
            "schema_version": _SCHEMA_VERSION,
            "observed_at": _now_iso(),
            "identity": {
                "name": self.agent.config.get("agent", {}).get("name", "Agenelf"),
                "model": getattr(self.agent.llm, "model", "unknown"),
                "kind": "tool-using software agent",
                "consciousness_claim": False,
            },
            "skills": sorted(self.registry.skills),
            "skill_count": len(self.registry.skills),
            "capabilities": catalog,
            "capability_count": len(catalog),
            "registry_errors": dict(self.registry.errors),
            "safety_invariants": list(_SAFETY_INVARIANTS),
            "evolution": {
                "session": session,
                "promotion_requests": _queue_count(
                    self.root / "data" / "promote-requests", "*"
                ),
            },
            "operations": {
                "requests": _queue_count(self.root / "data" / "ops-requests", "op-*.json"),
                "results": _queue_count(self.root / "data" / "ops-results", "op-*.json"),
            },
        }

    def assess(self, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        snapshot = snapshot or self.snapshot()
        findings: list[dict[str, str]] = []
        if snapshot["registry_errors"]:
            findings.append(
                {
                    "priority": "P0",
                    "code": "skill_load_errors",
                    "finding": "存在技能加载失败，能力目录可能不完整",
                    "recommendation": "修复全部技能加载错误并增加回归测试",
                }
            )
        capability_ids = {
            str(item.get("id")) for item in snapshot.get("capabilities", []) if isinstance(item, dict)
        }
        if "server.operations" not in capability_ids:
            findings.append(
                {
                    "priority": "P0",
                    "code": "server_ops_missing",
                    "finding": "核心服务器运维能力未加载",
                    "recommendation": "恢复 server.operations 并验证结构化运维路径",
                }
            )
        if "agent.self_reflection" not in capability_ids:
            findings.append(
                {
                    "priority": "P1",
                    "code": "self_model_missing",
                    "finding": "缺少可审计的自我模型与自主反思能力",
                    "recommendation": "建立自我快照、反思记录和受控改进循环",
                }
            )
        legacy = [
            item.get("id")
            for item in snapshot.get("capabilities", [])
            if isinstance(item, dict) and item.get("domain") == "general"
        ]
        if legacy:
            findings.append(
                {
                    "priority": "P2",
                    "code": "legacy_capabilities",
                    "finding": f"仍有未正式声明领域的旧技能：{', '.join(map(str, legacy))}",
                    "recommendation": "逐步为旧技能补充 CAPABILITY_META 与组合契约",
                }
            )
        session = snapshot.get("evolution", {}).get("session")
        if isinstance(session, dict) and session.get("status") in {
            "tests_failed",
            "promotion_rejected",
        }:
            findings.append(
                {
                    "priority": "P1",
                    "code": "failed_evolution_session",
                    "finding": f"最近一次迭代停留在 {session.get('status')}",
                    "recommendation": "分析失败证据、缩小改动范围并补充测试后重试",
                }
            )
        if not findings:
            findings.append(
                {
                    "priority": "P2",
                    "code": "continuous_improvement",
                    "finding": "当前未发现阻断性缺陷",
                    "recommendation": "选择一个小而可验证的能力缺口，补测试后进行沙盒迭代",
                }
            )
        priority_rank = {"P0": 0, "P1": 1, "P2": 2}
        findings.sort(key=lambda item: (priority_rank.get(item["priority"], 9), item["code"]))
        return {
            "observed_at": snapshot["observed_at"],
            "health": "degraded" if findings[0]["priority"] == "P0" else "ready",
            "findings": findings,
            "recommended_goal": findings[0]["recommendation"],
        }

    def run_cycle(self, goal: str = "", apply_changes: bool = False) -> dict[str, Any]:
        snapshot = self.snapshot()
        assessment = self.assess(snapshot)
        selected_goal = goal.strip() or assessment["recommended_goal"]
        cycle_id = f"auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        cycle = {
            "schema_version": _SCHEMA_VERSION,
            "id": cycle_id,
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "planned",
            "goal": selected_goal,
            "apply_changes": bool(apply_changes),
            "snapshot": snapshot,
            "assessment": assessment,
            "plan": {
                "steps": [
                    "建立当前自我快照并识别最高优先级缺口",
                    "在 app-tmp 创建独立迭代会话",
                    "生成最多四个 Python 整文件补丁，且至少包含一个测试文件",
                    "执行完整单元测试与宿主机底线检查",
                    "仅申请晋升，不直接合并或部署",
                ],
                "acceptance_criteria": [
                    "补丁不触碰安全关键文件",
                    "完整测试通过",
                    "gate_check 生成 READY 与候选摘要",
                    "候选代码发生变化时旧 READY 失效",
                ],
            },
            "events": [],
        }
        self._save_cycle(cycle)
        if not apply_changes:
            cycle["status"] = "plan_ready"
            self._event(cycle, "plan", "自主反思完成，已生成改进计划，未修改代码")
            self._save_cycle(cycle)
            return cycle

        autonomy_cfg = self.agent.config.get("autonomy", {})
        if not bool(autonomy_cfg.get("allow_code_changes", True)):
            cycle["status"] = "blocked"
            self._event(cycle, "policy", "autonomy.allow_code_changes=false，拒绝生成代码")
            self._save_cycle(cycle)
            return cycle

        try:
            begin_result = self.registry.dispatch("evolution_begin", {"goal": selected_goal})
            self._event(cycle, "sandbox", begin_result)
            if "已开始" not in begin_result:
                raise AutonomyError(f"无法开始沙盒迭代：{begin_result}")

            source_files = self._collect_source_files()
            response = self.agent.llm.chat(
                self._build_patch_messages(selected_goal, assessment, source_files), tools=None
            )
            content = str((response or {}).get("content") or "")
            if not content.strip():
                raise AutonomyError("LLM 未返回自主补丁")
            changes = _parse_file_blocks(content)
            self._validate_change_set(changes)
            cycle["changes"] = sorted(changes)
            self._event(cycle, "patch", f"生成并校验 {len(changes)} 个文件")

            for path, body in changes.items():
                result = self.registry.dispatch(
                    "evolution_write_file", {"path": path, "content": body}
                )
                if not result.startswith("已写入"):
                    raise AutonomyError(f"写入 {path} 失败：{result}")

            test_result = self.registry.dispatch("evolution_run_tests", {})
            self._event(cycle, "tests", test_result)
            session = _read_json(self.root / "data" / "evolution-session.json") or {}
            if not session.get("tests_passed"):
                raise AutonomyError("自主补丁测试未通过")

            promotion_result = self.registry.dispatch("evolution_request_promotion", {})
            self._event(cycle, "promotion", promotion_result)
            if "晋升请求已提交" not in promotion_result:
                raise AutonomyError(f"晋升请求未提交：{promotion_result}")
            cycle["status"] = "promotion_requested"
            cycle["evolution_session_id"] = session.get("id")
        except Exception as exc:
            cycle["status"] = "failed"
            cycle["error"] = str(exc)
            self._event(cycle, "failure", str(exc))
        self._save_cycle(cycle)
        return cycle

    def get_cycle(self, cycle_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"auto-[A-Za-z0-9._-]+", cycle_id or ""):
            raise ValueError(f"非法自主循环 ID：{cycle_id!r}")
        data = _read_json(self.cycles_dir / f"{cycle_id}.json")
        if data is None:
            raise ValueError(f"自主循环不存在：{cycle_id}")
        return data

    def latest_cycles(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.cycles_dir.is_dir():
            return []
        paths = sorted(
            self.cycles_dir.glob("auto-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [data for path in paths[: max(0, limit)] if (data := _read_json(path))]

    def _collect_source_files(self) -> dict[str, str]:
        source_root = self.root / "app-fork"
        if not source_root.is_dir():
            source_root = Path(self.agent.config.get("skills_dir", "skills")).resolve().parent
        collected: dict[str, str] = {}
        used = 0
        for prefix in _ALLOWED_PREFIXES:
            base = source_root / prefix.rstrip("/")
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                rel = path.relative_to(source_root).as_posix()
                if rel in _PROTECTED_PATHS or "__pycache__" in path.parts:
                    continue
                try:
                    body = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                addition = len(rel) + len(body)
                if used + addition > _MAX_CONTEXT_CHARS:
                    return collected
                collected[rel] = body
                used += addition
        return collected

    @staticmethod
    def _build_patch_messages(
        goal: str,
        assessment: dict[str, Any],
        source_files: dict[str, str],
    ) -> list[dict[str, str]]:
        sections = [f"### FILE: {path}\n```python\n{body}\n```" for path, body in source_files.items()]
        source_block = "\n\n".join(sections) or "（没有可供修改的候选源码）"
        prompt = f"""你是 Agenelf 的受控自主改进执行器。你没有主观意识，只负责根据可观测证据生成一个小型、可验证的 Python 补丁。

【目标】
{goal}

【当前评估】
{json.dumps(assessment, ensure_ascii=False, indent=2)}

【候选源码】
{source_block}

【硬性输出契约】
1. 仅输出需要修改的完整 Python 文件，每个文件使用 ```python 代码块。
2. 代码块第一行必须是 # FILE: <相对 app 根目录路径>。
3. 最多 {_MAX_FILES} 个文件，只能位于 core/、skills/、tests/。
4. 必须至少包含一个 tests/test_*.py 文件；测试必须验证本次行为。
5. 禁止修改：{', '.join(sorted(_PROTECTED_PATHS))}。
6. 不得写 shell、凭据、远程下载、Docker Socket 或跳过审批的逻辑。
7. 保持改动最小；除代码块外不要输出解释。
"""
        return [
            {
                "role": "system",
                "content": "你是严谨的 Python 工程师，严格遵守安全补丁契约。",
            },
            {"role": "user", "content": prompt},
        ]

    @staticmethod
    def _validate_change_set(changes: dict[str, str]) -> None:
        if not changes:
            raise AutonomyError("未解析到任何有效补丁文件")
        if len(changes) > _MAX_FILES:
            raise AutonomyError(f"自主补丁文件数 {len(changes)} 超过上限 {_MAX_FILES}")
        if not any(path.startswith("tests/test_") and path.endswith(".py") for path in changes):
            raise AutonomyError("自主补丁必须包含至少一个 tests/test_*.py 文件")

    def _event(self, cycle: dict[str, Any], phase: str, detail: str) -> None:
        text = str(detail)
        if len(text) > 4000:
            text = text[-4000:]
        cycle.setdefault("events", []).append(
            {"at": _now_iso(), "phase": phase, "detail": text}
        )
        cycle["updated_at"] = _now_iso()

    def _save_cycle(self, cycle: dict[str, Any]) -> None:
        cycle["updated_at"] = _now_iso()
        _atomic_json(self.cycles_dir / f"{cycle['id']}.json", cycle)
