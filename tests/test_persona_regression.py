"""K10 persona regression — pure scorer / loader + controller smoke.

The pure marker matching (``score_reply``), JSONL loading
(``load_golden_turns`` with malformed-line skipping), and snapshot
aggregation (``build_snapshot``) are covered here without any LLM.

The controller smoke test exercises ``PersonaRegressionMixin`` via a
minimal stub host (mirrors ``tests/test_day_color_provider.py``) with a
fake chat client returning canned replies, asserting kv persistence +
snapshot shape.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.persona import persona_regression as pr
from app.core.session.persona_regression_mixin import PersonaRegressionMixin


# ── pure: parse / load ──────────────────────────────────────────────


class LoadGoldenTurnsTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8",
        )
        tmp.write(text)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_loads_valid_lines(self) -> None:
        path = self._write(
            json.dumps({"id": "a", "user": "hi", "scope": "minimal"})
            + "\n"
            + json.dumps(
                {
                    "id": "b",
                    "user": "yo",
                    "scope": "full",
                    "require_tags": ["[[reaction:"],
                    "forbid": ["as an ai"],
                },
            )
            + "\n",
        )
        turns = pr.load_golden_turns(path)
        self.assertEqual([t.id for t in turns], ["a", "b"])
        self.assertEqual(turns[1].scope, "full")
        self.assertEqual(turns[1].require_tags, ("[[reaction:",))
        self.assertEqual(turns[1].forbid, ("as an ai",))

    def test_skips_comments_and_blanks(self) -> None:
        path = self._write(
            "# a comment\n"
            "\n"
            + json.dumps({"id": "a", "user": "hi"})
            + "\n",
        )
        turns = pr.load_golden_turns(path)
        self.assertEqual(len(turns), 1)

    def test_skips_malformed_json(self) -> None:
        path = self._write(
            "{not valid json\n"
            + json.dumps({"id": "a", "user": "hi"})
            + "\n",
        )
        turns = pr.load_golden_turns(path)
        self.assertEqual([t.id for t in turns], ["a"])

    def test_skips_missing_required_fields(self) -> None:
        path = self._write(
            json.dumps({"id": "a"})  # no user
            + "\n"
            + json.dumps({"user": "hi"})  # no id
            + "\n"
            + json.dumps({"id": "ok", "user": "hi"})
            + "\n",
        )
        turns = pr.load_golden_turns(path)
        self.assertEqual([t.id for t in turns], ["ok"])

    def test_unknown_scope_falls_back_to_minimal(self) -> None:
        path = self._write(
            json.dumps({"id": "a", "user": "hi", "scope": "weird"}) + "\n",
        )
        turns = pr.load_golden_turns(path)
        self.assertEqual(turns[0].scope, "minimal")

    def test_duplicate_ids_skipped(self) -> None:
        path = self._write(
            json.dumps({"id": "a", "user": "one"})
            + "\n"
            + json.dumps({"id": "a", "user": "two"})
            + "\n",
        )
        turns = pr.load_golden_turns(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].user, "one")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(
            pr.load_golden_turns("/no/such/fixture.jsonl"), [],
        )

    def test_shipped_fixture_loads(self) -> None:
        turns = pr.load_golden_turns("data/persona/golden_turns.jsonl")
        self.assertGreaterEqual(len(turns), 5)
        self.assertTrue(all(t.id and t.user for t in turns))


# ── pure: score ─────────────────────────────────────────────────────


class ScoreReplyTests(unittest.TestCase):
    def test_clean_pass(self) -> None:
        turn = pr.GoldenTurn(
            id="t",
            user="hi",
            require_tags=("[[reaction:",),
            forbid=("as an ai",),
        )
        result = pr.score_reply(
            "[[reaction:warm]] hey, missed you", turn,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.failures, ())

    def test_missing_required_tag_fails(self) -> None:
        turn = pr.GoldenTurn(id="t", user="hi", require_tags=("[[reaction:",))
        result = pr.score_reply("just words, no tag", turn)
        self.assertFalse(result.passed)
        self.assertTrue(any("missing tag" in f for f in result.failures))

    def test_forbidden_phrase_fails_case_insensitive(self) -> None:
        turn = pr.GoldenTurn(id="t", user="hi", forbid=("as an ai",))
        result = pr.score_reply("Well, AS AN AI, I cannot do that", turn)
        self.assertFalse(result.passed)
        self.assertTrue(any("forbidden" in f for f in result.failures))

    def test_require_any_pass_and_fail(self) -> None:
        turn = pr.GoldenTurn(
            id="t", user="hi", require_any=("hey", "yo", "hi"),
        )
        self.assertTrue(pr.score_reply("yo what's up", turn).passed)
        miss = pr.score_reply("greetings, human", turn)
        self.assertFalse(miss.passed)
        self.assertTrue(any("require_any" in f for f in miss.failures))

    def test_require_all_fails_on_any_missing(self) -> None:
        turn = pr.GoldenTurn(
            id="t", user="hi", require_all=("alpha", "beta"),
        )
        result = pr.score_reply("only alpha here", turn)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("missing require_all" in f for f in result.failures),
        )

    def test_preview_truncated(self) -> None:
        turn = pr.GoldenTurn(id="t", user="hi")
        result = pr.score_reply("x" * 500, turn)
        self.assertLessEqual(len(result.reply_preview), 200)

    def test_empty_reply_only_fails_positive_markers(self) -> None:
        turn = pr.GoldenTurn(
            id="t", user="hi", require_tags=("[[reaction:",), forbid=("ai",),
        )
        result = pr.score_reply("", turn)
        self.assertFalse(result.passed)
        # forbid should NOT trip on empty text
        self.assertFalse(any("forbidden" in f for f in result.failures))


# ── pure: snapshot ──────────────────────────────────────────────────


class BuildSnapshotTests(unittest.TestCase):
    def test_aggregates_counts(self) -> None:
        results = [
            pr.GoldenResult(id="a", scope="minimal", passed=True),
            pr.GoldenResult(
                id="b", scope="full", passed=False, failures=("x",),
            ),
        ]
        snap = pr.build_snapshot(results, model="m", ran_ms=12.34)
        self.assertEqual(snap["total"], 2)
        self.assertEqual(snap["passed"], 1)
        self.assertEqual(snap["failed"], 1)
        self.assertEqual(snap["model"], "m")
        self.assertEqual(snap["ran_ms"], 12.3)
        self.assertEqual(len(snap["results"]), 2)
        self.assertIn("ran_at", snap)

    def test_error_field_present_when_set(self) -> None:
        snap = pr.build_snapshot([], error="disabled")
        self.assertEqual(snap["error"], "disabled")
        self.assertEqual(snap["total"], 0)


# ── controller smoke ────────────────────────────────────────────────


class _FakeChatDb:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self._store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self._store[key] = value


class _FakeAssembler:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_eval_messages(
        self,
        user_text: str,
        *,
        full_context: bool,
        session_key: str = "",
        context_window: int = 0,
        response_budget: int = 0,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {"user": user_text, "full_context": full_context},
        )
        return [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": user_text},
        ]


class _FakeClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.chat_calls = 0

    def chat(self, messages, options=None, model=None, **kwargs) -> str:
        self.chat_calls += 1
        return self.reply


class _Host(PersonaRegressionMixin):
    def __init__(self, reply: str, *, enabled: bool = True) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                persona_regression_enabled=enabled,
                persona_regression_fixture_path=(
                    "data/persona/golden_turns.jsonl"
                ),
            ),
        )
        self._prompt_assembler = _FakeAssembler()
        self._maintenance_client = _FakeClient(reply)
        self._chat_db = _FakeChatDb()
        self._effective_worker_model = "fake-model"
        self.context_window_size = 8192
        self.session_key = "u:main"


class ControllerSmokeTests(unittest.TestCase):
    def test_run_persists_and_returns_snapshot(self) -> None:
        # A reply that passes the strongest markers across all turns.
        reply = "[[reaction:warm]] hey, that sounds rough, I'm right here"
        host = _Host(reply)
        snap = host.run_persona_regression()
        self.assertGreater(snap["total"], 0)
        self.assertEqual(snap["model"], "fake-model")
        # snapshot persisted to kv_meta
        stored = host.persona_regression_snapshot()
        self.assertEqual(stored["total"], snap["total"])
        # the worker LLM was called once per turn
        self.assertEqual(
            host._maintenance_client.chat_calls, snap["total"],
        )

    def test_full_scope_turn_requests_full_context(self) -> None:
        host = _Host("[[reaction:warm]] hi")
        host.run_persona_regression()
        # the shipped fixture has at least one full-scope turn
        self.assertTrue(
            any(c["full_context"] for c in host._prompt_assembler.calls),
        )

    def test_disabled_returns_error_no_calls(self) -> None:
        host = _Host("whatever", enabled=False)
        snap = host.run_persona_regression()
        self.assertEqual(snap["error"], "disabled")
        self.assertEqual(host._maintenance_client.chat_calls, 0)

    def test_snapshot_empty_before_run(self) -> None:
        host = _Host("x")
        self.assertEqual(host.persona_regression_snapshot(), {})

    def test_corporate_reply_fails(self) -> None:
        host = _Host("As an AI language model, I cannot have feelings.")
        snap = host.run_persona_regression()
        self.assertGreater(snap["failed"], 0)


if __name__ == "__main__":
    unittest.main()
