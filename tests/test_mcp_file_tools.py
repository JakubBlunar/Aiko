"""Tests for the MCP task/file-roots debug tools.

These verify that the tools registered in
:func:`app.mcp.server.create_mcp_server` —
``list_file_roots`` / ``list_active_tasks`` / ``answer_file_task``
/ ``cancel_task`` — wire up correctly to a session's
:class:`TaskOrchestrator` and to the sandbox validator. File
read/search now come from the filesystem MCP plugin, so their
``start_*`` debug tools were removed.

We use the same ``_FakeSession`` shape as the chunk-7 tests so the
tools can be exercised through :meth:`FastMCP.call_tool` without
needing a real :class:`SessionController`. The session stub records
every call to the orchestrator's public surface (``start_task`` /
``list_running`` / ``cancel``) so each test can assert the right
kwargs landed.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.mcp.server import create_mcp_server


@dataclass
class _FakeAgent:
    task_file_allowed_roots: tuple[dict[str, Any], ...] = ()
    tasks_enabled: bool = True


@dataclass
class _FakeSettings:
    agent: _FakeAgent = field(default_factory=_FakeAgent)


@dataclass
class _FakeRow:
    id: int
    handler_name: str = "file_search"
    title: str = "untitled"
    status: str = "running"
    progress: float | None = None
    last_message: str | None = None
    user_id: str = "test-user"
    initiated_by: str = "system"
    created_at: str = "2026-06-07T13:00:00+00:00"
    # Chunk 12: surface the awaiting-input prompt so MCP users can
    # see what answer the task is waiting for.
    input_request: dict[str, Any] | None = None


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.list_calls: list[str | None] = []
        self.cancel_calls: list[int] = []
        self.answer_calls: list[tuple[int, str]] = []
        self.next_task_id: int | None = 42
        self.running_rows: list[_FakeRow] = []
        self.cancel_outcome: bool = True
        self.answer_outcome: bool = True
        self.start_raise: Exception | None = None

    def start_task(
        self,
        *,
        user_id: str,
        handler_name: str,
        args: dict[str, Any],
        title: str,
        initiated_by: str = "aiko",
    ) -> int | None:
        if self.start_raise is not None:
            raise self.start_raise
        self.start_calls.append(
            {
                "user_id": user_id,
                "handler_name": handler_name,
                "args": dict(args),
                "title": title,
                "initiated_by": initiated_by,
            }
        )
        return self.next_task_id

    def list_running(self, user_id: str | None = None) -> list[_FakeRow]:
        self.list_calls.append(user_id)
        return list(self.running_rows)

    def cancel(self, task_id: int) -> bool:
        self.cancel_calls.append(int(task_id))
        return bool(self.cancel_outcome)

    def answer(self, task_id: int, answer: str) -> bool:
        self.answer_calls.append((int(task_id), str(answer)))
        return bool(self.answer_outcome)


class _FakeSession:
    """Just enough surface for the chunk-9 MCP tools.

    The chunk-7 / chunk-8 work on this stub already exercised the
    ``send_message`` path. We add ``_task_orchestrator`` +
    ``_user_id`` + ``_settings.agent.task_file_allowed_roots``
    here.
    """

    def __init__(
        self,
        *,
        orchestrator: _FakeOrchestrator | None,
        roots: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self._user_id = "test-user"
        self._task_orchestrator = orchestrator
        self._settings = _FakeSettings(
            agent=_FakeAgent(task_file_allowed_roots=roots),
        )

    # The session.* fields below are only used by other MCP tools
    # we don't exercise here. Stubbed to avoid AttributeError on
    # tool registration scanning.
    @property
    def effective_chat_model(self) -> str:
        return "stub-model"

    @property
    def context_window_size(self) -> int:
        return 1024

    @property
    def tts_provider(self) -> str:
        return "stub"

    @property
    def tts_voice(self) -> str:
        return "stub"

    @property
    def session_key(self) -> str:
        return "session-test-user"

    def get_last_metrics(self) -> dict[str, Any]:
        return {}

    def clear_conversation_memory(self) -> None:
        pass

    def _notify_message(self, who: str, text: str) -> None:
        pass


def _call_tool(server: Any, name: str, args: dict[str, Any]) -> Any:
    blocks, structured = asyncio.run(server.call_tool(name, args))
    if structured and "result" in structured:
        return structured["result"]
    if blocks:
        return getattr(blocks[0], "text", None)
    return None


class ListFileRootsTests(unittest.TestCase):
    def test_empty_config_returns_empty_list(self) -> None:
        session = _FakeSession(orchestrator=_FakeOrchestrator())
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "list_file_roots", {}))
        self.assertEqual(out, [])

    def test_active_root_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "Docs").mkdir()
            session = _FakeSession(
                orchestrator=_FakeOrchestrator(),
                roots=({"label": "Docs", "path": str(base / "Docs")},),
            )
            server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
            out = json.loads(_call_tool(server, "list_file_roots", {}))
            self.assertEqual(len(out), 1)
            self.assertTrue(out[0]["active"])
            self.assertEqual(out[0]["label"], "Docs")
            self.assertEqual(out[0]["reason"], "")
            self.assertEqual(out[0]["warnings"], [])

    def test_missing_root_reported_inactive(self) -> None:
        session = _FakeSession(
            orchestrator=_FakeOrchestrator(),
            roots=({"label": "Ghost", "path": "/never/exists/here"},),
        )
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "list_file_roots", {}))
        self.assertFalse(out[0]["active"])
        self.assertEqual(out[0]["reason"], "missing")


class ListActiveTasksTests(unittest.TestCase):
    def test_empty_when_no_active_tasks(self) -> None:
        session = _FakeSession(orchestrator=_FakeOrchestrator())
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "list_active_tasks", {}))
        self.assertEqual(out, [])

    def test_reports_running_rows(self) -> None:
        orch = _FakeOrchestrator()
        orch.running_rows = [
            _FakeRow(
                id=1, handler_name="file_search", title="search alpha",
                status="running", progress=0.5, last_message="scanning...",
            ),
            _FakeRow(
                id=2, handler_name="file_search", title="search beta",
                status="awaiting_input",
            ),
        ]
        session = _FakeSession(orchestrator=orch)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "list_active_tasks", {}))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[0]["status"], "running")
        self.assertEqual(out[0]["progress"], 0.5)
        self.assertEqual(out[1]["status"], "awaiting_input")
        # Verify the call passed user_id=None (all-users debug view).
        self.assertEqual(orch.list_calls, [None])

    def test_disabled_subsystem_returns_empty_list(self) -> None:
        session = _FakeSession(orchestrator=None)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "list_active_tasks", {}))
        self.assertEqual(out, [])


class AnswerFileTaskTests(unittest.TestCase):
    """Chunk 12: `answer_file_task` resolves an awaiting-input task."""

    def test_successful_answer(self) -> None:
        orch = _FakeOrchestrator()
        session = _FakeSession(orchestrator=orch)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(
            _call_tool(
                server,
                "answer_file_task",
                {"task_id": 13, "answer": "Documents:foo.md"},
            )
        )
        self.assertEqual(out, {"answered": True, "task_id": 13})
        self.assertEqual(
            orch.answer_calls, [(13, "Documents:foo.md")]
        )

    def test_failed_answer_returns_false(self) -> None:
        orch = _FakeOrchestrator()
        orch.answer_outcome = False
        session = _FakeSession(orchestrator=orch)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(
            _call_tool(
                server,
                "answer_file_task",
                {"task_id": 4, "answer": "x"},
            )
        )
        self.assertEqual(out, {"answered": False, "task_id": 4})

    def test_disabled_subsystem(self) -> None:
        session = _FakeSession(orchestrator=None)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(
            _call_tool(
                server, "answer_file_task",
                {"task_id": 1, "answer": "x"},
            )
        )
        self.assertIn("error", out)


class ListActiveTasksInputRequestTests(unittest.TestCase):
    """Chunk 12: awaiting-input rows surface their ``input_request``
    so the MCP user can see the prompt + candidate options without
    a separate roundtrip.
    """

    def test_input_request_surfaces_when_present(self) -> None:
        orch = _FakeOrchestrator()
        orch.running_rows = [
            _FakeRow(
                id=5,
                handler_name="file_read",
                title="file read",
                status="awaiting_input",
                input_request={
                    "prompt": "Which root?",
                    "options": ["Docs:x.md", "Notes:x.md"],
                },
            ),
        ]
        session = _FakeSession(orchestrator=orch)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "list_active_tasks", {}))
        self.assertEqual(len(out), 1)
        self.assertIn("input_request", out[0])
        self.assertEqual(out[0]["input_request"]["prompt"], "Which root?")
        self.assertEqual(
            out[0]["input_request"]["options"],
            ["Docs:x.md", "Notes:x.md"],
        )

    def test_input_request_omitted_when_running(self) -> None:
        orch = _FakeOrchestrator()
        orch.running_rows = [
            _FakeRow(id=6, status="running", input_request=None),
        ]
        session = _FakeSession(orchestrator=orch)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "list_active_tasks", {}))
        self.assertNotIn("input_request", out[0])


class CancelTaskTests(unittest.TestCase):
    def test_successful_cancel(self) -> None:
        orch = _FakeOrchestrator()
        orch.cancel_outcome = True
        session = _FakeSession(orchestrator=orch)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "cancel_task", {"task_id": 7}))
        self.assertEqual(out, {"cancelled": True, "task_id": 7})
        self.assertEqual(orch.cancel_calls, [7])

    def test_failed_cancel_returns_false(self) -> None:
        orch = _FakeOrchestrator()
        orch.cancel_outcome = False
        session = _FakeSession(orchestrator=orch)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "cancel_task", {"task_id": 99}))
        self.assertEqual(out, {"cancelled": False, "task_id": 99})

    def test_disabled_subsystem_returns_error(self) -> None:
        session = _FakeSession(orchestrator=None)
        server = create_mcp_server(session, port=0)  # type: ignore[arg-type]
        out = json.loads(_call_tool(server, "cancel_task", {"task_id": 1}))
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
