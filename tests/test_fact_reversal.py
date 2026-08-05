"""Tests for F14 -- the fact-checker reverses a claim Aiko told the user.

Two surfaces:

* the reversal gate inside
  (:meth:`app.core.memory.idle_fact_checker.IdleFactChecker._apply_verdict`)
  -- the bar (contradict + a delta clearing ``fact_reversal_min_delta`` +
  an actual content rewrite), the L37 surfaced gate, and the F13
  suppression (a row the user already corrected / an archived row);
* the controller cue-arm
  (:meth:`app.core.session.post_turn_helpers_mixin.PostTurnHelpersMixin.queue_fact_reversal_cue`)
  -- the low-key acknowledgment line, subject = the corrected fact.

The :class:`OllamaClient` / :class:`WebSearchTool` / queue are irrelevant
here: the gate is driven straight through ``_apply_verdict`` with a stub
memory store, and the cue-arm through a stub cue-pool store.
"""
from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from app.core.memory.idle_fact_checker import IdleFactChecker, Verdict
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


# ── fakes ──────────────────────────────────────────────────────────────


@dataclass
class _Row:
    id: int
    content: str
    kind: str = "fact"
    confidence: float = 0.85
    tier: str = "long_term"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "content": self.content}


class _FakeStore:
    """The slice of MemoryStore ``_apply_verdict`` touches."""

    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def get(self, memory_id: int) -> _Row | None:
        if self._row is not None and int(memory_id) == self._row.id:
            return self._row
        return None

    def update(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
        metadata_merge: bool = False,
        **_kw: Any,
    ) -> _Row | None:
        row = self._row
        if row is None:
            return None
        if content is not None:
            row.content = content
        if confidence is not None:
            row.confidence = float(confidence)
        if metadata is not None:
            if metadata_merge:
                row.metadata.update(metadata)
            else:
                row.metadata = dict(metadata)
        return row


def _checker(
    *,
    store: _FakeStore,
    surfaced: bool = True,
    min_delta: float = 0.25,
    enabled: bool = True,
) -> tuple[IdleFactChecker, list[dict[str, Any]]]:
    """A checker wired with just enough to exercise ``_apply_verdict``."""
    armed: list[dict[str, Any]] = []

    def _arm(*, wrong: str, corrected: str, memory_id: int) -> bool:
        armed.append(
            {"wrong": wrong, "corrected": corrected, "memory_id": memory_id}
        )
        return True

    worker = IdleFactChecker(
        queue=SimpleNamespace(),
        memory_store=store,
        agent_settings=SimpleNamespace(
            fact_checker_enabled=True, fact_reversal_enabled=enabled,
        ),
        memory_settings=SimpleNamespace(fact_reversal_min_delta=min_delta),
        ollama=SimpleNamespace(),
        chat_model="stub",
        web_search_tool=SimpleNamespace(),
        rate_limiter=SimpleNamespace(),
        cancel_event=threading.Event(),
        arm_reversal=_arm,
        was_surfaced=lambda _mid: surfaced,
    )
    return worker, armed


def _apply(
    worker: IdleFactChecker,
    memory_id: int,
    verdict: Verdict,
) -> None:
    claim = SimpleNamespace(memory_id=memory_id, claim_kind="fact")
    worker._apply_verdict(claim, verdict)  # noqa: SLF001


# ── the reversal bar ───────────────────────────────────────────────────


class ReversalBarTests(unittest.TestCase):
    def _row(self, **kw: Any) -> _Row:
        return _Row(id=5, content="Mercury is the largest planet.", **kw)

    def test_contradict_with_rewrite_and_big_delta_fires(self) -> None:
        row = self._row()
        worker, armed = _checker(store=_FakeStore(row))
        _apply(
            worker,
            5,
            Verdict(
                kind="contradict",
                delta=-0.3,
                rewrite="Jupiter is the largest planet.",
            ),
        )
        self.assertEqual(len(armed), 1)
        self.assertEqual(armed[0]["memory_id"], 5)
        # Wrong text = the pre-update content; corrected = the rewrite.
        self.assertEqual(armed[0]["wrong"], "Mercury is the largest planet.")
        self.assertEqual(
            armed[0]["corrected"], "Jupiter is the largest planet.",
        )

    def test_support_verdict_never_fires(self) -> None:
        worker, armed = _checker(store=_FakeStore(self._row()))
        _apply(worker, 5, Verdict(kind="support", delta=0.2, rewrite=None))
        self.assertEqual(armed, [])

    def test_inconclusive_never_fires(self) -> None:
        worker, armed = _checker(store=_FakeStore(self._row()))
        _apply(worker, 5, Verdict(kind="inconclusive", delta=0.0, rewrite=None))
        self.assertEqual(armed, [])

    def test_contradict_without_rewrite_does_not_fire(self) -> None:
        """A confidence drop with no content rewrite is drift, not a beat."""
        worker, armed = _checker(store=_FakeStore(self._row()))
        _apply(worker, 5, Verdict(kind="contradict", delta=-0.3, rewrite=None))
        self.assertEqual(armed, [])

    def test_delta_below_bar_does_not_fire(self) -> None:
        """Rewrite present but the drop is below ``fact_reversal_min_delta``."""
        # |0.22| clears the 0.2 rewrite-accept threshold (so the content is
        # rewritten) but not the 0.25 reversal bar.
        worker, armed = _checker(store=_FakeStore(self._row()), min_delta=0.25)
        _apply(
            worker,
            5,
            Verdict(kind="contradict", delta=-0.22, rewrite="Jupiter is."),
        )
        self.assertEqual(armed, [])

    def test_master_switch_off_does_not_fire(self) -> None:
        worker, armed = _checker(store=_FakeStore(self._row()), enabled=False)
        _apply(
            worker,
            5,
            Verdict(kind="contradict", delta=-0.3, rewrite="Jupiter is."),
        )
        self.assertEqual(armed, [])


# ── the surfaced gate + F13 suppression ────────────────────────────────


class SurfacedGateTests(unittest.TestCase):
    def _fire(
        self, worker: IdleFactChecker, mid: int = 5,
    ) -> None:
        _apply(
            worker,
            mid,
            Verdict(
                kind="contradict",
                delta=-0.3,
                rewrite="Jupiter is the largest planet.",
            ),
        )

    def test_never_surfaced_memory_is_not_owned(self) -> None:
        row = _Row(id=5, content="Mercury is the largest planet.")
        worker, armed = _checker(store=_FakeStore(row), surfaced=False)
        self._fire(worker)
        self.assertEqual(armed, [])

    def test_user_correction_supersede_suppresses(self) -> None:
        row = _Row(
            id=5,
            content="Mercury is the largest planet.",
            metadata={"superseded_by": 9, "superseded_reason": "user_correction"},
        )
        worker, armed = _checker(store=_FakeStore(row))
        self._fire(worker)
        self.assertEqual(armed, [])

    def test_archived_row_suppresses(self) -> None:
        row = _Row(
            id=5, content="Mercury is the largest planet.", tier="archive",
        )
        worker, armed = _checker(store=_FakeStore(row))
        self._fire(worker)
        self.assertEqual(armed, [])


# ── the cue-arm ────────────────────────────────────────────────────────


class _FakeCueStore:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add(
        self,
        cue_type: str,
        subject: str,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        ttl_hours: float | None = None,
        hold_hours: float = 0.0,
    ) -> int:
        self.added.append(
            {
                "cue_type": cue_type,
                "subject": subject,
                "text": text,
                "payload": dict(payload or {}),
                "ttl_hours": ttl_hours,
            }
        )
        return len(self.added)


class _ArmHost(PostTurnHelpersMixin, CuePoolMixin):
    def __init__(self) -> None:
        self._cue_store = _FakeCueStore()
        self._settings = SimpleNamespace(agent=SimpleNamespace())


class CueArmTests(unittest.TestCase):
    def test_arms_a_low_key_reversal_cue(self) -> None:
        host = _ArmHost()
        ok = host.queue_fact_reversal_cue(
            wrong="Mercury is the largest planet",
            corrected="Jupiter is the largest planet",
            memory_id=5,
        )
        self.assertTrue(ok)
        self.assertEqual(len(host._cue_store.added), 1)
        row = host._cue_store.added[0]
        self.assertEqual(row["cue_type"], "fact_reversal")
        # Subject is the corrected fact so post-turn matching credits her
        # for stating the right thing.
        self.assertEqual(row["subject"], "Jupiter is the largest planet")
        self.assertIn("Jupiter is the largest planet", row["text"])
        # TTL inherited from the policy (72h).
        self.assertAlmostEqual(row["ttl_hours"], 72.0)
        # Payload carries the memory id for the ledger loop.
        self.assertEqual(row["payload"]["memory_id"], 5)
        # Never narrate the machinery.
        self.assertNotIn("database", row["text"].lower())

    def test_empty_corrected_arms_nothing(self) -> None:
        host = _ArmHost()
        ok = host.queue_fact_reversal_cue(wrong="x", corrected="", memory_id=5)
        self.assertFalse(ok)
        self.assertEqual(host._cue_store.added, [])

    def test_corrected_equal_to_wrong_arms_nothing(self) -> None:
        """No reversal to own when the "fix" restates the claim verbatim."""
        host = _ArmHost()
        ok = host.queue_fact_reversal_cue(
            wrong="Jupiter is the largest planet",
            corrected="Jupiter is the largest planet",
            memory_id=5,
        )
        self.assertFalse(ok)
        self.assertEqual(host._cue_store.added, [])


if __name__ == "__main__":
    unittest.main()
