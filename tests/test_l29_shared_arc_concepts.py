"""Tests for L29(a) episodic shared arcs (subject=relationship narratives).

Covers the full vertical slice:

* the pure ``shared_arc_grouping`` helper (seed-and-sweep segmentation, the
  time-gap split, the coherence split, the ``min_chain`` floor, the
  ``quiet_days`` rejection of a live thread, interleaved concurrent threads,
  and the topic/vibe split that motivated the whole phase),
* the ``_run_shared_arc_pass`` worker pass (creation with ordered ``sequence``
  evidence under ``subject="relationship"``, open-arc rejection, the
  watermark no-op, the ``shared_arc_synthesis_enabled`` switch, and the fact
  that it does not disturb the L8 user/aiko passes),
* the ``narrative_relationship`` proposer (the pair voice, chain order taken
  from the candidate rather than the LLM, reinforce-by-id).

The rendering side needs no coverage here: the ``relationship`` branch of
``_concept_narrative_header`` predates this phase and is already pinned by
``tests/test_l8_narrative_concepts.py``.
"""
from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.concepts import shared_arc_grouping as sag
from app.core.concepts.ritual_grouping import MomentInput
from app.core.concepts.proposers import ExistingConcept, ProposerContext
from app.core.concepts.proposers.narrative_relationship import (
    SPEC,
    propose_narrative_relationship,
)

from tests.test_concept_synthesis_worker import (
    MemStub,
    WorkerHarness,
)


_UTC = timezone.utc
_BASE = datetime(2026, 1, 5, 20, 0, tzinfo=_UTC)
# Comfortably past every episode below, so the quiet-period gate is satisfied
# unless a test deliberately moves it.
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)


def _vec(*xs) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


def _moment(
    mid: int,
    *,
    day: float,
    vector,
    vibe: str = "warm",
    text: str | None = None,
) -> MomentInput:
    return MomentInput(
        id=mid,
        embedding=vector,
        text=text or f"moment {mid}",
        vibe=vibe,
        when=(_BASE + timedelta(days=day)).isoformat(),
        salience=0.5,
    )


# ── shared_arc_grouping (pure) ──────────────────────────────────────────


class SharedArcGroupingTests(unittest.TestCase):
    def test_groups_a_coherent_contiguous_run(self) -> None:
        moments = [
            _moment(10 + i, day=i * 2, vector=_vec(1.0, 0.0, 0.0))
            for i in range(4)
        ]
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(len(episodes), 1)
        episode = episodes[0]
        self.assertEqual(episode.member_ids, (10, 11, 12, 13))
        self.assertEqual(episode.size, 4)
        # Members come back oldest-first: the chain order the proposer cites.
        self.assertEqual(
            [m.id for m in episode.members], [10, 11, 12, 13]
        )
        self.assertAlmostEqual(episode.span_days, 6.0, places=3)

    def test_time_gap_splits_one_topic_into_two_episodes(self) -> None:
        # Same topic, two separate pushes 40 days apart. One arc would be a
        # lie about what happened; two is the honest reading.
        first = [
            _moment(10 + i, day=i, vector=_vec(1.0, 0.0, 0.0))
            for i in range(3)
        ]
        second = [
            _moment(20 + i, day=40 + i, vector=_vec(1.0, 0.0, 0.0))
            for i in range(3)
        ]
        episodes = sag.group_episodes(
            first + second, min_chain=3, gap_days=10.0, now=_NOW
        )
        self.assertEqual(len(episodes), 2)
        self.assertEqual(
            {e.member_ids for e in episodes},
            {(10, 11, 12), (20, 21, 22)},
        )

    def test_incoherent_moment_is_skipped_not_fatal(self) -> None:
        # An unrelated moment in the middle of a run must not end the
        # episode -- with several moments a day across unrelated topics,
        # interleaving is the norm, and closing on the first mismatch would
        # never build a chain longer than two.
        moments = [
            _moment(10, day=0, vector=_vec(1.0, 0.0, 0.0)),
            _moment(11, day=1, vector=_vec(1.0, 0.0, 0.0)),
            _moment(99, day=2, vector=_vec(0.0, 1.0, 0.0)),  # off-topic
            _moment(12, day=3, vector=_vec(1.0, 0.0, 0.0)),
        ]
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].member_ids, (10, 11, 12))

    def test_two_interleaved_threads_each_get_an_episode(self) -> None:
        # Concurrent threads are why the sweep re-seeds instead of making one
        # greedy pass: the second topic must not be swallowed or dropped.
        moments = []
        for i in range(3):
            moments.append(
                _moment(10 + i, day=i * 2, vector=_vec(1.0, 0.0, 0.0))
            )
            moments.append(
                _moment(20 + i, day=i * 2 + 1, vector=_vec(0.0, 1.0, 0.0))
            )
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(
            {e.member_ids for e in episodes},
            {(10, 11, 12), (20, 21, 22)},
        )

    def test_min_chain_floor(self) -> None:
        moments = [
            _moment(10 + i, day=i, vector=_vec(1.0, 0.0, 0.0))
            for i in range(2)
        ]
        self.assertEqual(
            sag.group_episodes(moments, min_chain=3, now=_NOW), []
        )

    def test_short_run_releases_members_to_a_later_seed(self) -> None:
        # The A-thread has only two members, so it fails the floor -- but its
        # follower must be released, or a later seed could never reuse it.
        # Here the B-thread is long enough and must still be found.
        moments = [
            _moment(10, day=0, vector=_vec(1.0, 0.0, 0.0)),
            _moment(11, day=1, vector=_vec(1.0, 0.0, 0.0)),
            _moment(20, day=2, vector=_vec(0.0, 1.0, 0.0)),
            _moment(21, day=3, vector=_vec(0.0, 1.0, 0.0)),
            _moment(22, day=4, vector=_vec(0.0, 1.0, 0.0)),
        ]
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].member_ids, (20, 21, 22))

    def test_live_thread_is_held_back_by_quiet_days(self) -> None:
        # A project whose last beat was yesterday is not a closed arc, and
        # the proposer's ``closed`` gate should never be asked about it.
        recent = _NOW - timedelta(days=1)
        moments = [
            MomentInput(
                id=10 + i,
                embedding=_vec(1.0, 0.0, 0.0),
                text=f"moment {i}",
                vibe="warm",
                when=(recent - timedelta(days=2 - i)).isoformat(),
            )
            for i in range(3)
        ]
        self.assertEqual(
            sag.group_episodes(
                moments, min_chain=3, quiet_days=3.0, now=_NOW
            ),
            [],
        )
        # Same corpus, once it has been quiet long enough.
        later = _NOW + timedelta(days=5)
        self.assertEqual(
            len(
                sag.group_episodes(
                    moments, min_chain=3, quiet_days=3.0, now=later
                )
            ),
            1,
        )

    def test_same_vibe_different_topics_do_not_group(self) -> None:
        # The finding that motivated the phase: moments used to be embedded
        # with their ``"Shared moment (<vibe>): "`` prefix, so a shared vibe
        # dragged unrelated moments together. Vibe is a field; topics come
        # from the vector, and the grouping must honour that split.
        moments = [
            _moment(10, day=0, vector=_vec(1.0, 0.0, 0.0), vibe="tender"),
            _moment(11, day=1, vector=_vec(0.0, 1.0, 0.0), vibe="tender"),
            _moment(12, day=2, vector=_vec(0.0, 0.0, 1.0), vibe="tender"),
        ]
        self.assertEqual(
            sag.group_episodes(moments, min_chain=3, now=_NOW), []
        )

    def test_shared_common_direction_does_not_chain_everything(self) -> None:
        # The second finding of the phase, and the reason vectors are
        # mean-centered. Every shared moment is "the two of them being
        # affectionate", so the raw embeddings share a dominant direction: on
        # the real corpus the mean pairwise cosine was 0.608 and *74% of all
        # pairs* cleared the old 0.55 floor, collapsing 145 moments into one
        # 83-member "episode". Here two genuinely different topics sit behind
        # a common component 4x their own magnitude -- raw cosine between them
        # is ~0.94, far above any workable floor, so without centering this is
        # one run of six.
        common = 4.0
        topic_a = _vec(common, 1.0, 0.0)
        topic_b = _vec(common, 0.0, 1.0)
        moments = [
            _moment(10 + i, day=i, vector=topic_a if i < 3 else topic_b)
            for i in range(6)
        ]
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(
            [e.member_ids for e in episodes],
            [(10, 11, 12), (13, 14, 15)],
        )

    def test_centering_skipped_for_a_single_topic_corpus(self) -> None:
        # A corpus with no topical variance has no mean worth removing:
        # subtracting it would leave pure noise and find nothing, when the
        # honest reading is that these moments really are all one thread.
        moments = [
            _moment(10 + i, day=i, vector=_vec(1.0, 0.0, 0.0))
            for i in range(4)
        ]
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].size, 4)

    def test_dominant_vibe_comes_from_the_field(self) -> None:
        moments = [
            _moment(10, day=0, vector=_vec(1.0, 0.0), vibe="tender"),
            _moment(11, day=1, vector=_vec(1.0, 0.0), vibe="tender"),
            _moment(12, day=2, vector=_vec(1.0, 0.0), vibe="general"),
        ]
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].dominant_vibe, "tender")

    def test_unparseable_when_is_dropped(self) -> None:
        moments = [
            _moment(10 + i, day=i, vector=_vec(1.0, 0.0, 0.0))
            for i in range(3)
        ]
        moments.append(
            MomentInput(
                id=99, embedding=_vec(1.0, 0.0, 0.0), text="undated",
                vibe="warm", when="not-a-timestamp",
            )
        )
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual(len(episodes), 1)
        self.assertNotIn(99, episodes[0].member_ids)

    def test_episodes_sorted_largest_first(self) -> None:
        moments = [
            _moment(10 + i, day=i, vector=_vec(1.0, 0.0, 0.0))
            for i in range(3)
        ]
        moments += [
            _moment(20 + i, day=i, vector=_vec(0.0, 1.0, 0.0))
            for i in range(5)
        ]
        episodes = sag.group_episodes(moments, min_chain=3, now=_NOW)
        self.assertEqual([e.size for e in episodes], [5, 3])


# ── worker shared-arc pass ──────────────────────────────────────────────


_ARC_MARKER = "SHARED STORY-ARCS in what"


def _arc_rows(n: int = 4, *, start_id: int = 700, day_step: float = 2.0):
    return [
        MemStub(
            start_id + i,
            f"Shared moment (proud): rebuilding the memory system, step {i}",
            "shared_moment",
            0.5 + i * 0.05,
            metadata={
                "vibe": "proud",
                "when": (_BASE + timedelta(days=i * day_step)).isoformat(),
                "what": f"rebuilding the memory system, step {i}",
            },
            embedding=_vec(1.0, 0.0, 0.0),
        )
        for i in range(n)
    ]


def _arc_responder(system, user):
    if _ARC_MARKER in system:
        return {"concepts": [{
            "label": "The month they rebuilt the memory system",
            "arc_index": 0,
            # Scrambled on purpose: ordinals must follow the candidate's
            # temporal order, not the order the model happened to list.
            "evidence_memory_ids": [702, 700, 703, 701],
            "closed": True,
            "rationale": "started rough, landed clean",
            "confidence": 0.75,
        }]}
    return {"concepts": []}


class SharedArcPassTests(unittest.TestCase):
    def test_creates_ordered_relationship_narrative(self) -> None:
        rows = _arc_rows()
        h = WorkerHarness(
            _arc_responder, clusters=[], self_memories=[], shared_moments=rows,
        )
        stats = h.worker.run()
        self.assertTrue(stats["shared_arc_dirty"])
        out = h.store.list_by(subject="relationship", kind="narrative")
        self.assertEqual(len(out), 1)
        concept = out[0]
        self.assertEqual(
            concept.label, "The month they rebuilt the memory system"
        )
        self.assertEqual(concept.evidence_model, "sequence")
        evidence = h.store.evidence_of(concept.concept_id)
        self.assertTrue(all(e.src_type == "memory" for e in evidence))
        self.assertEqual(
            [e.src_id for e in evidence], [str(m.id) for m in rows]
        )
        self.assertEqual(
            [e.ordinal for e in evidence], list(range(len(rows)))
        )

    def test_open_arc_rejected(self) -> None:
        def responder(system, user):
            if _ARC_MARKER in system:
                return {"concepts": [{
                    "label": "Something still going on",
                    "arc_index": 0,
                    "evidence_memory_ids": [700, 701, 702, 703],
                    "closed": False,
                    "confidence": 0.9,
                }]}
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[], self_memories=[],
            shared_moments=_arc_rows(),
        )
        h.worker.run()
        self.assertEqual(
            h.store.list_by(subject="relationship", kind="narrative"), []
        )

    def test_short_corpus_never_reaches_the_proposer(self) -> None:
        called = {"arc": 0}

        def responder(system, user):
            if _ARC_MARKER in system:
                called["arc"] += 1
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[], self_memories=[],
            shared_moments=_arc_rows(n=2),
        )
        stats = h.worker.run()
        self.assertEqual(called["arc"], 0)
        self.assertFalse(stats["shared_arc_dirty"])

    def test_incoherent_corpus_advances_the_watermark(self) -> None:
        # Nothing groups, but the watermark must still move or an unchanged,
        # unsegmentable corpus would re-run the grouping every idle tick.
        rows = _arc_rows(n=4)
        for i, row in enumerate(rows):
            row.embedding = _vec(*[1.0 if j == i else 0.0 for j in range(4)])
        called = {"arc": 0}

        def responder(system, user):
            if _ARC_MARKER in system:
                called["arc"] += 1
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[], self_memories=[], shared_moments=rows,
        )
        self.assertTrue(h.worker.run()["shared_arc_dirty"])
        self.assertEqual(called["arc"], 0)
        self.assertFalse(h.worker.run()["shared_arc_dirty"])

    def test_clean_rerun_is_noop(self) -> None:
        h = WorkerHarness(
            _arc_responder, clusters=[], self_memories=[],
            shared_moments=_arc_rows(),
        )
        h.worker.run()
        calls = h.ollama.calls
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["shared_arc_dirty"])
        self.assertEqual(h.ollama.calls, calls)
        self.assertEqual(h.store.count(), before)

    def test_disabled_switch_skips_pass(self) -> None:
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            shared_arc_synthesis_enabled=False,
        )
        h = WorkerHarness(
            _arc_responder, clusters=[], self_memories=[],
            shared_moments=_arc_rows(), agent=agent,
        )
        stats = h.worker.run()
        self.assertFalse(stats["shared_arc_dirty"])
        self.assertEqual(
            h.store.list_by(subject="relationship", kind="narrative"), []
        )

    def test_live_episode_is_not_proposed(self) -> None:
        # Same corpus, dated up to now: the quiet-period gate holds it back.
        now = datetime.now(timezone.utc)
        rows = _arc_rows()
        for i, row in enumerate(rows):
            row.metadata["when"] = (
                now - timedelta(days=len(rows) - 1 - i)
            ).isoformat()
        called = {"arc": 0}

        def responder(system, user):
            if _ARC_MARKER in system:
                called["arc"] += 1
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[], self_memories=[], shared_moments=rows,
        )
        h.worker.run()
        self.assertEqual(called["arc"], 0)

    def test_does_not_leak_into_the_l8_subjects(self) -> None:
        h = WorkerHarness(
            _arc_responder, clusters=[], self_memories=[],
            shared_moments=_arc_rows(),
        )
        h.worker.run()
        self.assertEqual(h.store.list_by(subject="user", kind="narrative"), [])
        self.assertEqual(h.store.list_by(subject="aiko", kind="narrative"), [])


# ── proposer (direct) ───────────────────────────────────────────────────


def _ctx(responder):
    calls: dict[str, str] = {}

    def call_llm(system, user):
        calls["system"] = system
        calls["user"] = user
        return responder(system, user)["concepts"]

    return ProposerContext(
        call_llm=call_llm, user_name="Jacob", assistant_name="Aiko"
    ), calls


def _candidate(ids, *, label="rebuilding the memory system"):
    from app.core.concepts.proposers import NarrativeCandidate

    mems = [
        MemStub(
            mid,
            f"Shared moment (proud): step {pos}",
            "shared_moment",
            0.5,
            metadata={"what": f"step {pos}", "vibe": "proud"},
            event_time=(_BASE + timedelta(days=pos)).isoformat(),
        )
        for pos, mid in enumerate(ids)
    ]
    return NarrativeCandidate(
        rep=ids[0], label=label, subject="relationship", memories=mems
    )


class SharedArcProposerTests(unittest.TestCase):
    def test_spec_shape(self) -> None:
        self.assertEqual(SPEC.kind, "narrative")
        self.assertEqual(SPEC.subject, "relationship")
        self.assertEqual(SPEC.evidence_model, "sequence")
        self.assertEqual(SPEC.population, "shared_arc")
        self.assertEqual(
            SPEC.sig_key, "concept_synth.narrative_sig.relationship"
        )

    def test_pair_voice_in_the_prompt(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_narrative_relationship(
            ctx, candidates=[_candidate([1, 2, 3])]
        )
        # Neither of the two L8 voices: this arc is about both of them.
        self.assertIn("Jacob and Aiko together", calls["user"])
        self.assertIn("third person plural", calls["user"])
        self.assertNotIn("about Jacob (third person)", calls["user"])
        self.assertNotIn("Aiko herself", calls["user"])

    def test_chain_order_comes_from_the_candidate(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "The rebuild",
                "arc_index": 0,
                "evidence_memory_ids": [3, 1, 2],
                "closed": True,
                "confidence": 0.7,
            }]}

        ctx, _calls = _ctx(responder)
        out = propose_narrative_relationship(
            ctx, candidates=[_candidate([1, 2, 3])]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].subject, "relationship")
        self.assertEqual(out[0].kind, "narrative")
        self.assertEqual(out[0].evidence_model, "sequence")
        self.assertEqual(
            out[0].evidence,
            [("memory", "1"), ("memory", "2"), ("memory", "3")],
        )

    def test_reinforce_by_id(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "arc_index": 0,
                "evidence_memory_ids": [1, 2, 3],
                "rationale": "fresh beats",
            }]}

        ctx, _calls = _ctx(responder)
        out = propose_narrative_relationship(
            ctx,
            candidates=[_candidate([1, 2, 3])],
            existing=[ExistingConcept(id=42, label="The rebuild")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")

    def test_short_chain_rejected(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Two beats is an anecdote",
                "arc_index": 0,
                "evidence_memory_ids": [1, 2],
                "closed": True,
                "confidence": 0.9,
            }]}

        ctx, _calls = _ctx(responder)
        self.assertEqual(
            propose_narrative_relationship(
                ctx, candidates=[_candidate([1, 2, 3])], min_chain=3
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
