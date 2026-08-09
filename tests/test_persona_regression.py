"""K10 persona regression — pure scorer / loader + controller smoke.

The pure marker matching (``score_reply``), JSONL loading
(``load_golden_turns`` with malformed-line skipping), and snapshot
aggregation (``build_snapshot``) are covered here without any LLM.

The controller smoke test exercises ``PersonaRegressionMixin`` via a
minimal stub host (mirrors ``tests/test_day_color_provider.py``) with a
fake chat client returning canned replies, asserting kv persistence +
snapshot shape.

The K10-followup background worker
(:class:`~app.core.proactive.persona_regression_worker.PersonaRegressionWorker`)
is covered at the bottom: its gates, its cadence, and the
regressed/recovered diff it computes against the previous snapshot.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.persona import persona_regression as pr
from app.core.proactive import persona_regression_worker as prw
from app.core.session.persona_regression_mixin import PersonaRegressionMixin


_NOW = datetime(2026, 7, 31, 3, 0, tzinfo=timezone.utc)


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


class AnaphoricMarkerTests(unittest.TestCase):
    """K88/K90 -- the fixture's only non-substring marker."""

    def _turn(self) -> pr.GoldenTurn:
        return pr.GoldenTurn(id="t", user="hi", forbid_anaphoric=True)

    def test_an_echo_opener_fails(self) -> None:
        result = pr.score_reply("So am I, honestly.", self._turn())
        self.assertFalse(result.passed)
        self.assertIn("forbidden: anaphoric opener", result.failures)

    def test_leading_with_her_own_clause_passes(self) -> None:
        result = pr.score_reply(
            "Same, though mine's mostly about the lamps going on early.",
            self._turn(),
        )
        self.assertTrue(result.passed)

    def test_a_particle_in_front_of_her_own_clause_is_fine(self) -> None:
        # The marker is about the grammar of the opening clause, not a
        # ban on warm noises.
        result = pr.score_reply(
            "Oh, I finally got the basil to behave.", self._turn(),
        )
        self.assertTrue(result.passed)

    def test_off_by_default(self) -> None:
        turn = pr.GoldenTurn(id="t", user="hi")
        self.assertTrue(pr.score_reply("Exactly.", turn).passed)

    def test_empty_reply_has_no_opener_to_judge(self) -> None:
        result = pr.score_reply("", self._turn())
        self.assertNotIn("forbidden: anaphoric opener", result.failures)

    def test_the_flag_round_trips_through_the_fixture(self) -> None:
        turn = pr.parse_golden_turn(
            {"id": "t", "user": "hi", "forbid_anaphoric": True},
        )
        self.assertTrue(turn.forbid_anaphoric)
        self.assertFalse(
            pr.parse_golden_turn({"id": "t", "user": "hi"}).forbid_anaphoric,
        )

    def test_the_shipped_fixture_carries_anti_follow_cases(self) -> None:
        turns = pr.load_golden_turns("data/persona/golden_turns.jsonl")
        self.assertGreaterEqual(
            sum(1 for t in turns if t.forbid_anaphoric), 3,
        )


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


# ── K10-followup: the background worker ─────────────────────────────


def _snapshot(*rows: tuple[str, bool]) -> dict[str, Any]:
    """Minimal snapshot shaped like ``build_snapshot`` output."""
    results = [
        {"id": turn_id, "scope": "minimal", "passed": passed}
        for turn_id, passed in rows
    ]
    passed_n = sum(1 for _, p in rows if p)
    return {
        "total": len(rows),
        "passed": passed_n,
        "failed": len(rows) - passed_n,
        "results": results,
    }


class FailingIdsTests(unittest.TestCase):
    def test_reads_failed_ids(self) -> None:
        self.assertEqual(
            prw.failing_ids(_snapshot(("a", True), ("b", False))), {"b"},
        )

    def test_tolerates_junk(self) -> None:
        self.assertEqual(prw.failing_ids({}), set())
        self.assertEqual(prw.failing_ids({"results": "nope"}), set())
        self.assertEqual(
            prw.failing_ids({"results": ["x", {"passed": False}]}), set(),
        )


class PersonaRegressionWorkerTests(unittest.TestCase):
    def _worker(
        self,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        enabled: bool = True,
        run=None,
    ) -> prw.PersonaRegressionWorker:
        self.runs = 0

        def _run() -> dict[str, Any]:
            self.runs += 1
            return after if after is not None else _snapshot(("a", True))

        return prw.PersonaRegressionWorker(
            run_regression=run or _run,
            snapshot_provider=lambda: before if before is not None else {},
            enabled_provider=lambda: enabled,
            interval_seconds=86400.0,
        )

    def test_disabled_does_not_run(self) -> None:
        worker = self._worker(enabled=False)
        self.assertTrue(worker.run().get("disabled"))
        self.assertEqual(self.runs, 0)

    def test_disabled_is_never_ready(self) -> None:
        worker = self._worker(enabled=False)
        self.assertFalse(worker.is_ready(now=_NOW, last_run_at=None))

    def test_ready_when_never_run(self) -> None:
        self.assertTrue(self._worker().is_ready(now=_NOW, last_run_at=None))

    def test_readiness_ignores_the_interval(self) -> None:
        """Pacing is the scheduler's job after the demand migration.

        The daily interval is now the heartbeat, and since this worker
        reports zero pressure the heartbeat is the *only* thing that
        ever admits it — so the cadence is unchanged even though
        ``is_ready`` no longer looks at the clock.
        """
        worker = self._worker()
        recent = _NOW - timedelta(hours=1)
        self.assertTrue(worker.is_ready(now=_NOW, last_run_at=recent))

    def test_demand_is_pure_heartbeat(self) -> None:
        signal = self._worker().demand(now=_NOW, last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertTrue(signal.needs_llm)

    def test_interval_is_floored_at_an_hour(self) -> None:
        worker = prw.PersonaRegressionWorker(
            run_regression=dict,
            snapshot_provider=dict,
            interval_seconds=5.0,
        )
        self.assertEqual(worker.interval_seconds, 3600.0)

    def test_new_failure_is_reported(self) -> None:
        worker = self._worker(
            before=_snapshot(("a", True), ("b", True)),
            after=_snapshot(("a", True), ("b", False)),
        )
        result = worker.run()
        self.assertEqual(result["ran"], 1)
        self.assertEqual(result["regressed"], ["b"])
        self.assertEqual(result["recovered"], [])
        self.assertEqual(result["passed"], 1)

    def test_standing_failure_is_not_a_new_regression(self) -> None:
        worker = self._worker(
            before=_snapshot(("a", True), ("b", False)),
            after=_snapshot(("a", True), ("b", False)),
        )
        result = worker.run()
        self.assertEqual(result["regressed"], [])
        self.assertEqual(result["recovered"], [])

    def test_recovery_is_reported(self) -> None:
        worker = self._worker(
            before=_snapshot(("a", False)),
            after=_snapshot(("a", True)),
        )
        self.assertEqual(worker.run()["recovered"], ["a"])

    def test_first_ever_run_has_no_baseline(self) -> None:
        # No previous snapshot: every failure is "new", which is the
        # honest read -- there's nothing to compare against.
        worker = self._worker(before={}, after=_snapshot(("a", False)))
        self.assertEqual(worker.run()["regressed"], ["a"])

    def test_error_snapshot_is_not_counted_as_a_run(self) -> None:
        worker = self._worker(after={"error": "unavailable", "results": []})
        result = worker.run()
        self.assertEqual(result["ran"], 0)
        self.assertEqual(result["error"], "unavailable")

    def test_raising_core_is_swallowed(self) -> None:
        def _boom() -> dict[str, Any]:
            raise RuntimeError("no client")

        worker = self._worker(run=_boom)
        self.assertEqual(worker.run(), {"ran": 0, "error": "exception"})

    def test_raising_baseline_still_runs(self) -> None:
        def _boom() -> dict[str, Any]:
            raise RuntimeError("kv down")

        worker = prw.PersonaRegressionWorker(
            run_regression=lambda: _snapshot(("a", True)),
            snapshot_provider=_boom,
        )
        self.assertEqual(worker.run()["ran"], 1)

    def test_end_to_end_against_the_real_core(self) -> None:
        host = _Host("As an AI language model, I cannot have feelings.")
        worker = prw.PersonaRegressionWorker(
            run_regression=host.run_persona_regression,
            snapshot_provider=host.persona_regression_snapshot,
        )
        result = worker.run()
        self.assertEqual(result["ran"], 1)
        self.assertGreater(len(result["regressed"]), 0)
        # Second pass over the same failures: no longer *new*.
        self.assertEqual(worker.run()["regressed"], [])

    def test_master_switch_off_reports_the_error(self) -> None:
        host = _Host("x", enabled=False)
        worker = prw.PersonaRegressionWorker(
            run_regression=host.run_persona_regression,
            snapshot_provider=host.persona_regression_snapshot,
        )
        self.assertEqual(worker.run()["error"], "disabled")


if __name__ == "__main__":
    unittest.main()
