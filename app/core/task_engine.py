"""Minimal governed task engine foundation.

Tasks are planning records, not direct executors. Execution must still go
through existing capabilities, permissions and evidence pipelines.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import uuid


TERMINAL = {"completed", "failed", "cancelled"}
VALID = {"proposed", "planned", "running", "waiting_approval", "verifying"} | TERMINAL


class TaskEngine:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "data" / "tasks"
        self.path.mkdir(parents=True, exist_ok=True)

    def create(self, title: str, steps: list[str], acceptance: list[str]) -> dict:
        task = {
            "id": "task-" + uuid.uuid4().hex[:16],
            "title": title,
            "steps": steps[:50],
            "acceptance_criteria": acceptance[:20],
            "evidence": [],
            "status": "planned",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(task)
        return task

    def get(self, task_id: str) -> dict:
        file = self.path / f"{task_id}.json"
        if not file.exists():
            raise ValueError("task not found")
        return json.loads(file.read_text(encoding="utf-8"))

    def transition(self, task_id: str, status: str, evidence: str | None = None) -> dict:
        if status not in VALID:
            raise ValueError("invalid task state")
        task = self.get(task_id)
        if task["status"] in TERMINAL:
            raise ValueError("terminal task cannot transition")
        task["status"] = status
        if evidence:
            task["evidence"].append(evidence)
        self._write(task)
        return task

    def _write(self, task: dict):
        (self.path / f"{task['id']}.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
