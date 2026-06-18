"""Tests for :mod:`app.llm.tools.file_tasks` — the chunk 10 agent tools.

The tools are thin glue around :class:`TaskOrchestrator`. These tests
verify the glue without needing a real orchestrator:

* Schema shape matches the Ollama tools-API contract.
* ``run()`` rejects bad / empty / wrong-type args before reaching
  the orchestrator.
* Happy path threads through the orchestrator with the right
  kwargs (``initiated_by="aiko"``, ``user_id`` from the session,
  ``handler_name="file_search"``, etc.).
* ``handler_for`` guard catches the "subsystem on but handler not
  registered" edge case (no file roots configured).
* Per-user cap rejection (``start_task`` returns None) raises a
  clean :class:`ToolError`.
* ``cancel_file_task`` validates ``task_id`` as a positive int and
  threads through.
* Disabled subsystem (no orchestrator on session) raises a
  user-facing :class:`ToolError` rather than crashing.
"""
from __future__ import annotations

import json
import os
import unittest
from typing import Any

from app.llm.tools.base import ToolError
from app.llm.tools.file_tasks import (
    AnswerFileTaskTool,
    CancelFileTaskTool,
    ListFileRootsTool,
    StartFileReadTool,
    StartFileSearchTool,
    build_file_task_tools,
)


class _FakeRow:
    """Minimal task row exposing the fields the tools read."""

    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.result = result
        self.metadata = metadata
        self.error = error


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[int] = []
        self.answer_calls: list[tuple[int, str]] = []
        self.next_task_id: int | None = 7
        self.cancel_outcome: bool = True
        self.answer_outcome: bool = True
        self.start_raise: Exception | None = None
        self.cancel_raise: Exception | None = None
        self.answer_raise: Exception | None = None
        # Inline fast-path knobs. ``wait_status`` is what
        # ``wait_for_task`` returns; default "timeout" keeps spawns on
        # the async (reply-on-complete) path so the legacy tests that
        # assert the running/async payload keep passing.
        self.wait_status: str = "timeout"
        self.wait_calls: int = 0
        self.rows: dict[int, _FakeRow] = {}
        # Chunk 12: file_read joins file_search as a default handler.
        self._handlers: dict[str, Any] = {
            "file_search": object(),
            "file_read": object(),
        }

    def handler_for(self, name: str) -> Any | None:
        return self._handlers.get(name)

    def start_task(
        self,
        *,
        user_id: str,
        handler_name: str,
        args: dict[str, Any],
        title: str,
        initiated_by: str = "aiko",
        metadata: dict[str, Any] | None = None,
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
                "metadata": dict(metadata) if metadata else None,
            }
        )
        return self.next_task_id

    def wait_for_task(self, task_id: int, *, timeout: float = 5.0) -> str:
        self.wait_calls += 1
        return self.wait_status

    def get(self, task_id: int) -> _FakeRow | None:
        return self.rows.get(int(task_id))

    def cancel(self, task_id: int) -> bool:
        if self.cancel_raise is not None:
            raise self.cancel_raise
        self.cancel_calls.append(int(task_id))
        return bool(self.cancel_outcome)

    def answer(self, task_id: int, answer: str) -> bool:
        if self.answer_raise is not None:
            raise self.answer_raise
        self.answer_calls.append((int(task_id), str(answer)))
        return bool(self.answer_outcome)


class _FakeSession:
    """The two attributes the tools read."""

    def __init__(
        self,
        *,
        orchestrator: _FakeOrchestrator | None,
        user_id: str = "jacob",
    ) -> None:
        self._user_id = user_id
        self._task_orchestrator = orchestrator


# ── StartFileSearchTool ──────────────────────────────────────────────────


class StartFileSearchSchemaTests(unittest.TestCase):
    def test_schema_name(self) -> None:
        tool = StartFileSearchTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        self.assertEqual(tool.name, "start_file_search")
        self.assertEqual(tool.schema().name, "start_file_search")

    def test_schema_required_query(self) -> None:
        tool = StartFileSearchTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        params = tool.schema().parameters
        self.assertIn("query", params["properties"])
        self.assertEqual(params["required"], ["query"])

    def test_schema_optional_params_documented(self) -> None:
        tool = StartFileSearchTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        props = tool.schema().parameters["properties"]
        for key in ("root_label", "max_results", "case_sensitive"):
            self.assertIn(key, props)

    def test_schema_cross_references_workflow(self) -> None:
        # Routing fix: the fast search tool must point multi-step requests
        # at start_workflow so the model stops hand-chaining.
        tool = StartFileSearchTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        desc = tool.schema().description.lower()
        self.assertIn("start_workflow", desc)
        self.assertIn("one search only", desc)


class StartFileSearchRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _FakeOrchestrator()
        self.session = _FakeSession(orchestrator=self.orch)
        self.tool = StartFileSearchTool(self.session)

    def test_happy_path_returns_task_id(self) -> None:
        out = json.loads(self.tool.run({"query": "memory"}))
        self.assertEqual(out["task_id"], 7)
        self.assertEqual(out["handler"], "file_search")
        self.assertIn("note", out)
        call = self.orch.start_calls[0]
        self.assertEqual(call["user_id"], "jacob")
        self.assertEqual(call["handler_name"], "file_search")
        self.assertEqual(call["args"]["query"], "memory")
        self.assertEqual(call["args"]["root_label"], "")
        self.assertEqual(call["args"]["max_results"], 50)
        self.assertEqual(call["args"]["case_sensitive"], False)
        self.assertEqual(call["initiated_by"], "aiko")

    def test_query_required(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({})

    def test_query_empty_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"query": "   "})

    def test_max_results_clamps_low(self) -> None:
        self.tool.run({"query": "x", "max_results": 0})
        self.assertEqual(self.orch.start_calls[0]["args"]["max_results"], 1)

    def test_max_results_clamps_high(self) -> None:
        self.tool.run({"query": "x", "max_results": 9999})
        self.assertEqual(self.orch.start_calls[0]["args"]["max_results"], 500)

    def test_max_results_invalid_type_falls_back_to_default(self) -> None:
        self.tool.run({"query": "x", "max_results": "lots"})
        self.assertEqual(self.orch.start_calls[0]["args"]["max_results"], 50)

    def test_root_label_threaded_through_and_titled(self) -> None:
        self.tool.run({"query": "alpha", "root_label": "Notes"})
        call = self.orch.start_calls[0]
        self.assertEqual(call["args"]["root_label"], "Notes")
        self.assertIn("Notes", call["title"])
        self.assertIn("alpha", call["title"])

    def test_case_sensitive_threaded_through(self) -> None:
        self.tool.run({"query": "FOO", "case_sensitive": True})
        self.assertTrue(self.orch.start_calls[0]["args"]["case_sensitive"])

    def test_disabled_subsystem_raises_tool_error(self) -> None:
        tool = StartFileSearchTool(_FakeSession(orchestrator=None))
        with self.assertRaises(ToolError) as ctx:
            tool.run({"query": "anything"})
        self.assertIn("disabled", str(ctx.exception))

    def test_missing_handler_raises_tool_error(self) -> None:
        orch = _FakeOrchestrator()
        orch._handlers.clear()  # no file_search handler registered
        tool = StartFileSearchTool(_FakeSession(orchestrator=orch))
        with self.assertRaises(ToolError) as ctx:
            tool.run({"query": "anything"})
        self.assertIn("not registered", str(ctx.exception))

    def test_per_user_cap_rejection_surfaces_tool_error(self) -> None:
        self.orch.next_task_id = None
        with self.assertRaises(ToolError) as ctx:
            self.tool.run({"query": "x"})
        self.assertIn("rejected", str(ctx.exception))

    def test_orchestrator_exception_wrapped(self) -> None:
        self.orch.start_raise = RuntimeError("db locked")
        with self.assertRaises(ToolError) as ctx:
            self.tool.run({"query": "x"})
        self.assertIn("db locked", str(ctx.exception))


# ── CancelFileTaskTool ───────────────────────────────────────────────────


class CancelFileTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _FakeOrchestrator()
        self.session = _FakeSession(orchestrator=self.orch)
        self.tool = CancelFileTaskTool(self.session)

    def test_schema_required_task_id(self) -> None:
        params = self.tool.schema().parameters
        self.assertIn("task_id", params["properties"])
        self.assertEqual(params["required"], ["task_id"])

    def test_happy_path(self) -> None:
        out = json.loads(self.tool.run({"task_id": 42}))
        self.assertEqual(out, {"cancelled": True, "task_id": 42})
        self.assertEqual(self.orch.cancel_calls, [42])

    def test_unknown_task_id_returns_false_not_error(self) -> None:
        # The orchestrator returns False for unknown / already-terminal
        # tasks. The tool reports that verbatim to the LLM so Aiko can
        # phrase the user-facing message naturally rather than treating
        # a no-op cancel as a hard failure.
        self.orch.cancel_outcome = False
        out = json.loads(self.tool.run({"task_id": 99}))
        self.assertEqual(out, {"cancelled": False, "task_id": 99})

    def test_missing_task_id_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({})

    def test_non_int_task_id_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": "abc"})

    def test_negative_task_id_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": -1})

    def test_zero_task_id_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": 0})

    def test_disabled_subsystem_raises_tool_error(self) -> None:
        tool = CancelFileTaskTool(_FakeSession(orchestrator=None))
        with self.assertRaises(ToolError):
            tool.run({"task_id": 1})

    def test_orchestrator_exception_wrapped(self) -> None:
        self.orch.cancel_raise = RuntimeError("io error")
        with self.assertRaises(ToolError) as ctx:
            self.tool.run({"task_id": 1})
        self.assertIn("io error", str(ctx.exception))


# ── StartFileReadTool (chunk 12) ─────────────────────────────────────────


class StartFileReadSchemaTests(unittest.TestCase):
    def test_schema_name(self) -> None:
        tool = StartFileReadTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        self.assertEqual(tool.name, "start_file_read")
        self.assertEqual(tool.schema().name, "start_file_read")

    def test_schema_required_path(self) -> None:
        tool = StartFileReadTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        params = tool.schema().parameters
        self.assertIn("path", params["properties"])
        self.assertEqual(params["required"], ["path"])

    def test_schema_cross_references_workflow(self) -> None:
        tool = StartFileReadTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        desc = tool.schema().description.lower()
        self.assertIn("start_workflow", desc)
        self.assertIn("one file only", desc)

    def test_schema_optional_max_bytes(self) -> None:
        tool = StartFileReadTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        props = tool.schema().parameters["properties"]
        self.assertIn("max_bytes", props)


class StartFileReadRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _FakeOrchestrator()
        self.session = _FakeSession(orchestrator=self.orch)
        self.tool = StartFileReadTool(self.session)

    def test_happy_path(self) -> None:
        out = json.loads(self.tool.run({"path": "Documents:notes.md"}))
        self.assertEqual(out["task_id"], 7)
        self.assertEqual(out["handler"], "file_read")
        self.assertIn("note", out)
        call = self.orch.start_calls[0]
        self.assertEqual(call["handler_name"], "file_read")
        self.assertEqual(call["args"]["path"], "Documents:notes.md")
        # max_bytes was not supplied — should not appear in args.
        self.assertNotIn("max_bytes", call["args"])

    def test_max_bytes_threaded_when_set(self) -> None:
        self.tool.run({"path": "foo.md", "max_bytes": 4096})
        call = self.orch.start_calls[0]
        self.assertEqual(call["args"]["max_bytes"], 4096)

    def test_max_bytes_zero_is_omitted(self) -> None:
        # The agent setting for the ceiling already applies server-
        # side, so a zero from the LLM is "use default" and should
        # NOT land in the args dict.
        self.tool.run({"path": "foo.md", "max_bytes": 0})
        call = self.orch.start_calls[0]
        self.assertNotIn("max_bytes", call["args"])

    def test_empty_path_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"path": "  "})

    def test_missing_path_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({})

    def test_disabled_subsystem(self) -> None:
        tool = StartFileReadTool(_FakeSession(orchestrator=None))
        with self.assertRaises(ToolError) as ctx:
            tool.run({"path": "x.md"})
        self.assertIn("disabled", str(ctx.exception))

    def test_missing_handler(self) -> None:
        orch = _FakeOrchestrator()
        orch._handlers.pop("file_read", None)
        tool = StartFileReadTool(_FakeSession(orchestrator=orch))
        with self.assertRaises(ToolError) as ctx:
            tool.run({"path": "x.md"})
        self.assertIn("not registered", str(ctx.exception))

    def test_per_user_cap_rejection(self) -> None:
        self.orch.next_task_id = None
        with self.assertRaises(ToolError):
            self.tool.run({"path": "x.md"})

    def test_initiated_by_aiko(self) -> None:
        self.tool.run({"path": "anything.md"})
        self.assertEqual(self.orch.start_calls[0]["initiated_by"], "aiko")


# ── AnswerFileTaskTool (chunk 12) ────────────────────────────────────────


class AnswerFileTaskSchemaTests(unittest.TestCase):
    def test_required_fields(self) -> None:
        tool = AnswerFileTaskTool(_FakeSession(orchestrator=_FakeOrchestrator()))
        params = tool.schema().parameters
        self.assertEqual(set(params["required"]), {"task_id", "answer"})


class AnswerFileTaskRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _FakeOrchestrator()
        self.tool = AnswerFileTaskTool(_FakeSession(orchestrator=self.orch))

    def test_happy_path(self) -> None:
        out = json.loads(
            self.tool.run({"task_id": 5, "answer": "Documents:foo.md"})
        )
        self.assertEqual(out, {"answered": True, "task_id": 5})
        self.assertEqual(
            self.orch.answer_calls, [(5, "Documents:foo.md")]
        )

    def test_unaccepted_answer_returns_false(self) -> None:
        self.orch.answer_outcome = False
        out = json.loads(
            self.tool.run({"task_id": 9, "answer": "Notes:x.md"})
        )
        self.assertEqual(out, {"answered": False, "task_id": 9})

    def test_missing_task_id(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"answer": "x"})

    def test_zero_task_id_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": 0, "answer": "x"})

    def test_missing_answer_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": 1})

    def test_empty_answer_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tool.run({"task_id": 1, "answer": "   "})

    def test_disabled_subsystem(self) -> None:
        tool = AnswerFileTaskTool(_FakeSession(orchestrator=None))
        with self.assertRaises(ToolError):
            tool.run({"task_id": 1, "answer": "x"})

    def test_orchestrator_exception_wrapped(self) -> None:
        self.orch.answer_raise = RuntimeError("boom")
        with self.assertRaises(ToolError) as ctx:
            self.tool.run({"task_id": 1, "answer": "x"})
        self.assertIn("boom", str(ctx.exception))


# ── ListFileRootsTool ────────────────────────────────────────────────────


class _FakeAgentCfg:
    def __init__(self, roots: tuple[dict[str, Any], ...] = ()) -> None:
        self.task_file_allowed_roots = roots


class _FakeSettings:
    def __init__(self, agent_cfg: _FakeAgentCfg) -> None:
        self.agent = agent_cfg


class _FakeSessionWithSettings:
    """Variant of ``_FakeSession`` that also exposes ``_settings``,
    which ``list_file_roots`` reads to find the configured roots.
    """

    def __init__(
        self,
        *,
        roots: tuple[dict[str, Any], ...] = (),
        user_id: str = "jacob",
    ) -> None:
        self._user_id = user_id
        self._task_orchestrator = _FakeOrchestrator()
        self._settings = _FakeSettings(_FakeAgentCfg(roots))


class ListFileRootsSchemaTests(unittest.TestCase):
    def test_schema_name_and_no_required_params(self) -> None:
        tool = ListFileRootsTool(_FakeSessionWithSettings())
        self.assertEqual(tool.name, "list_file_roots")
        schema = tool.schema()
        self.assertEqual(schema.name, "list_file_roots")
        self.assertEqual(schema.parameters["required"], [])
        # Zero-arg tools still need a properties dict (some providers
        # 400 on missing "properties" key in the JSON-schema body).
        self.assertIn("properties", schema.parameters)


class ListFileRootsRunTests(unittest.TestCase):
    def setUp(self) -> None:
        # Use the host repo so we have a real directory to peek into
        # without needing fixtures. The repo root is always present.
        import pathlib
        self.repo_root = str(pathlib.Path(__file__).resolve().parent.parent)

    def test_empty_config_returns_empty_catalogue(self) -> None:
        session = _FakeSessionWithSettings(roots=())
        tool = ListFileRootsTool(session)
        out = json.loads(tool.run({}))
        self.assertEqual(out["roots"], [])
        self.assertEqual(out["total_roots"], 0)
        self.assertEqual(out["active_roots"], 0)

    def test_active_root_returns_preview_with_dirs_first(self) -> None:
        roots = ({"label": "Repo", "path": self.repo_root},)
        tool = ListFileRootsTool(_FakeSessionWithSettings(roots=roots))
        out = json.loads(tool.run({}))
        self.assertEqual(out["total_roots"], 1)
        self.assertEqual(out["active_roots"], 1)
        entry = out["roots"][0]
        self.assertEqual(entry["label"], "Repo")
        self.assertTrue(entry["active"])
        self.assertEqual(entry["reason"], "")
        self.assertIsInstance(entry["preview"], list)
        self.assertGreater(len(entry["preview"]), 0)
        # Each preview row carries name + kind.
        for row in entry["preview"]:
            self.assertIn("name", row)
            self.assertIn(row["kind"], ("dir", "file"))
        # Sort invariant: every dir precedes every file.
        kinds = [row["kind"] for row in entry["preview"]]
        last_dir_idx = max(
            (i for i, k in enumerate(kinds) if k == "dir"), default=-1,
        )
        first_file_idx = next(
            (i for i, k in enumerate(kinds) if k == "file"), len(kinds),
        )
        self.assertLess(last_dir_idx, first_file_idx)

    def test_inactive_root_reports_reason_and_no_preview(self) -> None:
        roots = (
            {
                "label": "Missing",
                "path": "F:/__definitely_does_not_exist_555__",
            },
        )
        tool = ListFileRootsTool(_FakeSessionWithSettings(roots=roots))
        out = json.loads(tool.run({}))
        entry = out["roots"][0]
        self.assertFalse(entry["active"])
        self.assertTrue(entry["reason"])  # non-empty
        self.assertEqual(entry["preview"], [])
        self.assertFalse(entry["preview_truncated"])
        self.assertEqual(out["active_roots"], 0)

    def test_preview_caps_and_flags_truncation(self) -> None:
        # Build a temp dir with > cap entries.
        import tempfile
        from app.llm.tools.file_tasks import _LIST_FILE_ROOTS_PREVIEW_CAP

        with tempfile.TemporaryDirectory() as td:
            for i in range(_LIST_FILE_ROOTS_PREVIEW_CAP + 5):
                # Mix dirs and files so the sort path stays exercised.
                if i % 3 == 0:
                    os.makedirs(os.path.join(td, f"dir_{i:02d}"))
                else:
                    with open(os.path.join(td, f"file_{i:02d}.txt"), "w") as f:
                        f.write("x")
            roots = ({"label": "Tmp", "path": td},)
            tool = ListFileRootsTool(_FakeSessionWithSettings(roots=roots))
            out = json.loads(tool.run({}))
        entry = out["roots"][0]
        self.assertEqual(
            len(entry["preview"]), _LIST_FILE_ROOTS_PREVIEW_CAP,
        )
        self.assertTrue(entry["preview_truncated"])

    def test_hidden_dotfiles_excluded_from_preview(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, ".hidden"), "w") as f:
                f.write("x")
            with open(os.path.join(td, "visible.txt"), "w") as f:
                f.write("x")
            roots = ({"label": "Tmp", "path": td},)
            tool = ListFileRootsTool(_FakeSessionWithSettings(roots=roots))
            out = json.loads(tool.run({}))
        names = {row["name"] for row in out["roots"][0]["preview"]}
        self.assertIn("visible.txt", names)
        self.assertNotIn(".hidden", names)

    def test_malformed_entries_skipped(self) -> None:
        # Entries missing label or path, and non-dict entries, must
        # not crash the tool — the configured catalogue can be
        # partially broken without the agent surface falling over.
        roots = (
            {"label": "", "path": self.repo_root},  # empty label
            {"label": "NoPath", "path": ""},        # empty path
            "not-a-dict",                            # non-dict
            {"label": "Repo", "path": self.repo_root},  # valid
        )
        tool = ListFileRootsTool(_FakeSessionWithSettings(roots=roots))
        out = json.loads(tool.run({}))
        self.assertEqual(out["total_roots"], 1)
        self.assertEqual(out["roots"][0]["label"], "Repo")

    def test_missing_settings_returns_empty_catalogue(self) -> None:
        # A partially-built session (no _settings) must not crash —
        # tests sometimes construct minimal fakes. The tool should
        # report zero roots and move on.
        class _Bare:
            _user_id = "x"
            _task_orchestrator = None

        tool = ListFileRootsTool(_Bare())
        out = json.loads(tool.run({}))
        self.assertEqual(out["total_roots"], 0)
        self.assertEqual(out["active_roots"], 0)


# ── duration-hybrid inline fast-path ─────────────────────────────────────


class _AgentCfgGrace:
    def __init__(
        self,
        *,
        grace: float = 3.0,
        reply_on_complete: bool = True,
    ) -> None:
        self.task_inline_grace_seconds = grace
        self.task_reply_on_complete_enabled = reply_on_complete


class _SettingsGrace:
    def __init__(self, agent: _AgentCfgGrace) -> None:
        self.agent = agent


class _SessionGrace(_FakeSession):
    def __init__(
        self,
        *,
        orchestrator: _FakeOrchestrator,
        grace: float = 3.0,
        reply_on_complete: bool = True,
    ) -> None:
        super().__init__(orchestrator=orchestrator)
        self._settings = _SettingsGrace(
            _AgentCfgGrace(grace=grace, reply_on_complete=reply_on_complete)
        )
        self._active_turn_user_text = ""


class InlineFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _FakeOrchestrator()
        self.session = _FakeSession(orchestrator=self.orch)
        self.read = StartFileReadTool(self.session)
        self.search = StartFileSearchTool(self.session)

    def test_async_payload_when_wait_times_out(self) -> None:
        out = json.loads(self.read.run({"path": "x.md"}))
        self.assertEqual(out["status"], "running")
        self.assertIn("automatically", out["note"])
        # reply_when_done is flagged so the controller fires the
        # aggregated proactive reply on completion.
        meta = self.orch.start_calls[0]["metadata"]
        self.assertTrue(meta["reply_when_done"])

    def test_inline_done_folds_read_content(self) -> None:
        self.orch.wait_status = "done"
        self.orch.rows[7] = _FakeRow(
            result={
                "content": "hello world",
                "line_count": 1,
                "label": "Docs",
                "relative_path": "x.md",
            }
        )
        out = json.loads(self.read.run({"path": "Docs:x.md"}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["content"], "hello world")
        self.assertIn("do NOT start another read", out["note"])

    def test_inline_done_folds_search_matches(self) -> None:
        self.orch.wait_status = "done"
        self.orch.rows[7] = _FakeRow(
            result={
                "match_count": 1,
                "matches": [{"label": "Docs", "relative_path": "a.md"}],
                "summary": "found 1 file(s)",
                "truncated": False,
            }
        )
        out = json.loads(self.search.run({"query": "a"}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["match_count"], 1)
        self.assertEqual(len(out["matches"]), 1)

    def test_inline_failed_read_reports_error(self) -> None:
        self.orch.wait_status = "failed"
        self.orch.rows[7] = _FakeRow(error="boom")
        out = json.loads(self.read.run({"path": "x.md"}))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["error"], "boom")

    def test_origin_prompt_recorded_in_metadata(self) -> None:
        self.session._active_turn_user_text = "please read x.md"  # type: ignore[attr-defined]
        self.read.run({"path": "x.md"})
        meta = self.orch.start_calls[0]["metadata"]
        self.assertEqual(meta["origin_prompt"], "please read x.md")

    def test_grace_zero_skips_wait_entirely(self) -> None:
        session = _SessionGrace(orchestrator=self.orch, grace=0.0)
        self.orch.wait_status = "done"  # would fold if consulted
        out = json.loads(StartFileReadTool(session).run({"path": "x.md"}))
        self.assertEqual(out["status"], "running")
        self.assertEqual(self.orch.wait_calls, 0)

    def test_reply_on_complete_disabled_metadata(self) -> None:
        session = _SessionGrace(
            orchestrator=self.orch, grace=3.0, reply_on_complete=False
        )
        StartFileReadTool(session).run({"path": "x.md"})
        meta = self.orch.start_calls[0]["metadata"]
        self.assertFalse(meta["reply_when_done"])


# ── factory ──────────────────────────────────────────────────────────────


class BuildFileTaskToolsTests(unittest.TestCase):
    def test_factory_returns_five_tools_in_order(self) -> None:
        # ``list_file_roots`` (the discovery tool) comes first so an
        # LLM scanning the catalogue reads the natural flow:
        # discover → search → read → cancel → answer.
        session = _FakeSession(orchestrator=_FakeOrchestrator())
        tools = build_file_task_tools(session)
        names = [t.name for t in tools]
        self.assertEqual(
            names,
            [
                "list_file_roots",
                "start_file_search",
                "start_file_read",
                "cancel_file_task",
                "answer_file_task",
            ],
        )

    def test_factory_works_with_disabled_subsystem(self) -> None:
        # The factory itself doesn't gate on the subsystem state —
        # ``rebuild_tool_registry`` does. So calling the factory with
        # a None orchestrator must not crash; the tools will raise
        # ``ToolError`` at run time if actually invoked.
        session = _FakeSession(orchestrator=None)
        tools = build_file_task_tools(session)
        self.assertEqual(len(tools), 5)


if __name__ == "__main__":
    unittest.main()
