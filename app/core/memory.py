"""Persistent long-term memory stored under the owner-specific local directory."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time

from .privacy import redact_sensitive_text

# 清洗孤立 surrogate 字符，防止 json.dump 崩溃
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

VALID_KINDS = {"fact", "preference", "episode"}


def _sanitize_memories(obj: object) -> object:
    """递归清洗对象中所有字符串的 surrogate 字符。"""
    if isinstance(obj, str):
        return _SURROGATE_RE.sub("�", obj)
    if isinstance(obj, dict):
        return {k: _sanitize_memories(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_memories(v) for v in obj]
    return obj
DEFAULT_PROMPT_LIMIT = 50
DEFAULT_PROMPT_MAX_CHARS = 8000
DEFAULT_MAX_ENTRIES = 1000


class MemoryStore:
    """Atomic JSON memory with redaction, bounded growth and basic recall."""

    def __init__(self, path: str, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.path = path
        self.max_entries = max(1, int(max_entries))
        self.memories: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                self.memories = [item for item in data if isinstance(item, dict)][
                    -self.max_entries :
                ]
        except (json.JSONDecodeError, OSError):
            self.memories = []

    def _save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".memory-", suffix=".json", dir=directory, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                # 递归清洗 surrogate 后写盘，兜底防御 LLM 返回的非法 Unicode
                json.dump(_sanitize_memories(self.memories), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_path, self.path)
        except OSError:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def add(self, kind: str, content: str) -> bool:
        """Add a redacted entry. Returns False for an exact consecutive duplicate."""

        if kind not in VALID_KINDS:
            raise ValueError(f"非法记忆类型: {kind}，应为 {VALID_KINDS} 之一")
        safe_content = redact_sensitive_text(content).strip()
        if not safe_content:
            return False
        if self.memories:
            last = self.memories[-1]
            if last.get("kind") == kind and last.get("content") == safe_content:
                return False
        self.memories.append({"kind": kind, "content": safe_content, "ts": time.time()})
        if len(self.memories) > self.max_entries:
            self.memories = self.memories[-self.max_entries :]
        self._save()
        return True

    def recall(self, query: str, limit: int = 5) -> list[str]:
        if not query:
            return []
        safe_query = redact_sensitive_text(query)
        keywords = [word.lower() for word in safe_query.split() if word.strip()]
        if not keywords:
            keywords = [safe_query.lower()]
        hits: list[dict] = []
        for memory in self.memories:
            content = redact_sensitive_text(memory.get("content", ""))
            lower = content.lower()
            if any(keyword in lower for keyword in keywords):
                hits.append({**memory, "content": content})
        hits.sort(key=lambda item: item.get("ts", 0), reverse=True)
        return [str(item["content"]) for item in hits[: max(0, int(limit))]]

    def stats(self) -> dict:
        counts = {kind: 0 for kind in sorted(VALID_KINDS)}
        for memory in self.memories:
            kind = str(memory.get("kind", ""))
            if kind in counts:
                counts[kind] += 1
        return {
            "path": os.path.abspath(self.path),
            "entries": len(self.memories),
            "max_entries": self.max_entries,
            "kinds": counts,
        }

    def as_prompt_block(
        self,
        limit: int = DEFAULT_PROMPT_LIMIT,
        max_chars: int = DEFAULT_PROMPT_MAX_CHARS,
    ) -> str:
        if not self.memories:
            return "（暂无长期记忆）"
        limit = max(0, int(limit))
        max_chars = max(0, int(max_chars))
        if not limit or not max_chars:
            return "（长期记忆提示注入已关闭）"
        memories = [memory for memory in self.memories if isinstance(memory, dict)]
        memories.sort(key=lambda memory: memory.get("ts", 0), reverse=True)
        lines: list[str] = []
        used = 0
        for memory in memories[:limit]:
            content = redact_sensitive_text(memory.get("content", ""))
            line = f"- [{memory.get('kind', 'fact')}] {content}"
            remaining = max_chars - used
            if remaining <= 0:
                break
            if len(line) > remaining:
                lines.append(line[: max(0, remaining - 1)] + "…")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) if lines else "（暂无可注入的长期记忆）"
