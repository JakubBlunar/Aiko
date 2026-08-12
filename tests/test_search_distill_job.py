"""Tests for the D3 post-turn search-distill job.

The brain-lane ``web_search`` tool stashes its hits on the session during
the turn (``_stash_turn_search_results``); the post-turn path drains them
and submits one speaking-window job that distils them into a knowledge
memory (``_maybe_schedule_search_distill_job``).

Contracts pinned here:

1. **No search, no job.** The overwhelming majority of turns never touch
   the tool, and they must not pay anything for this path.
2. **The stash is drained, not accumulated.** A second post-turn call
   must not re-distil the same hits, and hits must not leak into the next
   turn's job.
3. **The job runs off the turn** and honours the scheduler's cooperative
   cancel flag.
"""
from __future__ import annotations

import unittest
from typing import Any

from app.core.session.search_provider_mixin import SearchProviderMixin
from app.core.session.speaking_window_jobs_mixin import SpeakingWindowJobsMixin


class _StopFlag:
    def __init__(self, stopped: bool = False) -> None:
        self._stopped = stopped

    def is_set(self) -> bool:
        return self._stopped


class _StubScheduler:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def submit(self, job: Any) -> None:
        self.jobs.append(job)


class _StubKnowledgeWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []
        self.raises = False

    def distil_and_store(
        self, query: str, snippets: list[dict[str, str]],
    ) -> int:
        self.calls.append((query, snippets))
        if self.raises:
            raise RuntimeError("distil down")
        return 1


class _Session(SearchProviderMixin, SpeakingWindowJobsMixin):
    """Minimal stand-in carrying only what these two methods touch."""

    def __init__(self, *, worker: Any = None) -> None:
        self._scheduler = _StubScheduler()
        self._idle_knowledge = worker


_HITS = [{"title": "t", "url": "https://e.com", "snippet": "12 episodes"}]


class StashTests(unittest.TestCase):
    def test_drain_returns_none_when_she_never_searched(self) -> None:
        session = _Session()
        self.assertIsNone(session._drain_turn_search_results())

    def test_stash_then_drain_round_trips(self) -> None:
        session = _Session()
        session._stash_turn_search_results("dandadan season 2", _HITS)
        stashed = session._drain_turn_search_results()
        assert stashed is not None
        query, hits = stashed
        self.assertEqual(query, "dandadan season 2")
        self.assertEqual(hits, _HITS)

    def test_drain_clears_the_stash(self) -> None:
        session = _Session()
        session._stash_turn_search_results("q", _HITS)
        session._drain_turn_search_results()
        self.assertIsNone(session._drain_turn_search_results())

    def test_second_search_in_one_turn_wins(self) -> None:
        # The later query is the one she actually answered from.
        session = _Session()
        session._stash_turn_search_results("first", _HITS)
        session._stash_turn_search_results("second", _HITS)
        stashed = session._drain_turn_search_results()
        assert stashed is not None
        self.assertEqual(stashed[0], "second")

    def test_empty_results_are_not_stashed(self) -> None:
        session = _Session()
        session._stash_turn_search_results("q", [])
        session._stash_turn_search_results("", _HITS)
        self.assertIsNone(session._drain_turn_search_results())

    def test_stash_copies_the_rows(self) -> None:
        # The tool hands over its own dicts; a later mutation upstream
        # must not rewrite what the post-turn job distils.
        session = _Session()
        rows = [dict(_HITS[0])]
        session._stash_turn_search_results("q", rows)
        rows[0]["snippet"] = "mutated"
        stashed = session._drain_turn_search_results()
        assert stashed is not None
        self.assertEqual(stashed[1][0]["snippet"], "12 episodes")


class SchedulingTests(unittest.TestCase):
    def test_no_search_submits_nothing(self) -> None:
        session = _Session(worker=_StubKnowledgeWorker())
        session._maybe_schedule_search_distill_job()
        self.assertEqual(session._scheduler.jobs, [])

    def test_search_submits_one_deduped_job(self) -> None:
        session = _Session(worker=_StubKnowledgeWorker())
        session._stash_turn_search_results("dandadan season 2", _HITS)
        session._maybe_schedule_search_distill_job()
        self.assertEqual(len(session._scheduler.jobs), 1)
        job = session._scheduler.jobs[0]
        self.assertEqual(job.name, "search_knowledge_distill")
        self.assertEqual(job.dedupe_key, "search_knowledge_distill")

    def test_job_distils_the_stashed_hits(self) -> None:
        worker = _StubKnowledgeWorker()
        session = _Session(worker=worker)
        session._stash_turn_search_results("dandadan season 2", _HITS)
        session._maybe_schedule_search_distill_job()
        session._scheduler.jobs[0].callable(_StopFlag())
        self.assertEqual(worker.calls, [("dandadan season 2", _HITS)])

    def test_cancelled_job_does_not_call_the_llm(self) -> None:
        worker = _StubKnowledgeWorker()
        session = _Session(worker=worker)
        session._stash_turn_search_results("q", _HITS)
        session._maybe_schedule_search_distill_job()
        session._scheduler.jobs[0].callable(_StopFlag(stopped=True))
        self.assertEqual(worker.calls, [])

    def test_a_raising_worker_does_not_escape_the_job(self) -> None:
        worker = _StubKnowledgeWorker()
        worker.raises = True
        session = _Session(worker=worker)
        session._stash_turn_search_results("q", _HITS)
        session._maybe_schedule_search_distill_job()
        session._scheduler.jobs[0].callable(_StopFlag())  # must not raise

    def test_no_knowledge_worker_still_drains_the_stash(self) -> None:
        # Knowledge enrichment can be disabled while the search tool is
        # on; the hits must not sit around waiting for a worker that
        # never arrives and get distilled on some unrelated later turn.
        session = _Session(worker=None)
        session._stash_turn_search_results("q", _HITS)
        session._maybe_schedule_search_distill_job()
        self.assertEqual(session._scheduler.jobs, [])
        self.assertIsNone(session._drain_turn_search_results())

    def test_second_post_turn_call_does_not_resubmit(self) -> None:
        session = _Session(worker=_StubKnowledgeWorker())
        session._stash_turn_search_results("q", _HITS)
        session._maybe_schedule_search_distill_job()
        session._maybe_schedule_search_distill_job()
        self.assertEqual(len(session._scheduler.jobs), 1)


if __name__ == "__main__":
    unittest.main()
