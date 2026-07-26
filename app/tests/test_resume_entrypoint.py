from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import resume
from skills import task_continuation


class RecordingAgent:
    calls: list[str] = []
    complete_during_chat = False

    def __init__(self, config):
        self.config = config

    def chat(self, message: str, subject: str = "agent") -> str:
        self.__class__.calls.append(message)
        if self.__class__.complete_during_chat:
            continuation_id = next(
                line.split(":", 1)[1].strip()
                for line in message.splitlines()
                if line.startswith("continuation_id:")
            )
            task_continuation.complete(continuation_id, ["op-0123456789abcdef"])
            return "done"
        return "resume attempted"


class ResumeEntrypointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)
        RecordingAgent.calls = []
        RecordingAgent.complete_during_chat = False

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    @staticmethod
    def _config_loader(**kwargs):
        return {"mock": True, "app_dir": str(kwargs.get("app_dir", ""))}

    def test_pending_checkpoint_is_resumed_exactly_once(self) -> None:
        task_continuation.checkpoint("修复 sing-box", "读取日志并继续原任务")
        output: list[str] = []
        code = resume.run_once(
            agent_factory=RecordingAgent,
            config_loader=self._config_loader,
            emit=output.append,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(RecordingAgent.calls), 1)
        self.assertIn("不扩大原任务范围", RecordingAgent.calls[0])
        self.assertEqual(task_continuation.status()["status"], "attempted")
        resume.run_once(
            agent_factory=RecordingAgent,
            config_loader=self._config_loader,
            emit=output.append,
        )
        self.assertEqual(len(RecordingAgent.calls), 1)

    def test_agent_completion_is_not_overwritten_by_attempt_status(self) -> None:
        task_continuation.checkpoint("修复 sing-box", "完成后记录证据")
        RecordingAgent.complete_during_chat = True
        code = resume.run_once(
            agent_factory=RecordingAgent,
            config_loader=self._config_loader,
            emit=lambda _: None,
        )
        self.assertEqual(code, 0)
        state = task_continuation.status()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["evidence"], ["op-0123456789abcdef"])

    def test_no_pending_checkpoint_is_noop(self) -> None:
        code = resume.run_once(
            agent_factory=RecordingAgent,
            config_loader=self._config_loader,
            emit=lambda _: None,
        )
        self.assertEqual(code, 0)
        self.assertFalse(RecordingAgent.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
