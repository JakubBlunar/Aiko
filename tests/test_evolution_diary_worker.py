"""L17f: the evolution-diary worker -- composition, gates, grounding."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.concepts.concept_learning_event_store import (
    ConceptLearningEventStore,
    LearningEvent,
)
from app.core.concepts.evolution_diary_store import EvolutionDiaryStore
from app.core.concepts.evolution_diary_worker import (
    KV_LAST_FIRED_AT,
    EvolutionDiaryWorker,
    render_learning_brief,
)
from app.core.infra.chat_database import ChatDatabase


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

# Long enough to clear the worker's minimum body length, so a test that
# does not care about the prose still exercises the persist path.
ENTRY = "I stopped hedging about what you actually want from me."


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


@dataclass
class Settings:
    evolution_diary_interval_seconds: int = 86400
    evolution_diary_min_events: int = 3
    evolution_diary_min_salience: float = 0.45
    evolution_diary_cooldown_days: float = 7.0
    evolution_diary_backlog_pages: int = 3


@dataclass
class Agent:
    concepts_enabled: bool = True
    evolution_diary_enabled: bool = True


class FakeOllama:
    """Records the compose prompt so grounding can be asserted."""

    def __init__(self, entry: str = "", fail: bool = False) -> None:
        self.entry = entry
        self.fail = fail
        self.calls: list[list[dict]] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("model down")
        return json.dumps({"entry": self.entry}), {}


@dataclass
class Harness:
    db: ChatDatabase
    learning: ConceptLearningEventStore
    diary: EvolutionDiaryStore
    settings: Settings
    agent: Agent
    kv: dict = field(default_factory=dict)


def _harness() -> Harness:
    db = ChatDatabase(Path(tempfile.mkdtemp()) / "test.db")
    return Harness(
        db=db,
        learning=ConceptLearningEventStore(db),
        diary=EvolutionDiaryStore(db),
        settings=Settings(),
        agent=Agent(),
    )


def _worker(h: Harness, *, ollama=None, model="m") -> EvolutionDiaryWorker:
    return EvolutionDiaryWorker(
        learning_store=h.learning,
        diary_store=h.diary,
        memory_settings=h.settings,
        agent_settings=h.agent,
        ollama=ollama if ollama is not None else FakeOllama(ENTRY),
        chat_model=model,
        kv_get=h.kv.get,
        kv_set=lambda k, v: h.kv.__setitem__(k, v),
        user_name_provider=lambda: "Jacob",
        clock=lambda: NOW,
    )


def _event(h: Harness, n: int, **kw) -> int:
    base = dict(
        shape="emergence",
        concept_id=100 + n,
        kind="identity",
        subject="user",
        new_label=f"belief number {n}",
        because=f"enough moments pointed at belief number {n}",
        resolution=f"now held as belief number {n}",
        salience=0.6,
        fingerprint=f"fp-{n}",
        created_at=_iso(10 - n),
    )
    base.update(kw)
    return h.learning.add(LearningEvent(**base))  # type: ignore[arg-type]


def _three(h: Harness) -> list[int]:
    return [_event(h, n) for n in range(3)]


class BriefTests(unittest.TestCase):
    def test_the_brief_carries_prose_and_nothing_numeric(self) -> None:
        brief = render_learning_brief(
            [
                LearningEvent(
                    shape="succession",
                    subject="aiko",
                    because="what looked like A turned out to be B",
                    resolution="now held as B",
                    salience=0.87,
                    cosine=0.61,
                    decisive_event_id=4242,
                    concept_id=99,
                )
            ]
        )
        self.assertIn("what looked like A turned out to be B", brief)
        self.assertIn("about myself", brief)
        # No machinery may reach the model: numbers invite editorialising.
        self.assertNotIn("0.87", brief)
        self.assertNotIn("4242", brief)
        self.assertNotIn("99", brief)

    def test_user_subject_reads_as_about_them(self) -> None:
        brief = render_learning_brief(
            [LearningEvent(subject="user", because="they prefer depth")]
        )
        self.assertIn("about them", brief)

    def test_an_event_with_no_prose_falls_back_to_the_label(self) -> None:
        brief = render_learning_brief(
            [LearningEvent(because="", new_label="a bare belief")]
        )
        self.assertIn("a bare belief", brief)

    def test_a_wholly_empty_event_is_dropped(self) -> None:
        self.assertEqual(render_learning_brief([LearningEvent()]), "")

    def test_a_resolution_that_restates_the_label_is_dropped(self) -> None:
        # "now held as X" after a because clause that already named X
        # would say the same belief three times in one line.
        brief = render_learning_brief(
            [
                LearningEvent(
                    shape="emergence",
                    new_label="they prefer depth",
                    because="enough moments pointed at they prefer depth",
                    resolution="now held as they prefer depth",
                )
            ]
        )
        self.assertNotIn("now held as", brief)
        self.assertEqual(brief.count("they prefer depth"), 1)

    def test_a_resolution_that_adds_an_outcome_is_kept(self) -> None:
        brief = render_learning_brief(
            [
                LearningEvent(
                    shape="loss",
                    new_label="a faded belief",
                    because="the support for a faded belief fell away",
                    resolution="no longer held",
                )
            ]
        )
        self.assertIn("[no longer held]", brief)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _harness()

    def test_too_few_changes_writes_nothing_and_keeps_them(self) -> None:
        _event(self.h, 0)
        _event(self.h, 1)
        stats = _worker(self.h).run()
        self.assertEqual(stats["reason"], "nothing_to_report")
        self.assertEqual(self.h.diary.count(), 0)
        # Held, not consumed: next week's changes can join these.
        self.assertEqual(self.h.diary.latest_watermark(), 0)

    def test_reaching_the_floor_lets_an_entry_through(self) -> None:
        _three(self.h)
        stats = _worker(self.h).run()
        self.assertEqual(stats["fired"], 1)
        self.assertEqual(self.h.diary.count(), 1)

    def test_low_salience_changes_do_not_count_towards_the_floor(self) -> None:
        for n in range(4):
            _event(self.h, n, salience=0.2)
        stats = _worker(self.h).run()
        self.assertEqual(stats["reason"], "nothing_to_report")

    def test_the_cooldown_blocks_a_second_entry(self) -> None:
        _three(self.h)
        worker = _worker(self.h)
        worker.run()
        for n in range(3, 6):
            _event(self.h, n)
        self.assertEqual(worker.run()["reason"], "cooldown")
        self.assertEqual(self.h.diary.count(), 1)

    def test_an_expired_cooldown_allows_the_next_entry(self) -> None:
        _three(self.h)
        self.h.kv[KV_LAST_FIRED_AT] = _iso(30)
        stats = _worker(self.h).run()
        self.assertEqual(stats["fired"], 1)

    def test_forcing_bypasses_only_the_cooldown(self) -> None:
        _event(self.h, 0)
        worker = _worker(self.h)
        worker.force_next()
        # The floor is not a pacing gate -- forcing must not invent an entry.
        self.assertEqual(worker.run()["reason"], "nothing_to_report")

    def test_disabled_worker_skips(self) -> None:
        self.h.agent.evolution_diary_enabled = False
        _three(self.h)
        self.assertEqual(_worker(self.h).run()["reason"], "disabled")

    def test_concepts_off_disables_the_diary_too(self) -> None:
        self.h.agent.concepts_enabled = False
        _three(self.h)
        self.assertEqual(_worker(self.h).run()["reason"], "disabled")

    def test_no_model_is_a_clean_skip(self) -> None:
        _three(self.h)
        self.assertEqual(_worker(self.h, model="").run()["reason"], "no_llm")

    def test_demand_reports_pressure_only_when_an_entry_is_owed(self) -> None:
        worker = _worker(self.h)
        signal = worker.demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)
        _three(self.h)
        signal = worker.demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertGreater(signal.pressure, 0.0)
        self.assertTrue(signal.needs_llm)


class BacklogTests(unittest.TestCase):
    """One page per cooldown is a ceiling, and a ceiling below the arrival
    rate is a diary that falls further behind every week it runs. The live
    graph had 273 unreported changes against a 12-a-week drain.
    """

    def setUp(self) -> None:
        self.h = _harness()

    def _stock(self, count: int, *, start: int = 0) -> None:
        for n in range(start, start + count):
            _event(self.h, n)

    def _pending(self) -> int:
        return self.h.learning.count_since(
            self.h.diary.latest_watermark(), min_salience=0.45,
        )

    def test_a_deep_backlog_releases_the_cooldown(self) -> None:
        self._stock(100)
        worker = _worker(self.h)
        worker.run()
        # The cooldown is stamped and the clock has not moved, yet enough
        # is still waiting that the period counts as over.
        self.assertIn(KV_LAST_FIRED_AT, self.h.kv)
        self.assertEqual(self.h.diary.count(), 3)
        worker.run()
        self.assertEqual(self.h.diary.count(), 6)

    def test_a_shallow_backlog_still_waits_for_the_clock(self) -> None:
        """The release needs a real backlog; one busy afternoon is not one."""
        self._stock(14)
        worker = _worker(self.h)
        worker.run()
        self._stock(4, start=14)
        self.assertEqual(worker.run()["reason"], "cooldown")
        self.assertEqual(self.h.diary.count(), 1)

    def test_a_catch_up_tick_composes_several_entries(self) -> None:
        self._stock(200)
        stats = _worker(self.h).run()
        self.assertEqual(stats["entries"], 3)
        self.assertEqual(stats["events"], 36)
        self.assertEqual(self.h.diary.count(), 3)

    def test_the_page_cap_bounds_one_tick(self) -> None:
        self.h.settings.evolution_diary_backlog_pages = 2
        self._stock(200)
        self.assertEqual(_worker(self.h).run()["entries"], 2)

    def test_keeping_pace_still_writes_one_entry_a_period(self) -> None:
        """The ordinary rhythm is untouched by the catch-up path."""
        self._stock(10)
        stats = _worker(self.h).run()
        self.assertEqual(stats.get("entries", 1), 1)
        self.assertEqual(self.h.diary.count(), 1)

    def test_the_backlog_shrinks_over_simulated_days(self) -> None:
        """The regression the existing tests could not see: they pop the
        cooldown key between pages, so the pacing mismatch was invisible.
        Here the cooldown stays in place and the clock does the work.
        """
        self._stock(200)
        day = [0]
        worker = EvolutionDiaryWorker(
            learning_store=self.h.learning,
            diary_store=self.h.diary,
            memory_settings=self.h.settings,
            agent_settings=self.h.agent,
            ollama=FakeOllama(ENTRY),
            chat_model="m",
            kv_get=self.h.kv.get,
            kv_set=lambda k, v: self.h.kv.__setitem__(k, v),
            user_name_provider=lambda: "Jacob",
            clock=lambda: NOW + timedelta(days=day[0]),
        )
        start = self._pending()
        self.assertEqual(start, 200)
        seen = [start]
        for d in range(14):
            day[0] = d
            worker.run()
            seen.append(self._pending())
        # Down to under a single page, from a backlog that the old ceiling
        # of twelve a week would have taken four months to clear.
        self.assertLess(seen[-1], 12)
        # And the last of it is paced normally again: once the backlog is
        # shallow the cooldown takes back over, which is the point.
        self.assertGreater(seen[-1], 0)
        # Monotone: no tick may put events back above the watermark.
        self.assertEqual(seen, sorted(seen, reverse=True))

    def test_an_empty_compose_stops_the_catch_up_rather_than_burning_calls(
        self,
    ) -> None:
        self._stock(200)
        ollama = FakeOllama("")
        stats = _worker(self.h, ollama=ollama).run()
        self.assertEqual(stats["reason"], "empty")
        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(self.h.diary.count(), 0)

    def test_state_reports_what_the_next_tick_will_do(self) -> None:
        self._stock(200)
        state = _worker(self.h).state()
        self.assertEqual(state["backlog_release_floor"], 24)
        self.assertEqual(state["pages_next_run"], 3)
        self._stock(0)


class CompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _harness()

    def test_the_entry_records_the_provenance_it_summarised(self) -> None:
        ids = _three(self.h)
        _worker(self.h, ollama=FakeOllama("I stopped hedging this week.")).run()
        [entry] = self.h.diary.list()
        self.assertEqual(entry.entry, "I stopped hedging this week.")
        self.assertEqual(sorted(entry.learning_event_ids), sorted(ids))
        self.assertEqual(sorted(entry.concept_ids), [100, 101, 102])
        self.assertEqual(entry.shape_counts, {"emergence": 3})
        self.assertEqual(entry.event_watermark, max(ids))

    def test_the_period_spans_the_events_it_covers(self) -> None:
        _three(self.h)
        _worker(self.h).run()
        [entry] = self.h.diary.list()
        self.assertLess(entry.period_start, entry.period_end)

    def test_the_period_is_the_span_not_the_page_order(self) -> None:
        # The backfill writes in concept-id order while each change keeps
        # the date it happened, so the oldest change can arrive last.
        _event(self.h, 0, created_at=_iso(4))
        _event(self.h, 1, created_at=_iso(30))
        _event(self.h, 2, created_at=_iso(12))
        _worker(self.h).run()
        [entry] = self.h.diary.list()
        self.assertEqual(entry.period_start, _iso(30))
        self.assertEqual(entry.period_end, _iso(4))

    def test_the_watermark_stops_a_change_being_told_twice(self) -> None:
        _three(self.h)
        worker = _worker(self.h)
        worker.run()
        self.h.kv.pop(KV_LAST_FIRED_AT, None)
        self.assertEqual(worker.run()["reason"], "nothing_to_report")
        self.assertEqual(self.h.diary.count(), 1)

    def test_later_changes_resume_from_the_watermark(self) -> None:
        first = _three(self.h)
        worker = _worker(self.h)
        worker.run()
        self.h.kv.pop(KV_LAST_FIRED_AT, None)
        later = [_event(self.h, n) for n in range(3, 6)]
        worker.run()
        entries = self.h.diary.list()
        self.assertEqual(len(entries), 2)
        self.assertEqual(sorted(entries[0].learning_event_ids), sorted(later))
        self.assertEqual(sorted(entries[1].learning_event_ids), sorted(first))

    def test_a_flood_of_rewordings_cannot_take_the_whole_page(self) -> None:
        """The H15 regression, at the page level.

        `succession` — a belief reworded into a near-identical one — is
        79% of everything the classifier produces. Without a cap the
        diary is structurally a log of relabelings, and the two shapes
        worth reading about (a belief forming, a belief lost) never
        appear even when they are sitting right there in the same run.
        """
        for n in range(20):
            _event(self.h, n, shape="succession")
        _event(self.h, 20, shape="emergence")
        _event(self.h, 21, shape="loss")

        _worker(self.h).run()

        [entry] = self.h.diary.list()
        # Both rare shapes reach the page even though twenty rewordings
        # queued ahead of them; before the cap the page was simply the
        # first twelve by id and neither ever appeared.
        self.assertEqual(entry.shape_counts.get("emergence"), 1)
        self.assertEqual(entry.shape_counts.get("loss"), 1)
        # Backfill still fills the page — the cap decides who gets in,
        # not how full the page is.
        self.assertEqual(sum(entry.shape_counts.values()), 12)

    def test_the_cap_does_not_starve_a_page_of_only_rewordings(self) -> None:
        # Nothing else arrived, so the cap must not leave the page
        # two-thirds empty and the backlog growing: it bounds one shape's
        # share of a *contested* page, not the page itself.
        for n in range(20):
            _event(self.h, n, shape="succession")

        _worker(self.h).run()

        [entry] = self.h.diary.list()
        self.assertEqual(entry.shape_counts, {"succession": 12})

    def test_an_uncontested_page_consumes_nothing_it_did_not_narrate(
        self,
    ) -> None:
        """The watermark may only advance over what the page accounted for.

        Advancing past rows a bounded pass never used is the "global
        MAX(id)" defect this codebase has already been bitten by; here it
        would silently delete a fortnight of history.
        """
        ids = [_event(self.h, n, shape="succession") for n in range(20)]

        worker = _worker(self.h)
        worker.run()
        [entry] = self.h.diary.list()
        self.assertEqual(entry.event_watermark, ids[11])

        # The remaining eight are still waiting, not skipped.
        self.h.kv.pop(KV_LAST_FIRED_AT, None)
        worker.run()
        second = self.h.diary.list()[0]
        self.assertEqual(
            sorted(second.learning_event_ids), sorted(ids[12:])
        )

    def test_a_contested_page_drops_the_rewordings_it_passed_over(
        self,
    ) -> None:
        # The deliberate loss the cap buys, and the reason it is bounded
        # to the page's own span: rewordings that lost a contested page
        # are gone rather than queued forever behind the shapes that beat
        # them.
        for n in range(11):
            _event(self.h, n, shape="succession")
        last = _event(self.h, 11, shape="emergence")

        worker = _worker(self.h)
        worker.run()

        [entry] = self.h.diary.list()
        self.assertEqual(entry.event_watermark, last)
        self.assertIn(last, entry.learning_event_ids)
        self.h.kv.pop(KV_LAST_FIRED_AT, None)
        self.assertEqual(worker.run()["reason"], "nothing_to_report")

    def test_an_empty_compose_spends_the_period_but_keeps_the_changes(
        self,
    ) -> None:
        _three(self.h)
        stats = _worker(self.h, ollama=FakeOllama("")).run()
        self.assertEqual(stats["reason"], "empty")
        self.assertEqual(self.h.diary.count(), 0)
        # Cooldown spent, so it does not loop on the same material...
        self.assertIn(KV_LAST_FIRED_AT, self.h.kv)
        # ...but the changes are still there to try again with.
        self.assertEqual(self.h.diary.latest_watermark(), 0)

    def test_a_one_word_reply_is_treated_as_empty(self) -> None:
        _three(self.h)
        stats = _worker(self.h, ollama=FakeOllama("hm")).run()
        self.assertEqual(stats["reason"], "empty")
        self.assertEqual(self.h.diary.count(), 0)

    def test_a_model_failure_is_survivable(self) -> None:
        _three(self.h)
        stats = _worker(self.h, ollama=FakeOllama(fail=True)).run()
        self.assertEqual(stats["reason"], "empty")
        self.assertEqual(self.h.diary.count(), 0)

    def test_the_prompt_is_grounded_in_the_stored_prose(self) -> None:
        _three(self.h)
        ollama = FakeOllama("an entry")
        _worker(self.h, ollama=ollama).run()
        [messages] = ollama.calls
        prompt = messages[-1]["content"]
        self.assertIn("enough moments pointed at belief number 0", prompt)
        self.assertIn("Jacob", prompt)
        system = messages[0]["content"]
        self.assertIn("Use ONLY what the list says", system)

    def test_events_reach_the_prompt_oldest_first(self) -> None:
        _three(self.h)
        ollama = FakeOllama("an entry")
        _worker(self.h, ollama=ollama).run()
        prompt = ollama.calls[0][-1]["content"]
        self.assertLess(
            prompt.index("belief number 0"), prompt.index("belief number 2")
        )

    def test_a_large_period_spills_into_the_next_entry_in_order(self) -> None:
        ids = [_event(self.h, n) for n in range(20)]
        worker = _worker(self.h)
        worker.run()
        [first] = self.h.diary.list()
        self.assertLessEqual(len(first.learning_event_ids), 12)
        # The oldest changes are narrated first, and the watermark stops
        # where the page did -- the remainder is deferred, never stranded.
        self.assertEqual(list(first.learning_event_ids), ids[:12])
        self.assertEqual(first.event_watermark, ids[11])

        self.h.kv.pop(KV_LAST_FIRED_AT, None)
        worker.run()
        second = self.h.diary.list()[0]
        self.assertEqual(list(second.learning_event_ids), ids[12:])

    def test_state_explains_why_nothing_fired(self) -> None:
        state = _worker(self.h).state()
        self.assertEqual(state["blocker"], "nothing_to_report")
        self.assertEqual(state["pending"], 0)
        _three(self.h)
        state = _worker(self.h).state()
        self.assertIsNone(state["blocker"])
        self.assertEqual(state["pending"], 3)


if __name__ == "__main__":
    unittest.main()
