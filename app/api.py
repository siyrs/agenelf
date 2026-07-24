"""Agenelf HTTP 交互入口（第二个入口，第一个为 cli.py）。

基于 FastAPI + uvicorn 提供 HTTP 接口：
- POST /chat              对话：{"message": str} -> {"reply": str}
- GET  /health            健康检查：{"status": "ok", "skills": n, "model": ...}
- GET  /evolution/status  自我迭代晋升管道状态（会话 + promote-requests）

配置来源：app/config.yaml + 环境变量覆盖
- OPENAI_API_KEY   覆盖 llm.api_key
- OPENAI_BASE_URL  覆盖 llm.base_url
- AGENELF_MODEL    覆盖 llm.model
- AGENELF_MOCK=1   强制使用 MockLLM（无需真实 API Key）

运行方式：
    cd app && uvicorn api:app --host 0.0.0.0 --port 8000
    或  python app/api.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 保证无论从哪个目录启动（uvicorn api:app / python app/api.py / 测试导入）
# 都能以 app/ 为基准导入 core 包
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from core.agent import Agent  # noqa: E402


# ----------------------------------------------------------------------
# 配置加载
# ----------------------------------------------------------------------
def _runtime_root() -> Path:
    """获取运行时根目录：AGENELF_ROOT 优先，否则取 app/ 的上一级。"""
    env_root = os.environ.get("AGENELF_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    return _APP_DIR.parent


def load_config() -> dict:
    """加载 app/config.yaml 并应用环境变量覆盖，返回完整配置。"""
    config_path = _APP_DIR / "config.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config: dict = yaml.safe_load(f) or {}
    else:
        config = {}

    # 环境变量覆盖 LLM 配置
    llm_cfg = config.setdefault("llm", {})
    if os.environ.get("OPENAI_API_KEY"):
        llm_cfg["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("OPENAI_BASE_URL"):
        llm_cfg["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("AGENELF_MODEL"):
        llm_cfg["model"] = os.environ["AGENELF_MODEL"]
    if os.environ.get("AGENELF_MOCK") == "1":
        # 强制 mock：无需真实 API Key，用于本地开发与测试
        config["mock"] = True

    # 路径类配置锚定到 app/ 目录，避免依赖启动时的工作目录
    config.setdefault("skills_dir", str(_APP_DIR / "skills"))
    config.setdefault("persona_path", str(_APP_DIR / "persona" / "persona.yaml"))
    config.setdefault(
        "memory_path", str(_APP_DIR / "memory_store" / "memory.json")
    )
    return config


# ----------------------------------------------------------------------
# Agent 单例（懒加载）
# ----------------------------------------------------------------------
_agent: Agent | None = None


def get_agent() -> Agent:
    """获取 Agent 单例；首次调用时按当前配置懒加载。"""
    global _agent
    if _agent is None:
        _agent = Agent(load_config())
    return _agent


# ----------------------------------------------------------------------
# HTTP 接口
# ----------------------------------------------------------------------
app = FastAPI(title="Agenelf API", version="0.1.0")


class ChatRequest(BaseModel):
    """POST /chat 的请求体。"""

    message: str


class ChatResponse(BaseModel):
    """POST /chat 的响应体。"""

    reply: str


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """处理一轮对话，返回助手回复文本。"""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    try:
        reply = get_agent().chat(req.message)
    except Exception as exc:  # Agent 内部错误不应使服务崩溃
        raise HTTPException(status_code=500, detail=f"对话处理失败：{exc}") from exc
    return ChatResponse(reply=reply)


@app.get("/health")
def health() -> dict:
    """健康检查：返回状态、已加载技能数与当前模型名。"""
    agent = get_agent()
    return {
        "status": "ok",
        "skills": len(agent.registry.skills),
        "model": agent.llm.model,
    }


@app.get("/evolution/status")
def evolution_status() -> dict:
    """返回自我迭代晋升管道状态。

    读取运行时根目录下 data/evolution-session.json（当前会话）
    与 data/promote-requests/（最近的晋升请求及其标记文件）。
    """
    root = _runtime_root()
    data_dir = root / "data"

    # 当前会话记录（不存在或损坏时为 None）
    session: dict | None = None
    session_path = data_dir / "evolution-session.json"
    if session_path.exists():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            session = None

    # 最近的晋升请求（按目录修改时间倒序，最多 10 条）
    requests: list[dict] = []
    requests_dir = data_dir / "promote-requests"
    if requests_dir.is_dir():
        entries = [p for p in requests_dir.iterdir() if p.is_dir()]
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for entry in entries[:10]:
            requests.append(
                {
                    "id": entry.name,
                    "markers": sorted(f.name for f in entry.iterdir() if f.is_file()),
                }
            )

    return {
        "root": str(root),
        "session": session,
        "promotion_requests": requests,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
