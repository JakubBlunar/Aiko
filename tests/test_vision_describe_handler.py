"""Tests for VisionDescribeHandler + the describe_image skill gating.

The vision call is faked (a subclass of :class:`OllamaClient` that
records the call and returns a canned description) so the suite never
touches the network or a real model.
"""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.core.tasks.handlers.vision_describe import VisionDescribeHandler
from app.core.tasks.sandbox import FileTaskRoot
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskFailed,
    TaskInputNeeded,
)
from app.core.tasks.workflow.skill_registry import (
    WORKFLOW_SKILL_DESCRIBE_IMAGE,
    build_builtin_skill_registry,
)
from app.llm.ollama_client import OllamaClient


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _Emitter:
    def __init__(self) -> None:
        self.outcomes: list[Any] = []

    def __call__(self, outcome: Any) -> None:
        self.outcomes.append(outcome)

    def last(self) -> Any:
        return self.outcomes[-1] if self.outcomes else None

    def has(self, cls: type) -> bool:
        return any(isinstance(o, cls) for o in self.outcomes)

    def first(self, cls: type) -> Any:
        for o in self.outcomes:
            if isinstance(o, cls):
                return o
        return None


class _FakeVisionClient(OllamaClient):
    """An OllamaClient whose ``chat`` is canned (no network)."""

    def __init__(self, response: str = "a sleepy orange cat", raise_exc: Exception | None = None) -> None:
        # Deliberately skip super().__init__ — we never touch the network.
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._raise = raise_exc

    def chat(  # type: ignore[override]
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        model: str | None = None,
        think: bool = False,
        *,
        surface: str = "chat",
    ) -> str:
        self.calls.append(
            {"messages": messages, "model": model, "think": think, "surface": surface}
        )
        if self._raise is not None:
            raise self._raise
        return self._response


class VisionDescribeHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir_a = Path(self._tmp.name) / "alpha"
        self.dir_b = Path(self._tmp.name) / "beta"
        self.dir_a.mkdir()
        self.dir_b.mkdir()
        self.root_a = FileTaskRoot(label="Alpha", path=str(self.dir_a), read_only=True)
        self.root_b = FileTaskRoot(label="Beta", path=str(self.dir_b), read_only=True)
        (self.dir_a / "photo.png").write_bytes(_PNG_BYTES)

    def _handler(
        self,
        *,
        roots: list[FileTaskRoot] | None = None,
        client: Any = "default",
        model: str = "qwen3.5:27b",
        max_bytes: int = 8 * 1024 * 1024,
        allowed: tuple[str, ...] = (".png", ".jpg"),
    ) -> tuple[VisionDescribeHandler, _FakeVisionClient | None]:
        fake = _FakeVisionClient() if client == "default" else client
        handler = VisionDescribeHandler(
            roots=roots if roots is not None else [self.root_a],
            client_provider=(lambda: fake),
            model_provider=(lambda: model),
            max_bytes=max_bytes,
            allowed_extensions=allowed,
        )
        return handler, (fake if isinstance(fake, _FakeVisionClient) else None)

    # ── happy path ───────────────────────────────────────────────────

    def test_single_root_success(self) -> None:
        h, fake = self._handler()
        emit = _Emitter()
        h.start({"path": "Alpha:photo.png"}, emit)
        self.assertTrue(emit.has(TaskCompleted))
        result = emit.first(TaskCompleted).result
        self.assertEqual(result["description"], "a sleepy orange cat")
        self.assertEqual(result["summary"], "a sleepy orange cat")
        self.assertEqual(result["content"], "a sleepy orange cat")
        self.assertEqual(result["model"], "qwen3.5:27b")
        # The image was base64-encoded and attached to the message.
        self.assertEqual(len(fake.calls), 1)
        msg = fake.calls[0]["messages"][0]
        self.assertEqual(msg["images"], [base64.b64encode(_PNG_BYTES).decode("ascii")])

    def test_bare_path_single_root(self) -> None:
        h, _ = self._handler()
        emit = _Emitter()
        h.start({"path": "photo.png"}, emit)
        self.assertTrue(emit.has(TaskCompleted))

    def test_default_prompt_used_when_no_question(self) -> None:
        h, fake = self._handler()
        emit = _Emitter()
        h.start({"path": "Alpha:photo.png"}, emit)
        content = fake.calls[0]["messages"][0]["content"]
        self.assertIn("describe", content.lower())

    def test_question_passed_through(self) -> None:
        h, fake = self._handler()
        emit = _Emitter()
        h.start({"path": "Alpha:photo.png", "question": "What colour is it?"}, emit)
        self.assertEqual(
            fake.calls[0]["messages"][0]["content"], "What colour is it?"
        )

    # ── multi-root disambiguation ────────────────────────────────────

    def test_multi_root_awaiting_then_resolve(self) -> None:
        (self.dir_b / "photo.png").write_bytes(_PNG_BYTES)
        h, _ = self._handler(roots=[self.root_a, self.root_b])
        emit = _Emitter()
        state = h.start({"path": "photo.png"}, emit)
        self.assertTrue(emit.has(TaskInputNeeded))
        self.assertEqual(state["phase"], "awaiting_disambiguation")
        emit2 = _Emitter()
        h.on_input(state, "Alpha:photo.png", emit2)
        self.assertTrue(emit2.has(TaskCompleted))

    # ── gating / failures ────────────────────────────────────────────

    def test_extension_not_allowed(self) -> None:
        (self.dir_a / "note.txt").write_bytes(b"hello")
        h, _ = self._handler(allowed=(".png",))
        emit = _Emitter()
        h.start({"path": "Alpha:note.txt"}, emit)
        self.assertTrue(emit.has(TaskFailed))
        self.assertIn("extension", emit.first(TaskFailed).error.lower())

    def test_byte_cap_enforced(self) -> None:
        (self.dir_a / "big.png").write_bytes(b"x" * 4096)
        h, _ = self._handler(max_bytes=1024)
        emit = _Emitter()
        h.start({"path": "Alpha:big.png"}, emit)
        self.assertTrue(emit.has(TaskFailed))
        self.assertIn("too large", emit.first(TaskFailed).error.lower())

    def test_missing_file_fails(self) -> None:
        h, _ = self._handler()
        emit = _Emitter()
        h.start({"path": "Alpha:nope.png"}, emit)
        self.assertTrue(emit.has(TaskFailed))

    def test_empty_path_rejected(self) -> None:
        h, _ = self._handler()
        emit = _Emitter()
        h.start({"path": "   "}, emit)
        self.assertTrue(emit.has(TaskFailed))

    def test_missing_client_fails(self) -> None:
        h = VisionDescribeHandler(
            roots=[self.root_a],
            client_provider=(lambda: None),
            model_provider=(lambda: "qwen3.5:27b"),
            allowed_extensions=(".png",),
        )
        emit = _Emitter()
        h.start({"path": "Alpha:photo.png"}, emit)
        self.assertTrue(emit.has(TaskFailed))
        self.assertIn("unavailable", emit.first(TaskFailed).error.lower())

    def test_non_ollama_client_fails(self) -> None:
        h, _ = self._handler(client=object())
        emit = _Emitter()
        h.start({"path": "Alpha:photo.png"}, emit)
        self.assertTrue(emit.has(TaskFailed))
        self.assertIn("image", emit.first(TaskFailed).error.lower())

    def test_model_not_found_friendly_error(self) -> None:
        fake = _FakeVisionClient(raise_exc=RuntimeError("model 'foo' not found"))
        h, _ = self._handler(client=fake)
        emit = _Emitter()
        h.start({"path": "Alpha:photo.png"}, emit)
        self.assertTrue(emit.has(TaskFailed))
        self.assertIn("pull", emit.first(TaskFailed).error.lower())

    def test_empty_description_fails(self) -> None:
        fake = _FakeVisionClient(response="   ")
        h, _ = self._handler(client=fake)
        emit = _Emitter()
        h.start({"path": "Alpha:photo.png"}, emit)
        self.assertTrue(emit.has(TaskFailed))
        self.assertIn("empty", emit.first(TaskFailed).error.lower())

    def test_no_active_roots_fails(self) -> None:
        missing = FileTaskRoot(
            label="Gone", path=str(Path(self._tmp.name) / "missing"), read_only=True
        )
        h, _ = self._handler(roots=[missing])
        emit = _Emitter()
        h.start({"path": "photo.png"}, emit)
        self.assertTrue(emit.has(TaskFailed))

    def test_cancel_noop(self) -> None:
        h, _ = self._handler()
        self.assertIsNone(h.cancel({"phase": "done"}))


class DescribeImageSkillGatingTests(unittest.TestCase):
    def test_skill_absent_by_default(self) -> None:
        reg = build_builtin_skill_registry(vision_enabled=False)
        self.assertNotIn(WORKFLOW_SKILL_DESCRIBE_IMAGE, reg.names())

    def test_skill_present_when_enabled(self) -> None:
        reg = build_builtin_skill_registry(vision_enabled=True)
        self.assertIn(WORKFLOW_SKILL_DESCRIBE_IMAGE, reg.names())


if __name__ == "__main__":
    unittest.main()
