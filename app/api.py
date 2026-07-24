"""FastAPI entrypoint for chat, personalization, operations and autonomy."""

from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from core import operations  # noqa: E402
from core.agent import Agent  # noqa: E402
from core.configuration import load_config as load_shared_config  # noqa: E402


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


app = FastAPI(title="Agenelf API", version="0.4.0")


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
    return {
        "status": "ok",
        "skills": len(agent.registry.skills),
        "capabilities": len(agent.registry.capability_catalog()),
        "model": agent.llm.model,
        "api_auth_enabled": bool(os.environ.get("AGENELF_API_TOKEN")),
        "autonomy": "controlled-sandbox",
        "local_context_ready": bool(
            local.get("profile_loaded") or local.get("preferences_loaded")
        ),
        "local_context_warnings": len(local.get("warnings", [])),
    }


@app.get("/capabilities", dependencies=[Depends(require_api_token)])
def capabilities() -> dict:
    return {"capabilities": get_agent().registry.capability_catalog()}


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
