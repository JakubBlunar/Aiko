"""Tests for UserProfileStore + UserProfileWorker (Phase 3a)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.core.infra.chat_database import ChatDatabase
from app.core.infra.user_profile import (
    PROFILE_FIELDS,
    UserProfileStore,
    UserProfileWorker,
    _parse_profile_payload,
)
from app.core.session.inner_life_part1 import InnerLifePart1Mixin


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = UserProfileStore(self.db)

    def close(self):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class _FakeOllama:
    def __init__(self, response: str = "{}") -> None:
        self.response = response
        self.calls: list[dict] = []
        self.fail = False

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return self.response


class ParseProfilePayloadTests(unittest.TestCase):
    def test_parses_clean_object(self):
        raw = (
            '{"fields":{"name":{"value":"Jacob","confidence":0.9},'
            '"hobbies":{"value":"chess, hiking","confidence":0.6}}}'
        )
        out = _parse_profile_payload(raw)
        self.assertEqual(out["name"], ("Jacob", 0.9))
        self.assertEqual(out["hobbies"], ("chess, hiking", 0.6))

    def test_drops_unknown_fields(self):
        raw = '{"fields":{"hair_color":{"value":"blue","confidence":0.9}}}'
        self.assertEqual(_parse_profile_payload(raw), {})

    def test_handles_string_values(self):
        raw = '{"fields":{"name":"Jacob"}}'
        self.assertEqual(_parse_profile_payload(raw), {"name": ("Jacob", 0.5)})

    def test_handles_garbage(self):
        self.assertEqual(_parse_profile_payload("nonsense"), {})

    def test_handles_fences(self):
        raw = '```json\n{"fields":{"goals":{"value":"ship phase 3","confidence":0.7}}}\n```'
        out = _parse_profile_payload(raw)
        self.assertIn("goals", out)


class UserProfileStoreTests(unittest.TestCase):
    def test_upsert_inserts_new(self):
        f = _Fixture()
        try:
            wrote = f.store.upsert("u1", "name", "Jacob", 0.9)
            self.assertTrue(wrote)
            entries = f.store.fields("u1")
            self.assertEqual(entries["name"].value, "Jacob")
        finally:
            f.close()

    def test_upsert_keeps_existing_for_lower_confidence(self):
        f = _Fixture()
        try:
            f.store.upsert("u2", "name", "Jacob", 0.9)
            wrote = f.store.upsert("u2", "name", "Jake", 0.5)
            self.assertFalse(wrote)
            entries = f.store.fields("u2")
            self.assertEqual(entries["name"].value, "Jacob")
        finally:
            f.close()

    def test_upsert_updates_with_higher_or_equal_confidence(self):
        f = _Fixture()
        try:
            f.store.upsert("u3", "occupation", "engineer", 0.5)
            wrote = f.store.upsert("u3", "occupation", "principal engineer", 0.6)
            self.assertTrue(wrote)
            entries = f.store.fields("u3")
            self.assertEqual(entries["occupation"].value, "principal engineer")
        finally:
            f.close()

    def test_upsert_same_value_bumps_confidence(self):
        f = _Fixture()
        try:
            f.store.upsert("u4", "hobbies", "chess", 0.5)
            wrote = f.store.upsert("u4", "hobbies", "chess", 0.7)
            self.assertFalse(wrote)
            entries = f.store.fields("u4")
            self.assertAlmostEqual(entries["hobbies"].confidence, 0.7, places=2)
        finally:
            f.close()

    def test_render_block_skips_low_confidence(self):
        f = _Fixture()
        try:
            f.store.upsert("u5", "name", "Jacob", 0.9)
            f.store.upsert("u5", "occupation", "wizard", 0.2)  # below threshold
            block = f.store.render_block("u5", min_confidence=0.4)
            self.assertIn("Jacob", block)
            self.assertNotIn("wizard", block)
        finally:
            f.close()

    def test_render_block_empty_when_no_high_confidence(self):
        f = _Fixture()
        try:
            f.store.upsert("u6", "name", "Jacob", 0.1)
            self.assertEqual(f.store.render_block("u6", min_confidence=0.4), "")
        finally:
            f.close()

    def test_as_dict_includes_all_fields(self):
        f = _Fixture()
        try:
            f.store.upsert("u7", "name", "Jacob", 0.8)
            f.store.upsert("u7", "occupation", "engineer", 0.7)
            data = f.store.as_dict("u7")
            self.assertEqual(set(data.keys()), {"name", "occupation"})
            self.assertEqual(data["name"]["value"], "Jacob")
        finally:
            f.close()


class UserProfileWorkerTests(unittest.TestCase):
    def _make_worker(self, response: str, **overrides):
        f = _Fixture()
        ollama = _FakeOllama(response=response)
        kwargs = {
            "ollama": ollama,
            "db": f.db,
            "store": f.store,
            "model": "m",
            "min_user_turns": 3,
        }
        kwargs.update(overrides)
        worker = UserProfileWorker(**kwargs)
        return f, ollama, worker

    def test_runs_after_min_turns_and_persists(self):
        f, ollama, worker = self._make_worker(
            response='{"fields":{"name":{"value":"Jacob","confidence":0.9}}}',
            min_user_turns=2,
        )
        try:
            for _ in range(2):
                worker.notify_user_turn()
            self.assertTrue(worker.should_run())

            def _hist():
                return [
                    ("user", "I'm Jacob and I work as an engineer"),
                    ("assistant", "Nice to meet you Jacob"),
                ]

            result = worker.maybe_run(
                "u1", session_key="s1", history_provider=_hist,
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertIn("name", result)
            self.assertEqual(result["name"].value, "Jacob")
            self.assertEqual(worker.stats()["fields_written"], 1)
        finally:
            f.close()

    def test_throttled_below_min_turns(self):
        f, ollama, worker = self._make_worker(response="{}", min_user_turns=5)
        try:
            for _ in range(2):
                worker.notify_user_turn()
            result = worker.maybe_run(
                "u2", session_key="s", history_provider=lambda: [],
            )
            self.assertIsNone(result)
            self.assertEqual(worker.stats()["skipped_throttled"], 1)
            self.assertEqual(ollama.calls, [])
        finally:
            f.close()

    def test_skips_with_no_history(self):
        f, ollama, worker = self._make_worker(response="{}", min_user_turns=1)
        try:
            worker.notify_user_turn()
            result = worker.maybe_run(
                "u3", session_key="s", history_provider=lambda: [],
            )
            self.assertIsNone(result)
            self.assertEqual(worker.stats()["skipped_no_input"], 1)
        finally:
            f.close()

    def test_failure_does_not_raise(self):
        f, ollama, worker = self._make_worker(
            response="{}", min_user_turns=1,
        )
        try:
            ollama.fail = True
            worker.notify_user_turn()
            result = worker.maybe_run(
                "u4", session_key="s",
                history_provider=lambda: [("user", "hi"), ("assistant", "hi")],
            )
            self.assertIsNone(result)
            self.assertEqual(worker.stats()["failed"], 1)
        finally:
            f.close()

    def test_does_not_double_run_after_completion(self):
        f, ollama, worker = self._make_worker(
            response='{"fields":{}}',
            min_user_turns=2,
        )
        try:
            for _ in range(2):
                worker.notify_user_turn()
            worker.maybe_run(
                "u5", session_key="s",
                history_provider=lambda: [("user", "hi"), ("assistant", "hi")],
            )
            # Second invocation immediately after — throttled.
            result = worker.maybe_run(
                "u5", session_key="s",
                history_provider=lambda: [("user", "more")],
            )
            self.assertIsNone(result)
        finally:
            f.close()


# ── G2: usual_hours field ─────────────────────────────────────────────


class UsualHoursFieldTests(unittest.TestCase):
    """The G2 schedule learner upserts a single ``usual_hours`` field
    via :class:`UserProfileStore`. The store has to (a) accept it as
    one of the valid fields and (b) round-trip through SQLite so the
    rendered profile block picks it up.
    """

    def test_usual_hours_is_in_profile_fields(self) -> None:
        self.assertIn("usual_hours", PROFILE_FIELDS)

    def test_round_trip_through_upsert(self) -> None:
        f = _Fixture()
        try:
            wrote = f.store.upsert(
                "user-1",
                "usual_hours",
                "weekday evenings (18-23)",
                confidence=0.8,
            )
            self.assertTrue(wrote)
            entries = f.store.fields("user-1")
            self.assertIn("usual_hours", entries)
            self.assertEqual(
                entries["usual_hours"].value, "weekday evenings (18-23)",
            )
            self.assertGreater(entries["usual_hours"].confidence, 0.0)
        finally:
            f.close()

    def test_render_block_includes_usual_hours(self) -> None:
        f = _Fixture()
        try:
            f.store.upsert(
                "user-1", "usual_hours", "weekend afternoons", 0.8,
            )
            block = f.store.render_block("user-1")
            self.assertIn("usual hours", block.lower())
            self.assertIn("weekend afternoons", block)
        finally:
            f.close()


# ── K3: routines field ────────────────────────────────────────────────


class RoutinesFieldTests(unittest.TestCase):
    """The K3 routine pass writes named ritual phrases into the
    ``routines`` field. Same shape as ``usual_hours`` — store has to
    allow-list it and the rendered block has to surface it so Aiko's
    persona block can lean into matching rhythms.
    """

    def test_routines_is_in_profile_fields(self) -> None:
        self.assertIn("routines", PROFILE_FIELDS)

    def test_round_trip_through_upsert(self) -> None:
        f = _Fixture()
        try:
            wrote = f.store.upsert(
                "user-1",
                "routines",
                "Sunday-morning chats, Friday-evening wind-downs",
                confidence=0.6,
            )
            self.assertTrue(wrote)
            entries = f.store.fields("user-1")
            self.assertIn("routines", entries)
            self.assertEqual(
                entries["routines"].value,
                "Sunday-morning chats, Friday-evening wind-downs",
            )
            self.assertGreater(entries["routines"].confidence, 0.0)
        finally:
            f.close()

    def test_render_block_includes_routines(self) -> None:
        f = _Fixture()
        try:
            f.store.upsert(
                "user-1",
                "routines",
                "Sunday-morning chats",
                0.7,
            )
            block = f.store.render_block("user-1")
            self.assertIn("routines", block.lower())
            self.assertIn("Sunday-morning chats", block)
        finally:
            f.close()


# ── L28: concept-led profile block ────────────────────────────────────


class _FakeConcept:
    def __init__(self, label: str, kind: str, confidence: float = 0.8,
                 concept_id: int = 0) -> None:
        self.label = label
        self.kind = kind
        self.confidence = confidence
        self.concept_id = concept_id


class _FakeView:
    """Minimal ConceptView stand-in for the profile composition path."""

    def __init__(self, concepts, *, enabled: bool = True) -> None:
        self._concepts = list(concepts)
        self.enabled = enabled
        self.calls: list[dict] = []

    def for_target(self, target, *, subject=None, min_confidence=0.0, limit=None):
        self.calls.append(
            {"target": target, "subject": subject,
             "min_confidence": min_confidence, "limit": limit}
        )
        rows = [
            c for c in self._concepts
            if float(c.confidence) >= float(min_confidence)
        ]
        if limit is not None:
            rows = rows[: int(limit)]
        return rows


def _profile_host(*, max_lines=10, min_confidence=0.5):
    """A lightweight fake ``self`` for the mixin helper."""
    return SimpleNamespace(
        _memory_settings=SimpleNamespace(
            profile_concept_max_lines=max_lines,
            profile_concept_min_confidence=min_confidence,
        )
    )


def _profile_lines(view, *, max_lines=10, min_confidence=0.5):
    """Invoke the mixin helper with a lightweight fake ``self``."""
    fake_self = _profile_host(
        max_lines=max_lines, min_confidence=min_confidence,
    )
    return InnerLifePart1Mixin._profile_concept_lines(fake_self, view)


def _profile_claim(view, *, max_lines=10, min_confidence=0.5):
    """The L39 claim stashed on the host by the same call."""
    fake_self = _profile_host(
        max_lines=max_lines, min_confidence=min_confidence,
    )
    InnerLifePart1Mixin._profile_concept_lines(fake_self, view)
    return fake_self._last_profile_concept_ids


class RenderBlockMergeTests(unittest.TestCase):
    def test_concept_lines_lead_and_values_suppressed(self) -> None:
        f = _Fixture()
        try:
            f.store.upsert("u1", "name", "Jacob", 0.9)
            f.store.upsert("u1", "values", "honesty", 0.8)
            block = f.store.render_block(
                "u1",
                concept_lines=["- cares about honesty above all"],
                skip_fields={"values"},
            )
            lines = block.splitlines()
            # Header, then the concept bullet leads, then the SQLite fields.
            self.assertEqual(lines[1], "- cares about honesty above all")
            self.assertIn("Jacob", block)
            # The SQLite ``values`` field is suppressed (concept owns it).
            self.assertNotIn("- values: honesty", block)
        finally:
            f.close()

    def test_header_shows_with_only_concept_lines(self) -> None:
        f = _Fixture()
        try:
            block = f.store.render_block(
                "empty-user",
                concept_lines=["- keeps a tidy dev setup"],
            )
            self.assertIn("(profile):", block)
            self.assertIn("- keeps a tidy dev setup", block)
        finally:
            f.close()

    def test_empty_with_no_concepts_and_no_fields(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(f.store.render_block("nobody"), "")
        finally:
            f.close()


class ProfileConceptLinesTests(unittest.TestCase):
    def test_identity_and_value_yield_bullets_and_skip_values(self) -> None:
        view = _FakeView([
            _FakeConcept("keeps a tidy dev setup", "identity"),
            _FakeConcept("cares about honesty", "value"),
        ])
        lines, skip = _profile_lines(view)
        self.assertEqual(
            lines, ["- keeps a tidy dev setup", "- cares about honesty"],
        )
        self.assertEqual(skip, {"values"})
        # Routed through for_target with the profile_block/user contract.
        self.assertEqual(view.calls[0]["target"], "profile_block")
        self.assertEqual(view.calls[0]["subject"], "user")

    def test_identity_only_does_not_skip_values(self) -> None:
        view = _FakeView([_FakeConcept("keeps a tidy dev setup", "identity")])
        lines, skip = _profile_lines(view)
        self.assertEqual(lines, ["- keeps a tidy dev setup"])
        self.assertEqual(skip, set())

    def test_disabled_view_is_noop(self) -> None:
        view = _FakeView([_FakeConcept("x", "value")], enabled=False)
        self.assertEqual(_profile_lines(view), ([], set()))

    def test_none_view_is_noop(self) -> None:
        self.assertEqual(_profile_lines(None), ([], set()))

    def test_zero_cap_disables_concept_lead(self) -> None:
        view = _FakeView([_FakeConcept("x", "value")])
        self.assertEqual(_profile_lines(view, max_lines=0), ([], set()))

    def test_duplicate_labels_deduped(self) -> None:
        view = _FakeView([
            _FakeConcept("cares about honesty", "value"),
            _FakeConcept("Cares About Honesty", "identity"),
        ])
        lines, _ = _profile_lines(view)
        self.assertEqual(lines, ["- cares about honesty"])


class ProfileConceptClaimTests(unittest.TestCase):
    """L39: the block stashes which concept ids it rendered, so the T3
    relevant_context lanes can skip them instead of stating the same claim
    twice in one assembly."""

    def test_rendered_concepts_are_claimed(self) -> None:
        view = _FakeView([
            _FakeConcept("keeps a tidy dev setup", "identity", concept_id=4),
            _FakeConcept("cares about honesty", "value", concept_id=9),
        ])
        self.assertEqual(_profile_claim(view), frozenset({4, 9}))

    def test_duplicate_label_sibling_is_also_claimed(self) -> None:
        # The second concept contributes no line of its own, but its text *is*
        # in the prompt via the line that won -- so T3 must skip it too.
        view = _FakeView([
            _FakeConcept("cares about honesty", "value", concept_id=9),
            _FakeConcept("Cares About Honesty", "identity", concept_id=10),
        ])
        self.assertEqual(_profile_claim(view), frozenset({9, 10}))

    def test_unrendered_concepts_are_not_claimed(self) -> None:
        # Beyond the cap, so it never reaches the prompt and T3 stays free to
        # surface it on relevance.
        view = _FakeView([
            _FakeConcept("keeps a tidy dev setup", "identity", concept_id=4),
            _FakeConcept("cares about honesty", "value", concept_id=9),
        ])
        self.assertEqual(_profile_claim(view, max_lines=1), frozenset({4}))

    def test_claim_is_cleared_on_every_early_return(self) -> None:
        # A cold or disabled layer must not leave a stale claim behind, or T3
        # would keep suppressing concepts the profile block no longer renders.
        self.assertEqual(_profile_claim(None), frozenset())
        self.assertEqual(
            _profile_claim(_FakeView([_FakeConcept("x", "value", concept_id=1)],
                                     enabled=False)),
            frozenset(),
        )
        self.assertEqual(
            _profile_claim(
                _FakeView([_FakeConcept("x", "value", concept_id=1)]),
                max_lines=0,
            ),
            frozenset(),
        )

    def test_idless_concept_is_not_claimed(self) -> None:
        # A concept with no id can't be matched downstream; claiming id 0 would
        # be a wildcard that suppresses every unidentified candidate.
        view = _FakeView([_FakeConcept("keeps a tidy dev setup", "identity")])
        self.assertEqual(_profile_claim(view), frozenset())


if __name__ == "__main__":
    unittest.main()
