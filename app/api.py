"""FastAPI entrypoint for chat, personalization, operations and growth."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from core import approval_catalog, operations, owner_approval, validation  # noqa: E402
from core.agent import Agent  # noqa: E402
from core.channel_envelope import CHANNELS  # noqa: E402
from core.configuration import load_config as load_shared_config  # noqa: E402
from core.self_development import SelfDevelopmentError  # noqa: E402
from core.task_engine import TaskEngine  # noqa: E402
from skills import task_board  # noqa: E402
from skills.evolution_ops import merged_promotion_requests  # noqa: E402

logger = logging.getLogger(__name__)


def _runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else _APP_DIR.parent


def load_config() -> dict:
    return load_shared_config(app_dir=_APP_DIR)


_agent: Agent | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent(load_config())
    return _agent


def require_api_token(x_agenelf_token: str | None = Header(default=None)) -> None:
    """Fail-closed bearer check for every protected endpoint.

    - ``AGENELF_API_TOKEN`` 已配置：必须携带匹配的 ``X-Agenelf-Token``，否则 401；
    - ``AGENELF_API_TOKEN`` 未配置：默认拒绝服务（503），提示管理员配置 token；
    - 仅开发调试可显式设置 ``AGENELF_API_ALLOW_INSECURE=1`` 恢复旧的免鉴权行为。
    """
    expected = os.environ.get("AGENELF_API_TOKEN", "").strip()
    if expected:
        if not hmac.compare_digest(x_agenelf_token or "", expected):
            raise HTTPException(status_code=401, detail="无效的 Agenelf API Token")
        return
    if os.environ.get("AGENELF_API_ALLOW_INSECURE", "").strip() == "1":
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "AGENELF_API_TOKEN 未配置，API 拒绝服务；"
            "请管理员在 .env 中配置强随机 token 后重启"
            "（仅限本地开发可设 AGENELF_API_ALLOW_INSECURE=1 绕过）"
        ),
    )


app = FastAPI(title="Agenelf API", version="0.6.0")


def _web_dir_candidates() -> list[Path]:
    """内嵌 Web 控制台的静态目录查找顺序。

    容器内 compose 将 ./app 挂在 /agenelf/app-fork，web/ 预期以只读方式
    挂在 /agenelf/web；本地开发则取 AGENELF_ROOT/web 或仓库根 web/。
    """
    return [
        _runtime_root() / "web",
        _APP_DIR.parent / "web",
        Path("/agenelf/web"),
    ]


def _mount_web_console() -> Path | None:
    """挂载 web/ 到 /ui；目录不存在时只记 warning，不影响 API 启动。"""
    for candidate in _web_dir_candidates():
        if candidate.is_dir():
            app.mount("/ui", StaticFiles(directory=candidate, html=True), name="ui")
            logger.info("Web 控制台静态目录：%s -> /ui", candidate)
            return candidate
    logger.warning(
        "未找到 web/ 目录（已查找 %s），跳过 /ui 静态托管",
        ", ".join(str(path) for path in _web_dir_candidates()),
    )
    return None


_mount_web_console()


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """未鉴权重定向到 Web 控制台；真正的数据端点仍受 token 保护。"""
    return RedirectResponse(url="/ui/")


# Deprecated aliases kept for backward compatibility; core.channel_envelope.CHANNELS
# 是渠道枚举的唯一来源（{"cli", "http", "web", "mobile", "voice"}）。
_DEPRECATED_CHANNEL_ALIASES = {"mobile_device": "mobile"}


def _normalize_channel(value: str) -> str:
    channel = value.strip().lower()
    channel = _DEPRECATED_CHANNEL_ALIASES.get(channel, channel)
    if channel not in CHANNELS:
        allowed = "、".join(sorted(CHANNELS))
        raise HTTPException(
            status_code=400,
            detail=f"channel 只能是 {allowed}（mobile_device 已废弃，等价于 mobile）",
        )
    return channel


# 会话 ID 白名单：防止把任意字符串当作历史桶键（路径注入/日志污染）。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _normalize_session_id(value: str | None) -> str | None:
    """校验可选 session_id；None/空白视为默认桶，非法格式返回 400。"""

    if value is None:
        return None
    session_id = value.strip()
    if not session_id:
        return None
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "session_id 只能包含字母、数字、点、下划线、连字符，"
                "以字母或数字开头，长度 1-64"
            ),
        )
    return session_id


def _chat_kwargs(session_id: str | None) -> dict:
    """仅在显式提供 session_id 时传递该参数，兼容不接受此关键字的 Agent 替身。"""

    return {"session_id": session_id} if session_id is not None else {}


class ChatRequest(BaseModel):
    message: str
    channel: str = "http"
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str


class AutonomyRequest(BaseModel):
    goal: str = ""
    apply_changes: bool = False


class RememberRequest(BaseModel):
    kind: str
    content: str


class ReflectionRequest(BaseModel):
    note: str = ""
    deep: bool = False


class IntentionRequest(BaseModel):
    title: str
    rationale: str = ""
    priority: str = "P2"
    acceptance_criteria: list[str] = Field(default_factory=list)


class PursueIntentionRequest(BaseModel):
    apply_changes: bool = False


class ValidationRunRequest(BaseModel):
    wait_seconds: int = Field(default=3, ge=0, le=12)


class CodeRepairSubmitRequest(BaseModel):
    repository: str
    unified_diff: str
    test_profile: str = ""
    expected_base: str = ""
    summary: str = ""
    wait_seconds: int = Field(default=5, ge=0, le=15)


class OptimizationApplyRequest(BaseModel):
    key: str
    value: float
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class OptimizationRollbackRequest(BaseModel):
    key: str


def _dispatch_json(tool_name: str, args: dict | None = None) -> dict:
    text = get_agent().registry.dispatch(tool_name, args or {}, subject="api")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=text) from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=500, detail=f"工具 {tool_name} 未返回对象")
    return value


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_api_token)])
def chat(request: ChatRequest) -> ChatResponse:
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    channel = _normalize_channel(request.channel)
    session_id = _normalize_session_id(request.session_id)
    try:
        reply = get_agent().chat(
            request.message, subject=channel, **_chat_kwargs(session_id)
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"对话处理失败：{exc}") from exc
    return ChatResponse(reply=reply)


def _sse(event: str, payload: dict) -> str:
    """序列化一条 SSE 帧（data 为单行 JSON，换行已被转义）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunk_reply(text: str, max_len: int = 240) -> list[str]:
    """把完整回复按段落/句子切分为流式增量块；超长句硬切，保证有界。"""
    chunks: list[str] = []
    for piece in re.split(r"(?<=[。！？!?；;\n])", text or ""):
        if not piece:
            continue
        while len(piece) > max_len:
            chunks.append(piece[:max_len])
            piece = piece[max_len:]
        chunks.append(piece)
    return chunks


@app.post("/chat/stream", dependencies=[Depends(require_api_token)])
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """SSE 流式对话：status(thinking) → message(delta)×N → done；异常发 error 事件。

    请求体与 /chat 相同；参数校验（空消息、非法 channel）在流开始前返回 4xx，
    agent 处理异常则转为 ``event: error``，保持事件流格式完整。
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    channel = _normalize_channel(request.channel)
    session_id = _normalize_session_id(request.session_id)

    def events():
        yield _sse("status", {"phase": "thinking"})
        try:
            reply = get_agent().chat(
                request.message, subject=channel, **_chat_kwargs(session_id)
            )
        except Exception as exc:
            yield _sse("error", {"error": f"对话处理失败：{exc}"})
            return
        for chunk in _chunk_reply(str(reply)):
            yield _sse("message", {"delta": chunk})
        yield _sse("done", {"ok": True})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health() -> dict:
    """无鉴权存活探针：只暴露最小信息，避免向未认证方泄露内部状态。"""
    return {"status": "ok", "version": app.version}


@app.get("/status", dependencies=[Depends(require_api_token)])
def status() -> dict:
    agent = get_agent()
    local = agent.local_status()
    development = agent.self_development_status()
    capability_health = agent.capability_health()
    counts = development.get("intention_status_counts", {})
    open_count = sum(
        int(counts.get(status, 0))
        for status in (
            "proposed",
            "planned",
            "active",
            "awaiting_promotion",
            "blocked",
        )
    )
    return {
        "status": "ok",
        "skills": len(agent.registry.skills),
        "capabilities": len(agent.registry.capability_catalog()),
        "model": agent.llm.model,
        "api_auth_enabled": bool(os.environ.get("AGENELF_API_TOKEN")),
        "autonomy": "controlled-sandbox",
        "self_development": "persistent-operational",
        "open_improvement_intentions": open_count,
        "last_reflection_at": development.get("last_reflection_at"),
        "capability_health": {
            name: card.get("health")
            for name, card in capability_health.get("scorecards", {}).items()
        },
        "validation_evidence": sum(
            1
            for item in capability_health.get("recent_evidence", [])
            if item.get("capability") == "software.validation"
        ),
        "local_context_ready": bool(
            local.get("profile_loaded") or local.get("preferences_loaded")
        ),
        "local_context_warnings": len(local.get("warnings", [])),
    }


@app.get("/capabilities", dependencies=[Depends(require_api_token)])
def capabilities() -> dict:
    return {"capabilities": get_agent().registry.capability_catalog()}


@app.get("/validation/catalog", dependencies=[Depends(require_api_token)])
def validation_catalog() -> dict:
    return _dispatch_json("list_validation_checks")


@app.post("/validation/checks/{check}", dependencies=[Depends(require_api_token)])
def run_validation_check(check: str, request: ValidationRunRequest) -> dict:
    return _dispatch_json(
        "run_validation_check",
        {"check": check, "wait_seconds": min(request.wait_seconds, 8)},
    )


@app.post("/validation/suites/{suite}", dependencies=[Depends(require_api_token)])
def run_validation_suite(suite: str, request: ValidationRunRequest) -> dict:
    return _dispatch_json(
        "run_validation_suite",
        {"suite": suite, "wait_seconds": request.wait_seconds},
    )


@app.get("/validation/results/{validation_id}", dependencies=[Depends(require_api_token)])
def validation_result(
    validation_id: str,
    wait_seconds: int = Query(default=0, ge=0, le=8),
) -> dict:
    return _dispatch_json(
        "get_validation_result",
        {"validation_id": validation_id, "wait_seconds": wait_seconds},
    )


@app.get("/code-repair/catalog", dependencies=[Depends(require_api_token)])
def code_repair_catalog() -> dict:
    return _dispatch_json("list_code_repair_repositories")


@app.post("/code-repair/requests", dependencies=[Depends(require_api_token)])
def submit_code_repair(request: CodeRepairSubmitRequest) -> dict:
    return _dispatch_json(
        "submit_code_repair_patch",
        {
            "repository": request.repository,
            "unified_diff": request.unified_diff,
            "test_profile": request.test_profile,
            "expected_base": request.expected_base,
            "summary": request.summary,
            "wait_seconds": request.wait_seconds,
        },
    )


@app.get("/code-repair/requests/{repair_id}", dependencies=[Depends(require_api_token)])
def code_repair_result(
    repair_id: str,
    wait_seconds: int = Query(default=0, ge=0, le=15),
) -> dict:
    return _dispatch_json(
        "get_code_repair_result",
        {"repair_id": repair_id, "wait_seconds": wait_seconds},
    )


@app.get("/local/status", dependencies=[Depends(require_api_token)])
def local_status() -> dict:
    return get_agent().local_status()


@app.post("/local/reload", dependencies=[Depends(require_api_token)])
def local_reload() -> dict:
    return get_agent().reload_local_context()


@app.post("/memory", dependencies=[Depends(require_api_token)])
def remember(request: RememberRequest) -> dict:
    if request.kind not in {"fact", "preference"}:
        raise HTTPException(status_code=400, detail="kind 只能是 fact 或 preference")
    if not request.content.strip():
        raise HTTPException(status_code=400, detail="content 不能为空")
    return get_agent().remember_owner(request.kind, request.content)


@app.get("/memory/search", dependencies=[Depends(require_api_token)])
def search_memory(
    q: str = Query(min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    return {"query": q, "results": get_agent().recall_owner(q, limit)}


@app.get("/self", dependencies=[Depends(require_api_token)])
def self_snapshot() -> dict:
    return get_agent().self_snapshot()


@app.get("/self/assessment", dependencies=[Depends(require_api_token)])
def self_assessment() -> dict:
    return get_agent().self_assess()


@app.get("/self/capability-health", dependencies=[Depends(require_api_token)])
def self_capability_health() -> dict:
    return get_agent().capability_health()


@app.get("/self/roadmap", dependencies=[Depends(require_api_token)])
def self_roadmap(limit: int = Query(default=10, ge=1, le=50)) -> dict:
    return get_agent().improvement_roadmap(limit=limit)


@app.get("/self/development", dependencies=[Depends(require_api_token)])
def self_development() -> dict:
    return get_agent().self_development_status()


@app.post("/self/reflections", dependencies=[Depends(require_api_token)])
def create_reflection(request: ReflectionRequest) -> dict:
    try:
        return get_agent().reflect_and_sediment(
            note=request.note,
            deep=request.deep,
        )
    except SelfDevelopmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"反思沉淀失败：{exc}") from exc


@app.get("/self/reflections", dependencies=[Depends(require_api_token)])
def list_reflections(
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    return {"reflections": get_agent().self_reflections(limit=limit)}


@app.get("/self/intentions", dependencies=[Depends(require_api_token)])
def list_intentions(
    status: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    try:
        values = get_agent().improvement_intentions(status=status, limit=limit)
    except SelfDevelopmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"intentions": values}


@app.post("/self/intentions", dependencies=[Depends(require_api_token)])
def create_intention(request: IntentionRequest) -> dict:
    if not request.title.strip():
        raise HTTPException(status_code=400, detail="title 不能为空")
    try:
        return get_agent().create_improvement_intention(
            title=request.title,
            rationale=request.rationale,
            priority=request.priority,
            acceptance_criteria=request.acceptance_criteria,
        )
    except SelfDevelopmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/self/intentions/{intention_id}", dependencies=[Depends(require_api_token)])
def get_intention(intention_id: str) -> dict:
    try:
        return get_agent().get_improvement_intention(intention_id)
    except SelfDevelopmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/self/intentions/{intention_id}/pursue",
    dependencies=[Depends(require_api_token)],
)
def pursue_intention(
    intention_id: str,
    request: PursueIntentionRequest,
) -> dict:
    try:
        return get_agent().pursue_improvement_intention(
            intention_id,
            apply_changes=request.apply_changes,
        )
    except SelfDevelopmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"推进改进意向失败：{exc}") from exc


@app.get("/self/optimization", dependencies=[Depends(require_api_token)])
def self_optimization_status() -> dict:
    return get_agent().optimization.status()


@app.post("/self/optimization/apply", dependencies=[Depends(require_api_token)])
def self_optimization_apply(request: OptimizationApplyRequest) -> dict:
    applied, message = get_agent().optimization.apply(
        request.key,
        request.value,
        request.reason,
        evidence=request.evidence,
    )
    if not applied:
        raise HTTPException(status_code=400, detail=message)
    return {"applied": True, "message": message}


@app.post("/self/optimization/rollback", dependencies=[Depends(require_api_token)])
def self_optimization_rollback(request: OptimizationRollbackRequest) -> dict:
    rolled_back, message = get_agent().optimization.rollback(request.key)
    if not rolled_back:
        raise HTTPException(status_code=400, detail=message)
    return {"rolled_back": True, "message": message}


@app.post("/self/optimization/auto", dependencies=[Depends(require_api_token)])
def self_optimization_auto() -> dict:
    return get_agent().optimization.auto_tune()


@app.post("/autonomy/cycles", dependencies=[Depends(require_api_token)])
def create_autonomy_cycle(request: AutonomyRequest) -> dict:
    try:
        return get_agent().run_autonomy_cycle(
            goal=request.goal, apply_changes=request.apply_changes
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"自主循环执行失败：{exc}") from exc


@app.get("/autonomy/cycles", dependencies=[Depends(require_api_token)])
def list_autonomy_cycles() -> dict:
    return {"cycles": get_agent().autonomy_status()}


@app.get("/autonomy/cycles/{cycle_id}", dependencies=[Depends(require_api_token)])
def autonomy_cycle_status(cycle_id: str) -> dict:
    try:
        result = get_agent().autonomy_status(cycle_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    assert isinstance(result, dict)
    return result


def _request_expiry(request_id: str, root: Path) -> str:
    """尽力为待审批项补充过期时间；读不到则返回空串。"""
    if request_id.startswith("op-"):
        path = operations.queue_paths(root)["requests"] / f"{request_id}.json"
    elif request_id.startswith("auth-"):
        path = owner_approval.approval_paths(root)["auth_requests"] / f"{request_id}.json"
    else:
        return ""
    request = operations.read_json(path)
    return str(request.get("expires_at", "")) if request else ""


@app.get("/approvals", dependencies=[Depends(require_api_token)])
def pending_approvals() -> dict:
    """只读列出待审批操作/授权请求；决策只能走 CLI /approve 或宿主机脚本。"""
    root = _runtime_root()
    pending = []
    for row in approval_catalog.list_pending_requests(root):
        request_id = str(row.get("id", ""))
        pending.append(
            {
                "operation_id": request_id,
                "kind": row.get("kind", ""),
                "summary": row.get("summary", ""),
                "operation": row.get("operation", ""),
                "target": row.get("target", ""),
                "risk": row.get("risk", ""),
                "created_at": row.get("created_at", ""),
                "expires_at": _request_expiry(request_id, root),
                "fingerprint": row.get("fingerprint", ""),
            }
        )
    return {
        "pending": pending,
        "hint": (
            "本端点只读。审批只能由主人执行：CLI 内 /approve <id> 或 /deny <id>，"
            "或宿主机 scripts/approve.sh <id> approve（Windows 用 approve.ps1 / approve.py）。"
        ),
    }


@app.get("/chat/history", dependencies=[Depends(require_api_token)])
def chat_history(
    limit: int = Query(default=50, ge=1, le=200),
    session_id: str | None = Query(default=None),
) -> dict:
    """读取指定会话桶的最近 N 条历史；不带 session_id 时读取默认桶。"""

    normalized = _normalize_session_id(session_id)
    entries = get_agent().get_history(session_id=normalized, limit=limit)
    return {
        "history": entries,
        "count": len(entries),
        "session_id": normalized or "default",
        "note": "会话历史按 session_id 分桶实现多会话隔离；省略 session_id 时为默认桶",
    }


@app.delete("/chat/history", dependencies=[Depends(require_api_token)])
def delete_chat_history(session_id: str | None = Query(default=None)) -> dict:
    """清空指定会话桶；不带 session_id 时清空默认桶（不影响其它桶）。"""

    normalized = _normalize_session_id(session_id)
    cleared = get_agent().clear_history(session_id=normalized)
    return {
        "cleared": cleared,
        "session_id": normalized or "default",
    }


# ---------------------------------------------------------------------------
# 任务只读视图：合并任务板（board.json）与治理任务引擎（data/tasks/）
# ---------------------------------------------------------------------------

_TASK_ID_RE = re.compile(r"task-[A-Za-z0-9][A-Za-z0-9._-]{0,120}")


def _board_store_dir() -> Path:
    """任务板存储目录（只读探测，不创建目录），与 skills.task_board 规则一致。"""
    override = getattr(task_board, "_store_dir", None)
    if override is not None:
        return Path(override)
    root = _runtime_root()
    if (root / "workspace").is_dir() or os.environ.get("AGENELF_ROOT", "").strip():
        return root / "workspace" / "tasks"
    return _APP_DIR / "memory_store"


def _load_board_tasks() -> list[dict]:
    """读取任务板主板任务；文件缺失/损坏时容错为空列表。"""
    path = _board_store_dir() / "board.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return []
    return [t for t in value["tasks"] if isinstance(t, dict)]


def _load_engine_tasks() -> list[dict]:
    """读取治理引擎任务（data/tasks/task-*.json）；目录缺失容错为空列表。"""
    directory = _runtime_root() / "data" / "tasks"
    if not directory.is_dir():
        return []
    tasks: list[dict] = []
    for path in directory.glob("task-*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            tasks.append(item)
    return tasks


def _board_task_summary(task: dict) -> dict:
    steps = task.get("steps") or []
    done = sum(1 for s in steps if isinstance(s, dict) and s.get("status") == "done")
    return {
        "id": task.get("id"),
        "source": "board",
        "title": task.get("title"),
        "status": task.get("status"),
        "priority": task.get("priority"),
        "progress": f"{done}/{len(steps)}",
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "done_at": task.get("done_at"),
        "linked_intention": task.get("linked_intention"),
        "block_reason": task.get("block_reason") or "",
    }


def _engine_task_summary(task: dict) -> dict:
    summary = TaskEngine.summary(task)
    summary["source"] = "engine"
    summary["created_at"] = task.get("created_at")
    return summary


@app.get("/tasks", dependencies=[Depends(require_api_token)])
def list_tasks(status: str = Query(default="")) -> dict:
    """只读合并列出两个来源的任务，按 updated_at 倒序；可用 status 精确过滤。"""
    status = status.strip()
    tasks: list[dict] = []
    for raw in _load_board_tasks():
        item = _board_task_summary(raw)
        if not status or item.get("status") == status:
            tasks.append(item)
    for raw in _load_engine_tasks():
        item = _engine_task_summary(raw)
        if not status or item.get("status") == status:
            tasks.append(item)
    tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {
        "tasks": tasks,
        "count": len(tasks),
        "sources": {
            "board": "workspace/tasks/board.json（结构化任务板）",
            "engine": "data/tasks/（治理任务引擎）",
        },
    }


@app.get("/tasks/{task_id}", dependencies=[Depends(require_api_token)])
def task_detail(task_id: str) -> dict:
    """单任务完整记录；engine 任务含 events/evidence 等审计历史字段。"""
    task_id = task_id.strip()
    if not _TASK_ID_RE.fullmatch(task_id) or ".." in task_id:
        raise HTTPException(status_code=400, detail=f"非法任务 ID：{task_id!r}")
    for raw in _load_board_tasks():
        if raw.get("id") == task_id:
            return {"source": "board", "task": raw}
    for raw in _load_engine_tasks():
        if raw.get("id") == task_id:
            return {"source": "engine", "task": raw}
    raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")


@app.get("/operations/{operation_id}", dependencies=[Depends(require_api_token)])
def operation_status(operation_id: str) -> dict:
    try:
        return operations.get_operation(operation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/evolution/status", dependencies=[Depends(require_api_token)])
def evolution_status() -> dict:
    root = _runtime_root()
    data_dir = root / "data"
    session: dict | None = None
    session_path = data_dir / "evolution-session.json"
    if session_path.exists():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            session = None
    # gate_check.sh 的候选输出（app-tmp/promote-requests，可用
    # PROMOTE_REQUESTS_DIR 覆盖）与宿主机已晋升记录（data/promote-requests）
    # 合并展示，每条标注 source：candidate | promoted。
    requests = merged_promotion_requests(root, limit=10)
    return {"root": str(root), "session": session, "promotion_requests": requests}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
