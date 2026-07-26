from __future__ import annotations

import unittest
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document

from core.interactive_prompt import (
    SlashCommandCompleter,
    accept_selected_completion,
    canonical_command,
    close_command_matches,
    command_hint,
    command_names,
    command_rows,
)


class _FakeRegistry:
    def __init__(self) -> None:
        self.skills = {
            "docker_ops": SimpleNamespace(
                SKILL_META={"description": "远程 Docker 运维"}
            ),
            "reasoning_trace": SimpleNamespace(
                SKILL_META={"description": "可见推理轨迹"}
            ),
        }


class _FakeAgent:
    def __init__(self) -> None:
        self.registry = _FakeRegistry()


class _PendingCompleter(SlashCommandCompleter):
    @staticmethod
    def _pending_operations():
        return [
            ("op-0123456789abcdef", "compose_deploy · pve-ubuntu · 修改端口"),
            ("op-fedcba9876543210", "docker_restart · pve-ubuntu · 重启 VPN"),
        ]


class InteractivePromptTest(unittest.TestCase):
    def _values(self, completer: SlashCommandCompleter, text: str) -> list[str]:
        document = Document(text=text, cursor_position=len(text))
        return [
            item.text
            for item in completer.get_completions(document, CompleteEvent())
        ]

    def test_catalog_is_single_source_for_help_and_startup_hint(self):
        names = command_names()
        rows = command_rows()
        self.assertIn("/approvals", names)
        self.assertIn("/approve", names)
        self.assertIn("/help", names)
        self.assertEqual(len(rows), len(names))
        self.assertIn("输入", command_hint())
        self.assertIn("Tab", command_hint())

    def test_slash_opens_full_command_menu(self):
        values = self._values(SlashCommandCompleter(_FakeAgent()), "/")
        self.assertIn("/approvals", values)
        self.assertIn("/approve", values)
        self.assertIn("/ops", values)
        self.assertIn("/quit", values)

    def test_partial_command_filters_menu(self):
        values = self._values(SlashCommandCompleter(_FakeAgent()), "/ap")
        self.assertEqual(values, ["/approvals", "/approve"])

    def test_reload_completes_loaded_skill_names(self):
        values = self._values(SlashCommandCompleter(_FakeAgent()), "/reload d")
        self.assertEqual(values, ["docker_ops"])
        values = self._values(SlashCommandCompleter(_FakeAgent()), "/reload ")
        self.assertIn("reasoning_trace", values)

    def test_approval_completes_pending_request_ids(self):
        completer = _PendingCompleter(_FakeAgent())
        values = self._values(completer, "/approve op-0")
        self.assertEqual(values, ["op-0123456789abcdef"])
        values = self._values(completer, "/deny ")
        self.assertEqual(
            values,
            ["op-0123456789abcdef", "op-fedcba9876543210"],
        )

    def test_argument_hints_are_contextual(self):
        completer = SlashCommandCompleter(_FakeAgent())
        self.assertEqual(
            self._values(completer, "/remember "), ["fact", "preference"]
        )
        self.assertEqual(
            self._values(completer, "/intend P"), ["P0", "P1", "P2", "P3"]
        )
        self.assertEqual(
            self._values(completer, "/validate "),
            ["check", "suite", "result"],
        )

    def test_tab_accepts_highlighted_completion(self):
        buffer = Buffer()
        original = Document("/appr", cursor_position=5)
        completion = Completion("/approve", start_position=-5)
        buffer.set_document(original, bypass_readonly=True)
        buffer.complete_state = CompletionState(
            original,
            completions=[completion],
            complete_index=0,
        )
        self.assertTrue(accept_selected_completion(buffer))
        self.assertEqual(buffer.text, "/approve")

    def test_aliases_and_typo_suggestions(self):
        self.assertEqual(canonical_command("/commands"), "/help")
        self.assertEqual(canonical_command("/exit"), "/quit")
        self.assertIn("/approvals", close_command_matches("/aprovals"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
