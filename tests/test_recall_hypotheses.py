"""``recall_hypotheses``: letting Aiko look at what she is unsure about.

The lane holds up one or two open questions per turn, which is right for
a conversation and useless when the user asks outright ("what are you
still not sure about with me?"). Without a way to look she would either
confabulate a plausible list or deny having any.

Two things here are load-bearing rather than incidental:

* the two origins share a shape and are told apart by an ``origin``
  field. Collapsing them would let her present something she invented as
  something she noticed, which is the single failure the separate table
  exists to prevent;
* an empty result is a real answer. It must arrive as an empty list the
  tool description obliges her to report, never as a missing key she can
  read past.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.hypothesis_store import (
    STATUS_REFUTED,
    SUBJECT_WORLD,
    Hypothesis,
    HypothesisStore,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.llm.tools.builtins import RecallHypothesesTool, ToolError


def _ms(**over) -> SimpleNamespace:
    base = dict(
        hypothesis_min_unsettled=0.22,
        hypothesis_min_sources=1,
        hypothesis_max_open=12,
        hypothesis_ttl_hours=336.0,
        hypothesis_graduate_min_support=2,
        hypothesis_graduate_min_credence=0.7,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Host(MemoryFacadeMixin):
    def __init__(self, *, concepts=None, hypotheses=None, settings=None):
        self._concept_store = concepts
        self._hypothesis_store = hypotheses
        self._memory_settings = settings or _ms()
        self._agent_settings = SimpleNamespace(
            hypothesis_invention_enabled=True,
            concept_hypothesis_ask_enabled=True,
        )


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = ChatDatabase(Path(tmp.name) / "test.db")
        self.concepts = ConceptStore(self.db)
        self.hypotheses = HypothesisStore(self.db)
        self.host = _Host(concepts=self.concepts, hypotheses=self.hypotheses)

    def _invented(
        self,
        statement: str = "Jacob would take to sailing",
        *,
        subject: str = "user",
        kind: str = "taste",
        credence: float = 0.5,
        support: int = 0,
        status: str | None = None,
        linked: int | None = None,
    ) -> Hypothesis:
        row = Hypothesis(
            statement=statement,
            subject=subject,
            kind=kind,
            credence=credence,
            support_count=support,
            rationale="he likes wind",
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        if status:
            row.status = status
        row.linked_concept_id = linked
        self.hypotheses.add(row)
        return row

    def _candidate(
        self,
        label: str = "Jacob treats walking as thinking time",
        *,
        subject: str = "user",
        kind: str = "pattern",
        status: str = "candidate",
        confidence: float = 0.5,
        sources: int = 1,
    ) -> Concept:
        c = Concept(
            label=label,
            kind=kind,
            subject=subject,
            status=status,
            confidence=confidence,
            evidence_count=sources,
            distinct_source_count=sources,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        c.concept_id = self.concepts.add(c)
        return c

    def _statements(self, payload: dict) -> list[str]:
        return [h["statement"] for h in payload["hypotheses"]]


# ── the facade ────────────────────────────────────────────────────────


class FacadeTests(_Fixture):
    def test_it_returns_both_origins_together(self) -> None:
        self._invented()
        self._candidate()

        payload = self.host.open_hypotheses()

        self.assertEqual(
            {h["origin"] for h in payload["hypotheses"]},
            {"invented", "grounded"},
        )

    def test_an_invention_is_labelled_as_one(self) -> None:
        """Otherwise she can present a guess as something she noticed."""
        self._invented()

        row = self.host.open_hypotheses()["hypotheses"][0]

        self.assertEqual(row["origin"], "invented")
        self.assertIn("credence", row)
        self.assertNotIn("confidence", row)

    def test_a_grounded_row_carries_confidence_not_credence(self) -> None:
        self._candidate()

        row = self.host.open_hypotheses()["hypotheses"][0]

        self.assertEqual(row["origin"], "grounded")
        self.assertIn("confidence", row)
        self.assertNotIn("credence", row)

    def test_the_least_settled_comes_first(self) -> None:
        """The point of looking is to find what is most open."""
        self._invented("nearly settled", credence=0.9, support=2)
        self._invented("wide open", credence=0.5, support=0)

        got = self._statements(self.host.open_hypotheses())

        self.assertEqual(got[0], "wide open")

    def test_an_empty_shelf_is_an_answer_not_a_gap(self) -> None:
        payload = self.host.open_hypotheses()

        self.assertEqual(payload["hypotheses"], [])
        self.assertEqual(payload["total"], 0)

    def test_a_linked_guess_is_hidden(self) -> None:
        """A concept speaks for the belief now; listing both shows one
        thing twice with two different confidence stories attached."""
        self._invented("already a belief", linked=99)

        self.assertEqual(self.host.open_hypotheses()["hypotheses"], [])

    def test_a_closed_guess_is_hidden(self) -> None:
        self._invented("she was wrong", status=STATUS_REFUTED)

        self.assertEqual(self.host.open_hypotheses()["hypotheses"], [])

    def test_an_active_concept_is_not_an_open_question(self) -> None:
        self._candidate(status="active")

        self.assertEqual(self.host.open_hypotheses()["hypotheses"], [])

    def test_a_settled_candidate_is_filtered_out(self) -> None:
        """Most candidates are beliefs waiting out the age floor, not doubts."""
        self._candidate(confidence=1.0, sources=6)

        self.assertEqual(self.host.open_hypotheses()["hypotheses"], [])

    def test_an_ungrounded_candidate_is_filtered_out(self) -> None:
        self._candidate(sources=0)

        self.assertEqual(self.host.open_hypotheses()["hypotheses"], [])

    def test_the_limit_is_honoured(self) -> None:
        for i in range(5):
            self._invented(f"guess {i}", credence=0.1 * i)

        payload = self.host.open_hypotheses(limit=2)

        self.assertEqual(len(payload["hypotheses"]), 2)
        self.assertEqual(payload["total"], 5)

    def test_it_filters_by_origin(self) -> None:
        self._invented()
        self._candidate()

        self.assertEqual(
            self._statements(self.host.open_hypotheses(origin="invented")),
            ["Jacob would take to sailing"],
        )

    def test_it_filters_by_subject(self) -> None:
        self._invented("about the world", subject=SUBJECT_WORLD)
        self._invented("about him")

        self.assertEqual(
            self._statements(self.host.open_hypotheses(subject="world")),
            ["about the world"],
        )

    def test_it_filters_by_kind(self) -> None:
        self._invented("a taste", kind="taste")
        self._invented("a ritual", kind="ritual")

        self.assertEqual(
            self._statements(self.host.open_hypotheses(kind="ritual")),
            ["a ritual"],
        )

    def test_a_missing_hypothesis_table_still_serves_the_grounded_half(
        self,
    ) -> None:
        """'She has hunches but never invented one' is a normal state."""
        host = _Host(concepts=self.concepts, hypotheses=None)
        self._candidate()

        self.assertEqual(len(host.open_hypotheses()["hypotheses"]), 1)

    def test_a_cold_layer_returns_the_empty_shape(self) -> None:
        payload = _Host().open_hypotheses()

        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["hypotheses"], [])

    def test_a_raising_store_is_absorbed(self) -> None:
        class _Broken:
            def list_by(self, **_k):
                raise RuntimeError("db gone")

        host = _Host(concepts=self.concepts, hypotheses=_Broken())
        self._candidate()

        self.assertEqual(len(host.open_hypotheses()["hypotheses"]), 1)


class StateTests(_Fixture):
    def test_it_reports_stock_against_the_cap(self) -> None:
        self._invented()

        state = self.host.hypothesis_state()

        self.assertEqual(state["live"], 1)
        self.assertEqual(state["max_open"], 12)

    def test_it_tells_the_exits_apart(self) -> None:
        self._invented("refuted one", status=STATUS_REFUTED)
        self._invented("open one")

        by_status = self.host.hypothesis_state()["by_status"]

        self.assertEqual(by_status.get(STATUS_REFUTED), 1)

    def test_it_counts_the_linked_rows(self) -> None:
        """A full shelf of linked rows is the usual quiet-lane explanation."""
        self._invented("linked", linked=5)

        self.assertEqual(self.host.hypothesis_state()["linked"], 1)

    def test_a_cold_layer_reports_no_store(self) -> None:
        state = _Host().hypothesis_state()

        self.assertFalse(state["store"])
        self.assertEqual(state["live"], 0)


# ── the tool ──────────────────────────────────────────────────────────


class ToolTests(_Fixture):
    def _tool(self) -> RecallHypothesesTool:
        return RecallHypothesesTool(self.host.open_hypotheses)

    def _run(self, **args) -> dict:
        return json.loads(self._tool().run(args))

    def test_it_returns_the_facade_payload_as_json(self) -> None:
        self._invented()

        got = self._run()

        self.assertEqual(
            got["hypotheses"][0]["statement"], "Jacob would take to sailing"
        )

    def test_an_empty_shelf_comes_back_as_an_empty_list(self) -> None:
        self.assertEqual(self._run()["hypotheses"], [])

    def test_a_bogus_subject_is_ignored_rather_than_passed_through(
        self,
    ) -> None:
        self._invented()

        self.assertEqual(len(self._run(subject="the moon")["hypotheses"]), 1)

    def test_a_bogus_origin_is_ignored(self) -> None:
        self._invented()

        self.assertEqual(len(self._run(origin="dreamt")["hypotheses"]), 1)

    def test_the_limit_is_clamped(self) -> None:
        seen: dict = {}

        def _provider(**kwargs):
            seen.update(kwargs)
            return {"hypotheses": []}

        RecallHypothesesTool(_provider).run({"limit": 9000})

        self.assertEqual(seen["limit"], 25)

    def test_a_junk_limit_falls_back_to_the_default(self) -> None:
        seen: dict = {}

        def _provider(**kwargs):
            seen.update(kwargs)
            return {"hypotheses": []}

        RecallHypothesesTool(_provider).run({"limit": "lots"})

        self.assertEqual(seen["limit"], 10)

    def test_a_missing_provider_is_an_explicit_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            RecallHypothesesTool(None).run({})

    def test_a_raising_provider_is_an_explicit_tool_error(self) -> None:
        def _boom(**_k):
            raise RuntimeError("db gone")

        with self.assertRaises(ToolError):
            RecallHypothesesTool(_boom).run({})

    def test_the_schema_tells_the_two_origins_apart(self) -> None:
        """The description is the only thing stopping her from
        presenting an invention as an observation."""
        schema = self._tool().schema()

        self.assertEqual(schema.name, "recall_hypotheses")
        self.assertIn("invented", schema.description)
        self.assertIn("grounded", schema.description)

    def test_the_schema_forbids_inventing_a_list_when_empty(self) -> None:
        self.assertIn("empty list", self._tool().schema().description)

    def test_it_routes_to_the_recall_family(self) -> None:
        from app.core.session.tool_pass_gate import _TOOL_FAMILY

        self.assertEqual(_TOOL_FAMILY["recall_hypotheses"], "recall")

    def test_asking_what_she_wonders_runs_the_tool_pass(self) -> None:
        from app.core.session.tool_pass_gate import (
            GateContext,
            should_run_tool_pass,
        )

        decision = should_run_tool_pass(
            "what are you still unsure about with me?",
            ["recall_hypotheses"],
            context=GateContext(),
        )

        self.assertTrue(decision.run)

    def test_ordinary_banter_still_skips_the_pass(self) -> None:
        from app.core.session.tool_pass_gate import (
            GateContext,
            should_run_tool_pass,
        )

        decision = should_run_tool_pass(
            "that sounds lovely, I wonder how the weekend will go",
            ["recall_hypotheses"],
            context=GateContext(),
        )

        self.assertFalse(decision.run)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
