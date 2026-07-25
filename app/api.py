"""FastAPI entrypoint for chat, personalization, operations and growth."""

from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from core import operations, validation  # noqa: E402
from core.agent import Agent  # noqa: E402
from core.configuration import load_config as load_shared_config  # noqa: E402
from core.self_development import SelfDevelopmentError  # noqa: E402


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
    expected = os.environ.get("AGENELF_API_TOKEN", "")
    if expected and not hmac.compare_digest(x_agenelf_token or "", expected):
        raise HTTPException(status_code=401, detail="无效的 Agenelf API Token")


app = FastAPI(title="Agenelf API", version="0.6.0")


class ChatRequest(BaseModel):
    message: str


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
    text = get_agent().registry.dispatch(tool_name, args or {})
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
    try:
        reply = get_agent().chat(request.message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"对话处理失败：{exc}") from exc
    return ChatResponse(reply=reply)


@app.get("/health")
def health() -> dict:
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
    requests: list[dict] = []
    requests_dir = data_dir / "promote-requests"
    if requests_dir.is_dir():
        entries = [path for path in requests_dir.iterdir() if path.is_dir()]
        entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for entry in entries[:10]:
            requests.append(
                {
                    "id": entry.name,
                    "markers": sorted(
                        path.name for path in entry.iterdir() if path.is_file()
                    ),
                }
            )
    return {"root": str(root), "session": session, "promotion_requests": requests}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
