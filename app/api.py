"""FastAPI entrypoint for chat, capability discovery and operation status."""

from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from core import operations  # noqa: E402
from core.agent import Agent  # noqa: E402


def _runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else _APP_DIR.parent


def load_config() -> dict:
    config_path = _APP_DIR / "config.yaml"
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            config: dict = yaml.safe_load(handle) or {}
    else:
        config = {}
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


_agent: Agent | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent(load_config())
    return _agent


def require_api_token(x_agenelf_token: str | None = Header(default=None)) -> None:
    """Require a token when configured; localhost-only remains the safe default."""

    expected = os.environ.get("AGENELF_API_TOKEN", "")
    if expected and not hmac.compare_digest(x_agenelf_token or "", expected):
        raise HTTPException(status_code=401, detail="无效的 Agenelf API Token")


app = FastAPI(title="Agenelf API", version="0.2.0")


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


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
    return {
        "status": "ok",
        "skills": len(agent.registry.skills),
        "capabilities": len(agent.registry.capability_catalog()),
        "model": agent.llm.model,
        "api_auth_enabled": bool(os.environ.get("AGENELF_API_TOKEN")),
    }


@app.get("/capabilities", dependencies=[Depends(require_api_token)])
def capabilities() -> dict:
    return {"capabilities": get_agent().registry.capability_catalog()}


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
                    "markers": sorted(path.name for path in entry.iterdir() if path.is_file()),
                }
            )
    return {"root": str(root), "session": session, "promotion_requests": requests}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
