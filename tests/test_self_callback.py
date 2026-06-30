"""Pure-module tests for K71 self-callback."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.affect import self_callback as sc


@dataclass
class _Mem:
    id: int
    content: str
    created_at: str


def _aged(days: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()


class ClassifyTests(unittest.TestCase):
    def test_feeling(self) -> None:
        self.assertEqual(
            sc.classify_self_memory("I've been feeling restless lately"),
            sc.KIND_FEELING,
        )
        self.assertEqual(
            sc.classify_self_memory("honestly I felt pretty low last week"),
            sc.KIND_FEELING,
        )

    def test_intention(self) -> None:
        self.assertEqual(
            sc.classify_self_memory("I want to get back into astronomy"),
            sc.KIND_INTENTION,
        )
        self.assertEqual(
            sc.classify_self_memory("I've been meaning to read more"),
            sc.KIND_INTENTION,
        )

    def test_feeling_beats_intention(self) -> None:
        # Contains both cues; feeling wins.
        self.assertEqual(
            sc.classify_self_memory(
                "I've been feeling restless and I want to fix that"
            ),
            sc.KIND_FEELING,
        )

    def test_biographical_fact_is_other(self) -> None:
        self.assertEqual(
            sc.classify_self_memory("I have a sister named Mai"),
            sc.KIND_OTHER,
        )
        self.assertEqual(sc.classify_self_memory(""), sc.KIND_OTHER)


class SelectCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)

    def test_picks_aged_feeling(self) -> None:
        mems = [
            _Mem(1, "I've been feeling restless", _aged(30)),
            _Mem(2, "I have two cats", _aged(40)),
        ]
        cand = sc.select_candidate(mems, now=self.now, min_age_days=14)
        self.assertIsNotNone(cand)
        assert cand is not None
        self.assertEqual(cand.memory_id, 1)
        self.assertEqual(cand.kind, sc.KIND_FEELING)
        self.assertEqual(cand.signature, "self:1")

    def test_too_young_skipped(self) -> None:
        mems = [_Mem(1, "I've been feeling low", _aged(5))]
        self.assertIsNone(
            sc.select_candidate(mems, now=self.now, min_age_days=14)
        )

    def test_oldest_qualifying_wins(self) -> None:
        mems = [
            _Mem(1, "I want to learn guitar", _aged(20)),
            _Mem(2, "I've been feeling anxious", _aged(60)),
        ]
        cand = sc.select_candidate(mems, now=self.now, min_age_days=14)
        assert cand is not None
        self.assertEqual(cand.memory_id, 2)

    def test_excluded_signature_skipped(self) -> None:
        mems = [
            _Mem(1, "I've been feeling low", _aged(60)),
            _Mem(2, "I want to travel more", _aged(30)),
        ]
        cand = sc.select_candidate(
            mems, now=self.now, min_age_days=14,
            exclude_signatures={"self:1"},
        )
        assert cand is not None
        self.assertEqual(cand.memory_id, 2)

    def test_no_qualifying_returns_none(self) -> None:
        mems = [_Mem(1, "I own a red bike", _aged(40))]
        self.assertIsNone(
            sc.select_candidate(mems, now=self.now, min_age_days=14)
        )

    def test_excerpt_truncated(self) -> None:
        long = "I've been feeling " + "restless " * 50
        mems = [_Mem(1, long, _aged(30))]
        cand = sc.select_candidate(
            mems, now=self.now, min_age_days=14, max_excerpt_chars=40,
        )
        assert cand is not None
        self.assertLessEqual(len(cand.excerpt), 41)


class RenderTests(unittest.TestCase):
    def test_feeling_render(self) -> None:
        line = sc.render_inner_life_block(
            sc.KIND_FEELING, "I've been feeling restless", 30,
            user_display_name="Jacob",
        )
        self.assertIn("Jacob", line)
        self.assertIn("restless", line)
        self.assertIn("close the loop", line)

    def test_intention_render(self) -> None:
        line = sc.render_inner_life_block(
            sc.KIND_INTENTION, "I want to get into astronomy", 50,
        )
        self.assertIn("astronomy", line)
        self.assertIn("follow through", line)

    def test_empty_excerpt_silent(self) -> None:
        self.assertEqual(
            sc.render_inner_life_block(sc.KIND_FEELING, "", 30), ""
        )

    def test_unknown_kind_silent(self) -> None:
        self.assertEqual(
            sc.render_inner_life_block("bogus", "something", 30), ""
        )


class GatherAndSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)

    def test_gather_includes_other_kinds(self) -> None:
        # gather (unlike select_candidate) keeps facts so the LLM can judge.
        mems = [
            _Mem(1, "I own a red bike", _aged(30)),
            _Mem(2, "I've been feeling low", _aged(40)),
        ]
        got = sc.gather_aged_candidates(
            mems, now=self.now, min_age_days=14,
        )
        ids = {c.memory_id for c in got}
        self.assertEqual(ids, {1, 2})
        # Oldest first.
        self.assertEqual(got[0].memory_id, 2)

    def test_gather_respects_age_and_exclude_and_cap(self) -> None:
        mems = [_Mem(i, f"thought {i}", _aged(20 + i)) for i in range(1, 6)]
        mems.append(_Mem(99, "too new", _aged(2)))
        got = sc.gather_aged_candidates(
            mems, now=self.now, min_age_days=14,
            exclude_signatures={"self:1"}, max_candidates=3,
        )
        self.assertEqual(len(got), 3)
        self.assertNotIn(1, {c.memory_id for c in got})
        self.assertNotIn(99, {c.memory_id for c in got})

    def test_build_prompt_lists_ids(self) -> None:
        cands = sc.gather_aged_candidates(
            [_Mem(7, "I want to learn guitar", _aged(30))],
            now=self.now, min_age_days=14,
        )
        system, user = sc.build_selection_prompt(
            cands, user_display_name="Jacob",
        )
        self.assertIn("Jacob", system)
        self.assertIn("id=7", user)

    def test_parse_selection_valid(self) -> None:
        out = sc.parse_selection(
            '{"memory_id": 5, "kind": "feeling", "worth": true}', {5, 6},
        )
        self.assertEqual(out, {"memory_id": 5, "kind": "feeling"})

    def test_parse_selection_worth_false(self) -> None:
        self.assertIsNone(
            sc.parse_selection(
                '{"memory_id": 5, "kind": "feeling", "worth": false}', {5},
            )
        )

    def test_parse_selection_out_of_range(self) -> None:
        self.assertIsNone(
            sc.parse_selection('{"memory_id": 9, "kind": "feeling"}', {5, 6})
        )

    def test_parse_selection_bad_kind(self) -> None:
        self.assertIsNone(
            sc.parse_selection('{"memory_id": 5, "kind": "fact"}', {5})
        )

    def test_parse_selection_garbage(self) -> None:
        self.assertIsNone(sc.parse_selection("not json", {5}))
        self.assertIsNone(sc.parse_selection("", {5}))


class RingHelperTests(unittest.TestCase):
    def test_round_trip_cap_and_signatures(self) -> None:
        store: dict[str, str] = {}

        def kv_get(k: str):
            return store.get(k)

        def kv_set(k: str, v: str):
            store[k] = v

        for i in range(6):
            sc.append_callback(
                kv_get, kv_set,
                {"at": f"t{i}", "signature": f"self:{i}"},
                max_entries=4,
            )
        ring = sc.load_callbacks(kv_get)
        self.assertEqual(len(ring), 4)
        sigs = sc.recent_signatures(kv_get)
        self.assertIn("self:5", sigs)
        self.assertNotIn("self:0", sigs)

    def test_load_tolerates_garbage(self) -> None:
        self.assertEqual(sc.load_callbacks(lambda _k: "xx"), [])
        self.assertEqual(sc.load_callbacks(lambda _k: None), [])


if __name__ == "__main__":
    unittest.main()
