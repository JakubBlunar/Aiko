"""L17e: the rare T6 "my read on this has changed" permission slip.

The one place the learning history touches the conversation, so the
gates and the anti-machinery framing are the whole point of the tests.
"""
from __future__ import annotations

import json
import unittest
from datetime import timedelta
from types import SimpleNamespace

from app.core.concepts.concept_drift_worker import DRIFT_PENDING_KEY
from app.core.infra import timephrase
from app.core.session.inner_life_part3 import InnerLifePart3Mixin


_LAST_KEY = "concept.drift.last_reflection"
_FP_KEY = "concept.drift.last_reflection_fp"

_ITEM = {
    "fingerprint": "fp-1",
    "shape": "succession",
    "subject": "user",
    "old": "likes detailed answers",
    "new": "prefers depth calibrated to the topic",
    "because": "he asked for shorter answers about ops three times",
    "salience": 0.8,
}


class _Kv:
    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self.data = dict(seed or {})

    def kv_get(self, key: str) -> str | None:
        return self.data.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.data[key] = value


class _Overrides:
    def __init__(self, armed: set[str] | None = None) -> None:
        self.armed = armed or set()

    def take(self, name: str, default: bool = False) -> bool:
        if name in self.armed:
            self.armed.discard(name)
            return True
        return default


class _Axes:
    def __init__(self, trust=0.6, closeness=0.6, comfort=0.6) -> None:
        self.trust = trust
        self.closeness = closeness
        self.comfort = comfort


class _AxesStore:
    def __init__(self, axes: _Axes) -> None:
        self._axes = axes

    def get(self, _user_id: str) -> _Axes:
        return self._axes


class _Host(InnerLifePart3Mixin):
    def __init__(
        self,
        *,
        pending: list[dict] | None = None,
        axes: _Axes | None = None,
        lull: float | None = 0.4,
        kv: dict[str, str] | None = None,
        armed: set[str] | None = None,
        reflection_enabled: bool = True,
        concepts_enabled: bool = True,
    ) -> None:
        seed = dict(kv or {})
        if pending is not None:
            seed[DRIFT_PENDING_KEY] = json.dumps(pending)
        self._chat_db = _Kv(seed)
        self._debug_overrides = _Overrides(armed)
        self._relationship_axes_store = _AxesStore(axes or _Axes())
        self._topic_stagnation_detector = SimpleNamespace(last_mean=lull)
        self._user_id = "u1"
        self.user_display_name = "Ben"
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                concepts_enabled=concepts_enabled,
                concept_learning_reflection_enabled=reflection_enabled,
            )
        )
        self._memory_settings = SimpleNamespace(
            concept_reflection_min_salience=0.6,
            concept_reflection_min_axes=0.3,
            concept_reflection_cooldown_days=30.0,
            stagnation_mild_threshold=0.18,
        )
        self._learning_reflection_fired = False


class RenderTests(unittest.TestCase):
    def test_renders_the_change_in_plain_language(self) -> None:
        host = _Host(pending=[_ITEM])
        out = host._render_concept_learning_block("hi")
        self.assertIn("likes detailed answers", out)
        self.assertIn("prefers depth calibrated to the topic", out)
        self.assertIn("he asked for shorter answers about ops", out)

    def test_never_leaks_machinery(self) -> None:
        # Only the data-bearing half is checked: the instruction half
        # names the vocabulary precisely in order to forbid it.
        host = _Host(pending=[_ITEM])
        out = host._render_concept_learning_block("hi").lower()
        facts = out.split("if it genuinely fits")[0]
        for banned in (
            "salience",
            "fingerprint",
            "succession",
            "concept",
            "confidence",
            "event",
            "score",
            "0.8",
        ):
            self.assertNotIn(banned, facts, f"leaked {banned!r}")

    def test_the_instruction_forbids_the_vocabulary_by_name(self) -> None:
        host = _Host(pending=[_ITEM])
        out = host._render_concept_learning_block("hi").lower()
        instruction = out.split("if it genuinely fits")[1]
        for forbidden in ("memory", "tracking", "confidence", "machinery"):
            self.assertIn(forbidden, instruction)

    def test_asks_for_a_statement_not_a_question(self) -> None:
        host = _Host(pending=[_ITEM])
        out = host._render_concept_learning_block("hi")
        self.assertIn("State it rather than asking", out)
        self.assertIn("fallible", out)

    def test_emergence_without_an_old_wording_still_reads(self) -> None:
        item = dict(_ITEM, old="")
        host = _Host(pending=[item])
        out = host._render_concept_learning_block("hi")
        self.assertIn("settled into thinking", out)
        self.assertIn("prefers depth calibrated to the topic", out)

    def test_missing_because_is_omitted_cleanly(self) -> None:
        host = _Host(pending=[dict(_ITEM, because="")])
        out = host._render_concept_learning_block("hi")
        self.assertIn("prefers depth", out)
        self.assertNotIn("What moved you", out)


class GateTests(unittest.TestCase):
    def test_empty_snapshot_says_nothing(self) -> None:
        self.assertEqual(
            _Host(pending=[])._render_concept_learning_block("hi"), ""
        )
        self.assertEqual(_Host()._render_concept_learning_block("hi"), "")

    def test_malformed_snapshot_says_nothing(self) -> None:
        host = _Host(kv={DRIFT_PENDING_KEY: "not json"})
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_feature_flag_off(self) -> None:
        host = _Host(pending=[_ITEM], reflection_enabled=False)
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_concept_layer_off(self) -> None:
        host = _Host(pending=[_ITEM], concepts_enabled=False)
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_once_per_conversation(self) -> None:
        host = _Host(pending=[_ITEM])
        self.assertNotEqual(host._render_concept_learning_block("hi"), "")
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_already_spoken_change_is_skipped(self) -> None:
        host = _Host(pending=[_ITEM], kv={_FP_KEY: "fp-1"})
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_a_newer_change_gets_through_the_watermark(self) -> None:
        newer = dict(_ITEM, fingerprint="fp-2", new="something newer entirely")
        host = _Host(pending=[_ITEM, newer], kv={_FP_KEY: "fp-1"})
        self.assertIn(
            "something newer entirely",
            host._render_concept_learning_block("hi"),
        )

    def test_global_cooldown(self) -> None:
        recent = (timephrase.utcnow() - timedelta(days=3)).isoformat()
        host = _Host(pending=[_ITEM], kv={_LAST_KEY: recent})
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_expired_cooldown_allows_it(self) -> None:
        old = (timephrase.utcnow() - timedelta(days=90)).isoformat()
        host = _Host(pending=[_ITEM], kv={_LAST_KEY: old})
        self.assertNotEqual(host._render_concept_learning_block("hi"), "")

    def test_low_trust_stays_quiet(self) -> None:
        host = _Host(pending=[_ITEM], axes=_Axes(trust=0.05))
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_low_warmth_stays_quiet(self) -> None:
        host = _Host(
            pending=[_ITEM], axes=_Axes(closeness=0.05, comfort=0.05)
        )
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_low_salience_stays_quiet(self) -> None:
        host = _Host(pending=[dict(_ITEM, salience=0.1)])
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_needs_a_lull_or_live_relevance(self) -> None:
        busy = _Host(pending=[_ITEM], lull=0.0)
        self.assertEqual(busy._render_concept_learning_block("hi"), "")

    def test_live_relevance_substitutes_for_a_lull(self) -> None:
        host = _Host(pending=[_ITEM], lull=0.0)
        out = host._render_concept_learning_block(
            "do you want detailed answers or shorter ones about depth?"
        )
        self.assertNotEqual(out, "")

    def test_one_shared_word_is_not_relevance(self) -> None:
        host = _Host(pending=[_ITEM], lull=0.0)
        self.assertEqual(
            host._render_concept_learning_block("what about depth"), ""
        )

    def test_debug_force_bypasses_every_gate(self) -> None:
        host = _Host(
            pending=[_ITEM],
            axes=_Axes(trust=0.0, closeness=0.0, comfort=0.0),
            lull=0.0,
            kv={_LAST_KEY: timephrase.utcnow().isoformat()},
            armed={"concept_learning_force_next"},
        )
        self.assertNotEqual(host._render_concept_learning_block(""), "")

    def test_firing_persists_the_cooldown_and_watermark(self) -> None:
        host = _Host(pending=[_ITEM])
        host._render_concept_learning_block("hi")
        self.assertEqual(host._chat_db.data[_FP_KEY], "fp-1")
        self.assertIn(_LAST_KEY, host._chat_db.data)

    def test_missing_axes_store_stays_quiet(self) -> None:
        host = _Host(pending=[_ITEM])
        host._relationship_axes_store = None
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_missing_database_stays_quiet(self) -> None:
        host = _Host(pending=[_ITEM])
        host._chat_db = None
        self.assertEqual(host._render_concept_learning_block("hi"), "")

    def test_blank_new_wording_is_skipped(self) -> None:
        host = _Host(pending=[dict(_ITEM, new="  ")])
        self.assertEqual(host._render_concept_learning_block("hi"), "")


if __name__ == "__main__":
    unittest.main()
