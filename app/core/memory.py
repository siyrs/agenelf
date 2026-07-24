"""长期记忆存储模块。

MemoryStore 以 JSON 文件落盘保存 Agent 的记忆条目，
启动时自动加载，支持关键词包含匹配的回忆检索。
"""

from __future__ import annotations

import json
import os
import tempfile
import time

# 允许的记忆类型：事实 / 偏好 / 交互片段
VALID_KINDS = {"fact", "preference", "episode"}
DEFAULT_PROMPT_LIMIT = 50
DEFAULT_PROMPT_MAX_CHARS = 8000


class MemoryStore:
    """简单的 JSON 落盘记忆存储。

    每条记忆是一个字典：{"kind": str, "content": str, "ts": float}
    """

    def __init__(self, path: str):
        # 记忆文件路径；启动时若存在则自动加载
        self.path = path
        self.memories: list[dict] = []
        self._load()

    def _load(self) -> None:
        """从磁盘加载记忆文件；文件缺失或损坏时从空记忆开始。"""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.memories = data
        except (json.JSONDecodeError, OSError):
            # 文件损坏不致命，忽略并以空记忆启动
            self.memories = []

    def _save(self) -> None:
        """原子地写回全部记忆，避免进程中断留下损坏的 JSON 文件。"""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".memory-", suffix=".json", dir=directory, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.memories, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(temp_path, self.path)
        except OSError:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def add(self, kind: str, content: str) -> None:
        """新增一条记忆。kind 必须是 fact/preference/episode 之一。"""
        if kind not in VALID_KINDS:
            raise ValueError(f"非法记忆类型: {kind}，应为 {VALID_KINDS} 之一")
        self.memories.append(
            {"kind": kind, "content": content, "ts": time.time()}
        )
        self._save()

    def recall(self, query: str, limit: int = 5) -> list[str]:
        """关键词包含匹配检索：任一查询词出现在内容中即命中。

        命中结果按时间倒序（越新越靠前），最多返回 limit 条内容。
        """
        if not query:
            return []
        # 将查询按空白拆成若干关键词，统一小写后做包含匹配
        keywords = [w.lower() for w in query.split() if w.strip()]
        if not keywords:
            keywords = [query.lower()]
        hits: list[dict] = []
        for mem in self.memories:
            content = str(mem.get("content", ""))
            lower = content.lower()
            if any(kw in lower for kw in keywords):
                hits.append(mem)
        # 最新的记忆排前面
        hits.sort(key=lambda m: m.get("ts", 0), reverse=True)
        return [str(m["content"]) for m in hits[:limit]]

    def as_prompt_block(
        self,
        limit: int = DEFAULT_PROMPT_LIMIT,
        max_chars: int = DEFAULT_PROMPT_MAX_CHARS,
    ) -> str:
        """渲染最近记忆为提示词块，并限制条目数和总字符数。

        JSON 文件保留完整历史；限制只作用于模型上下文，防止长时间运行后
        因历史记忆持续膨胀而耗尽上下文窗口。
        """
        if not self.memories:
            return "（暂无长期记忆）"
        limit = max(0, int(limit))
        max_chars = max(0, int(max_chars))
        if not limit or not max_chars:
            return "（长期记忆提示注入已关闭）"
        memories = [mem for mem in self.memories if isinstance(mem, dict)]
        memories.sort(key=lambda mem: mem.get("ts", 0), reverse=True)
        lines: list[str] = []
        used = 0
        for mem in memories[:limit]:
            line = f"- [{mem.get('kind', 'fact')}] {mem.get('content', '')}"
            remaining = max_chars - used
            if remaining <= 0:
                break
            if len(line) > remaining:
                lines.append(line[: max(0, remaining - 1)] + "…")
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return "（暂无可注入的长期记忆）"
        return "\n".join(lines)
