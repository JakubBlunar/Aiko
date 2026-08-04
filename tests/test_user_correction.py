"""Tests for F13 -- the user explicitly corrects Aiko.

Three surfaces:

* the pure detector
  (:func:`app.core.conversation.user_correction_detector.detect_user_correction`)
  -- precision of the marker gate, the overlap gate, and the
  fact-vs-opinion boundary (never fire on a ``self`` stance row);
* the off-turn worker
  (:class:`app.core.memory.user_correction_worker.UserCorrectionWorker`)
  -- the supersede semantics (new high-confidence row + demoted old row
  with a ``superseded_by`` link), the concept confidence penalty, and the
  acknowledgment-cue arming, with the LLM and stores stubbed;
* the ``render_cue`` acknowledgment line.
"""
from __future__ import annotations

import threading
import unittest
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.conversation import user_correction_detector as ucd
from app.core.memory.user_correction_worker import UserCorrectionWorker
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


@dataclass(frozen=True)
class _Mem:
    id: int
    content: str
    kind: str = "fact"
    confidence: float = 0.8


# A stored note and a message that clearly repairs it: shared content
# words {meeting, monday} clear the overlap gate, and "no, the" is a
# correction marker.
_NOTE = _Mem(id=7, content="Jacob's meeting is on Monday.", kind="fact")
_CORRECTION_MSG = "no, the meeting is on Tuesday, not Monday"


class DetectorTests(unittest.TestCase):
    def test_fires_on_an_explicit_correction(self) -> None:
        hit = ucd.detect_user_correction(_CORRECTION_MSG, [_NOTE])
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.memory_id, 7)
        self.assertTrue(hit.marker)
        self.assertGreaterEqual(hit.overlap, 2)

    def test_no_marker_no_hit(self) -> None:
        """A plain restatement with no repair marker is not a correction."""
        hit = ucd.detect_user_correction(
            "the meeting is on Tuesday and Monday", [_NOTE],
        )
        self.assertIsNone(hit)

    def test_marker_without_overlap_no_hit(self) -> None:
        """A correction marker aimed at nothing in the candidate set."""
        hit = ucd.detect_user_correction(
            "no, that's wrong",
            [_Mem(id=1, content="Jacob enjoys deep-sea documentaries.")],
        )
        self.assertIsNone(hit)

    def test_opinion_boundary_never_targets_a_self_row(self) -> None:
        """Correction of fact, not disagreement with Aiko's own stance.

        A ``self`` row is Aiko's persona/opinion and must never be a
        correction target even when the message looks like a repair and
        overlaps it -- that is K29's territory, not a memory rewrite.
        """
        self_row = _Mem(
            id=3,
            content="you are warm and you value honesty",
            kind="self",
            confidence=0.85,
        )
        hit = ucd.detect_user_correction(
            "no, you're not warm about honesty, you value honesty coldly",
            [self_row],
        )
        self.assertIsNone(hit)

    def test_disagreement_of_taste_is_not_a_marker(self) -> None:
        """"I don't think that's right" is opinion pushback, not a repair."""
        hit = ucd.detect_user_correction(
            "I don't think the meeting on Monday is a great idea", [_NOTE],
        )
        self.assertIsNone(hit)


class RenderCueTests(unittest.TestCase):
    def test_subject_is_the_corrected_fact(self) -> None:
        cue = ucd.render_cue(
            wrong="the meeting is on Monday",
            corrected="the meeting is on Tuesday",
            user_display_name="Jacob",
        )
        self.assertIn("Tuesday", cue)
        self.assertIn("Jacob", cue)

    def test_empty_correction_yields_no_cue(self) -> None:
        self.assertEqual(ucd.render_cue(wrong="x", corrected=""), "")


# ── worker fakes ──────────────────────────────────────────────────────


@dataclass
class _Row:
    id: int
    content: str
    kind: str = "fact"
    confidence: float = 0.8
    tier: str = "long_term"
    metadata: dict[str, Any] = field(default_factory=dict)


class _FakeStore:
    """Minimal MemoryStore surface the worker touches."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = {r.id: r for r in rows}
        self._next = max(self._rows, default=0) + 1
        self.added: list[_Row] = []
        self.updates: list[dict[str, Any]] = []

    def get(self, memory_id: int) -> _Row | None:
        return self._rows.get(int(memory_id))

    def add(
        self,
        content: str,
        kind: str,
        embedding: Any,
        *,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> _Row:
        row = _Row(
            id=self._next,
            content=content,
            kind=kind,
            confidence=float(confidence if confidence is not None else 0.7),
            tier="long_term",
            metadata=dict(metadata or {}),
        )
        self._next += 1
        self._rows[row.id] = row
        self.added.append(row)
        return row

    def update(
        self,
        memory_id: int,
        *,
        confidence: float | None = None,
        tier: str | None = None,
        metadata: dict[str, Any] | None = None,
        metadata_merge: bool = False,
        **_kw: Any,
    ) -> None:
        row = self._rows[int(memory_id)]
        if confidence is not None:
            row.confidence = float(confidence)
        if tier is not None:
            row.tier = tier
        if metadata is not None:
            if metadata_merge:
                row.metadata.update(metadata)
            else:
                row.metadata = dict(metadata)
        self.updates.append({"id": int(memory_id), "metadata": dict(row.metadata)})


class _FakeEmbedder:
    def embed(self, text: str) -> np.ndarray:
        return np.zeros(8, dtype=np.float32)


class _FakeOllama:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    def chat_stream(self, messages: Any, **_kwargs: Any):  # noqa: ANN401
        self.calls += 1
        yield self._response


class _FakeRateLimiter:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow

    def allow(self, _now: Any) -> bool:
        return self._allow


class _FakeConceptStore:
    def __init__(self, concept: Any | None) -> None:
        self._concept = concept
        self.updated: list[Any] = []

    def affected_concepts_for_memory(self, _memory_id: int) -> set[int]:
        return {1} if self._concept is not None else set()

    def get(self, _cid: int) -> Any:
        return self._concept

    def update(self, concept: Any) -> None:
        self.updated.append(concept)


def _memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        user_correction_max_per_run=8,
        user_correction_concept_penalty=0.25,
        user_correction_confidence=0.9,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _WorkerFixture(unittest.TestCase):
    def _worker(
        self,
        *,
        store: _FakeStore,
        candidates: list[dict[str, Any]],
        llm_response: str,
        allow: bool = True,
        concept: Any | None = None,
    ) -> tuple[UserCorrectionWorker, dict[str, Any]]:
        cues: list[dict[str, Any]] = []
        self.concept_store = _FakeConceptStore(concept)
        drained = {"used": False}

        def _drain() -> list[dict[str, Any]]:
            if drained["used"]:
                return []
            drained["used"] = True
            return list(candidates)

        worker = UserCorrectionWorker(
            memory_store=store,
            embedder=_FakeEmbedder(),
            ollama=_FakeOllama(llm_response),
            chat_model="test-model",
            rate_limiter=_FakeRateLimiter(allow),
            cancel_event=threading.Event(),
            agent_settings=SimpleNamespace(user_correction_enabled=True),
            memory_settings=_memory_settings(),
            drain_candidates=_drain,
            pending_count=lambda: 0 if drained["used"] else len(candidates),
            queue_cue=lambda **kw: (cues.append(kw) or True),
            concept_store=self.concept_store,
        )
        self.cues = cues
        return worker, {"cues": cues}


class SupersedeTests(_WorkerFixture):
    def _candidate(self) -> dict[str, Any]:
        hit = ucd.detect_user_correction(_CORRECTION_MSG, [_NOTE])
        assert hit is not None
        return {"hit": hit, "user_text": _CORRECTION_MSG}

    def test_confirmed_correction_writes_and_demotes(self) -> None:
        store = _FakeStore([_Row(id=7, content=_NOTE.content, confidence=0.8)])
        worker, _ = self._worker(
            store=store,
            candidates=[self._candidate()],
            llm_response=(
                '{"verdict": "YES", "correction": '
                '"Jacob\'s meeting is on Tuesday."}'
            ),
        )
        stats = worker.run()

        self.assertEqual(stats["confirmed"], 1)
        # New high-confidence correction row written.
        self.assertEqual(len(store.added), 1)
        new_row = store.added[0]
        self.assertEqual(new_row.content, "Jacob's meeting is on Tuesday.")
        self.assertAlmostEqual(new_row.confidence, 0.9)
        self.assertEqual(new_row.metadata.get("corrects_memory_id"), 7)
        # Old row demoted + linked.
        old = store.get(7)
        assert old is not None
        self.assertEqual(old.tier, "archive")
        self.assertLessEqual(old.confidence, 0.2)
        self.assertEqual(old.metadata.get("superseded_by"), new_row.id)
        self.assertEqual(
            old.metadata.get("superseded_reason"), "user_correction",
        )

    def test_confirmed_correction_arms_the_cue(self) -> None:
        store = _FakeStore([_Row(id=7, content=_NOTE.content)])
        worker, ctx = self._worker(
            store=store,
            candidates=[self._candidate()],
            llm_response='{"verdict": "YES", "correction": "meeting is Tuesday"}',
        )
        worker.run()
        self.assertEqual(len(ctx["cues"]), 1)
        self.assertEqual(ctx["cues"][0]["corrected"], "meeting is Tuesday")
        self.assertEqual(ctx["cues"][0]["wrong"], _NOTE.content)

    def test_no_verdict_leaves_memory_untouched(self) -> None:
        store = _FakeStore([_Row(id=7, content=_NOTE.content, confidence=0.8)])
        worker, ctx = self._worker(
            store=store,
            candidates=[self._candidate()],
            llm_response='{"verdict": "NO", "correction": ""}',
        )
        stats = worker.run()
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(len(store.added), 0)
        self.assertEqual(store.get(7).tier, "long_term")
        self.assertEqual(len(ctx["cues"]), 0)

    def test_rate_limited_run_makes_no_llm_call(self) -> None:
        store = _FakeStore([_Row(id=7, content=_NOTE.content)])
        worker, _ = self._worker(
            store=store,
            candidates=[self._candidate()],
            llm_response='{"verdict": "YES", "correction": "x"}',
            allow=False,
        )
        stats = worker.run()
        self.assertEqual(stats["skipped_rate_limit"], 1)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(len(store.added), 0)

    def test_already_superseded_row_is_skipped(self) -> None:
        store = _FakeStore([
            _Row(id=7, content=_NOTE.content, tier="archive"),
        ])
        worker, _ = self._worker(
            store=store,
            candidates=[self._candidate()],
            llm_response='{"verdict": "YES", "correction": "x"}',
        )
        stats = worker.run()
        self.assertEqual(stats["skipped_stale"], 1)
        self.assertEqual(len(store.added), 0)


class ConceptPropagationTests(_WorkerFixture):
    def _candidate(self) -> dict[str, Any]:
        hit = ucd.detect_user_correction(_CORRECTION_MSG, [_NOTE])
        assert hit is not None
        return {"hit": hit, "user_text": _CORRECTION_MSG}

    def test_demotion_knocks_down_a_backed_concept(self) -> None:
        concept = SimpleNamespace(
            concept_id=1, confidence=0.8, plasticity=0.5, status="active",
        )
        store = _FakeStore([_Row(id=7, content=_NOTE.content)])
        worker, _ = self._worker(
            store=store,
            candidates=[self._candidate()],
            llm_response='{"verdict": "YES", "correction": "meeting is Tuesday"}',
            concept=concept,
        )
        stats = worker.run()
        self.assertEqual(stats["concepts_touched"], 1)
        self.assertLess(concept.confidence, 0.8)
        self.assertEqual(len(self.concept_store.updated), 1)

    def test_retired_concept_is_left_alone(self) -> None:
        concept = SimpleNamespace(
            concept_id=1, confidence=0.8, plasticity=0.5, status="contradicted",
        )
        store = _FakeStore([_Row(id=7, content=_NOTE.content)])
        worker, _ = self._worker(
            store=store,
            candidates=[self._candidate()],
            llm_response='{"verdict": "YES", "correction": "meeting is Tuesday"}',
            concept=concept,
        )
        stats = worker.run()
        self.assertEqual(stats["concepts_touched"], 0)
        self.assertAlmostEqual(concept.confidence, 0.8)


class _CaptureStore:
    """iter_by_kind surface for the post-turn candidate scan."""

    def __init__(self, rows: list[_Mem]) -> None:
        self._rows = rows

    def iter_by_kind(self, kind: str) -> list[_Mem]:
        return [m for m in self._rows if m.kind == kind]


class _CaptureHost(PostTurnHelpersMixin):
    def __init__(self, rows: list[_Mem], *, enabled: bool = True) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(user_correction_enabled=enabled),
        )
        self._memory_settings = SimpleNamespace(
            user_correction_min_confidence=0.4,
            user_correction_min_overlap=2,
            user_correction_max_candidates=50,
        )
        self._memory_store = _CaptureStore(rows)
        self._rag_retriever = None
        self._pending_correction_candidates: deque[Any] = deque(maxlen=16)


class PostTurnCaptureTests(unittest.TestCase):
    def test_a_correction_is_stashed_for_the_worker(self) -> None:
        host = _CaptureHost([_NOTE])
        host._maybe_capture_user_correction(_CORRECTION_MSG)
        drained = host.drain_correction_candidates()
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0]["hit"].memory_id, 7)
        # Draining empties the queue.
        self.assertEqual(host.drain_correction_candidates(), [])

    def test_master_switch_off_captures_nothing(self) -> None:
        host = _CaptureHost([_NOTE], enabled=False)
        host._maybe_capture_user_correction(_CORRECTION_MSG)
        self.assertEqual(host.drain_correction_candidates(), [])

    def test_a_plain_message_is_not_stashed(self) -> None:
        host = _CaptureHost([_NOTE])
        host._maybe_capture_user_correction("the meeting is Monday and Tuesday")
        self.assertEqual(host.drain_correction_candidates(), [])


if __name__ == "__main__":
    unittest.main()
