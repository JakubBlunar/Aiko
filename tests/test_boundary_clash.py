"""Tests for the L18b learned-style addendum and the L18c boundary-clash cue.

Three layers:

* ``BoundaryClashDetectorTests`` / ``RenderTests`` -- the pure detector
  module (cosine + word-count gate, top-candidate pick, classify_pair
  sharpen, render copy + subject framing).
* ``BoundaryClashProviderTests`` -- the controller-level provider plumbing
  (master switch, cooldown decrement / arm, per-session cap, dependency
  surface), hosted on a minimal ``InnerLifeProvidersMixin`` stub like the
  K29 provider tests.
* ``LearnedStyleAddendumTests`` -- the L18b T0 steer: the builder output
  and that it lands in an assembled system prompt.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np

from app.core.affect import boundary_clash_detector as bcd
from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.prompt_support import build_learned_style_addendum


def _cand(
    *,
    cosine: float,
    label: str = "prefers you keep the teasing gentle",
    subject: str = "user",
    concept_id: int = 1,
) -> bcd.BoundaryCandidate:
    return bcd.BoundaryCandidate(
        concept_id=concept_id, subject=subject, label=label, cosine=cosine,
    )


LONG_TURN = "honestly I really don't want you poking fun at this right now"


# ── Detector ──────────────────────────────────────────────────────────────


class BoundaryClashDetectorTests(unittest.TestCase):
    def test_fires_above_cosine_as_approach(self) -> None:
        result = bcd.detect(
            LONG_TURN, candidates=[_cand(cosine=0.72)], min_cosine=0.58,
        )
        self.assertIsNotNone(result)
        assert result is not None
        # No lexical clash -> the gentle "approach" register.
        self.assertEqual(result.trigger, "boundary_approach")
        self.assertEqual(result.concept_id, 1)
        self.assertAlmostEqual(result.cosine, 0.72, places=5)

    def test_below_cosine_returns_none(self) -> None:
        self.assertIsNone(
            bcd.detect(
                LONG_TURN, candidates=[_cand(cosine=0.40)], min_cosine=0.58,
            )
        )

    def test_word_count_gate(self) -> None:
        self.assertIsNone(
            bcd.detect(
                "lol ok", candidates=[_cand(cosine=0.9)], min_user_words=4,
            )
        )

    def test_empty_candidates_returns_none(self) -> None:
        self.assertIsNone(bcd.detect(LONG_TURN, candidates=[]))

    def test_blank_text_returns_none(self) -> None:
        self.assertIsNone(bcd.detect("   ", candidates=[_cand(cosine=0.9)]))

    def test_top_candidate_is_picked(self) -> None:
        result = bcd.detect(
            LONG_TURN,
            candidates=[
                _cand(cosine=0.60, concept_id=1, label="line one"),
                _cand(cosine=0.81, concept_id=2, label="line two"),
                _cand(cosine=0.59, concept_id=3, label="line three"),
            ],
            min_cosine=0.58,
        )
        assert result is not None
        self.assertEqual(result.concept_id, 2)
        self.assertAlmostEqual(result.cosine, 0.81, places=5)

    def test_classify_pair_sharpens_to_push(self) -> None:
        # A definite lexical clash firms the register to "push". Patch
        # classify_pair so the test doesn't depend on the heuristic's
        # internal negation-flip tuning.
        fake = SimpleNamespace(label="definite", signals=["negation_flip"])
        with mock.patch.object(bcd, "classify_pair", return_value=fake):
            result = bcd.detect(
                LONG_TURN, candidates=[_cand(cosine=0.72)], min_cosine=0.58,
            )
        assert result is not None
        self.assertEqual(result.trigger, "boundary_push")
        self.assertEqual(result.heuristic_label, "definite")


class RenderTests(unittest.TestCase):
    def _result(self, *, trigger: str, subject: str) -> bcd.BoundaryClashResult:
        return bcd.BoundaryClashResult(
            trigger=trigger,  # type: ignore[arg-type]
            concept_id=1,
            subject=subject,
            label="prefers you keep the teasing gentle",
            cosine=0.7,
        )

    def test_approach_copy(self) -> None:
        block = bcd.render_inner_life_block(
            self._result(trigger="boundary_approach", subject="user"),
            user_display_name="Jacob",
        )
        self.assertIn("brushes up against", block)
        self.assertIn("Jacob", block)
        self.assertIn("never name the line out loud", block)

    def test_push_copy(self) -> None:
        block = bcd.render_inner_life_block(
            self._result(trigger="boundary_push", subject="user"),
            user_display_name="Jacob",
        )
        self.assertIn("pushing right at", block)

    def test_subject_framing(self) -> None:
        aiko = bcd.render_inner_life_block(
            self._result(trigger="boundary_approach", subject="aiko"),
            user_display_name="Jacob",
        )
        self.assertIn("one of your own lines", aiko)
        rel = bcd.render_inner_life_block(
            self._result(trigger="boundary_approach", subject="relationship"),
            user_display_name="Jacob",
        )
        self.assertIn("you and Jacob", rel)


# ── Provider ────────────────────────────────────────────────────────────────


def _vec(*values: float) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


class _FakeEmbedder:
    def __init__(self, vec: np.ndarray | None) -> None:
        self._vec = vec

    def embed(self, text: str) -> np.ndarray:
        if self._vec is None:
            raise RuntimeError("embedder unavailable")
        return self._vec


class _FakeConceptStore:
    """Concept store double exposing only ``nearest`` (what ConceptView
    .relevant calls). Ignores the embedding and returns preset pairs so
    the cosine is fully controlled by the test."""

    def __init__(self, pairs: list[tuple[Any, float]]) -> None:
        self._pairs = pairs

    def nearest(
        self,
        embedding: Any,
        *,
        status: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
        k: int = 8,
    ) -> list[tuple[Any, float]]:
        return list(self._pairs)[: int(k)]


def _concept(*, concept_id: int = 1, subject: str = "user", label: str = "prefers gentle teasing"):
    return SimpleNamespace(concept_id=concept_id, subject=subject, label=label)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        boundary_clash_min_cosine=0.58,
        boundary_clash_min_user_words=4,
        boundary_clash_cooldown_turns=5,
        boundary_clash_per_session_cap=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        pairs: list[tuple[Any, float]] | None = None,
        embedder_vec: np.ndarray | None = _vec(1.0, 0.0),
        cooldown: int = 0,
        session_count: int = 0,
        enabled: bool = True,
        memory_settings: SimpleNamespace | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(boundary_clash_enabled=enabled),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._concept_store = _FakeConceptStore(
            pairs if pairs is not None else [(_concept(), 0.72)]
        )
        self._topic_graph = None
        self._memory_store = None
        self._embedder = _FakeEmbedder(embedder_vec)
        self._boundary_clash_cooldown = cooldown
        self._boundary_clash_session_count = session_count
        self._last_boundary_clash: Any = None
        self.user_display_name = "Jacob"


class BoundaryClashProviderTests(unittest.TestCase):
    def test_fires_and_arms(self) -> None:
        host = _Host()
        block = host._render_boundary_clash_block(LONG_TURN)
        self.assertNotEqual(block, "")
        self.assertIn("Jacob", block)
        self.assertEqual(host._boundary_clash_cooldown, 5)
        self.assertEqual(host._boundary_clash_session_count, 1)
        self.assertIsNotNone(host._last_boundary_clash)

    def test_disabled_switch_untouched_cooldown(self) -> None:
        host = _Host(enabled=False, cooldown=2)
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")
        self.assertEqual(host._boundary_clash_cooldown, 2)

    def test_below_cosine_silent(self) -> None:
        host = _Host(pairs=[(_concept(), 0.40)])
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")
        self.assertEqual(host._boundary_clash_session_count, 0)
        self.assertIsNone(host._last_boundary_clash)

    def test_cooldown_blocks_and_decrements(self) -> None:
        host = _Host(cooldown=2)
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")
        self.assertEqual(host._boundary_clash_cooldown, 1)
        self.assertEqual(host._boundary_clash_session_count, 0)

    def test_session_cap_blocks(self) -> None:
        host = _Host(session_count=3)
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")
        self.assertEqual(host._boundary_clash_session_count, 3)

    def test_cap_zero_disables(self) -> None:
        host = _Host(
            session_count=999,
            memory_settings=_make_memory_settings(
                boundary_clash_per_session_cap=0,
            ),
        )
        self.assertNotEqual(host._render_boundary_clash_block(LONG_TURN), "")

    def test_no_concept_store_returns_empty(self) -> None:
        host = _Host()
        host._concept_store = None
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")

    def test_no_embedder_returns_empty(self) -> None:
        host = _Host()
        host._embedder = None
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")

    def test_embedder_failure_returns_empty(self) -> None:
        host = _Host(embedder_vec=None)
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")

    def test_no_active_boundaries_returns_empty(self) -> None:
        host = _Host(pairs=[])
        self.assertEqual(host._render_boundary_clash_block(LONG_TURN), "")


# ── L18b addendum ───────────────────────────────────────────────────────────


class LearnedStyleAddendumTests(unittest.TestCase):
    def test_default_name(self) -> None:
        text = build_learned_style_addendum()
        self.assertIn("the user", text)
        self.assertIn("just that -- defaults", text)
        self.assertIn("defaults simply stand", text)

    def test_name_aware(self) -> None:
        text = build_learned_style_addendum("Jacob")
        self.assertIn("Jacob", text)
        self.assertIn("win over the generic defaults", text)

    def test_lands_in_assembled_prompt(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        persona = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        persona.write("You are Aiko.\n\nHow you talk:\n- Keep it natural.")
        persona.close()
        db = ChatDatabase(Path(tmp_dir.name) / "t.db")
        try:
            assembler = PromptAssembler(
                db,
                persona_path=Path(persona.name),
                recent_window=20,
                cue_register_rotation_enabled=False,
            )
            db.add_message(
                session_id="s1", role="user", content="hi", token_count=1,
            )
            messages, _ = assembler.assemble_with_budget(
                "s1", "what's up", context_window=4096, response_budget=512,
            )
            system_prompt = messages[0]["content"]
            self.assertIn("win over the generic defaults", system_prompt)
        finally:
            conn = getattr(db._local, "conn", None)
            if conn is not None:
                conn.close()


if __name__ == "__main__":
    unittest.main()
