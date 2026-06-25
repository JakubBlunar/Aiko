"""Tests for the K29 opinion-injection detector.

Pure-function tests on
:func:`app.core.affect.opinion_injection_detector.detect` covering
the four anti-contrarianism guardrails layered into the pipeline:

1. **Length gate** -- short user messages return None.
2. **Predicate filter** -- biographical-fact stances are excluded.
3. **Cosine threshold** -- topical match must clear ``min_cosine``.
4. **Heuristic gate** -- only ``definite`` heuristic verdicts fire
   immediately; ``borderline`` requires an LLM YES verdict via the
   caller's ``llm_gate`` callback.

The detector is pure (no I/O, no embedder, no Ollama), so we feed
it numpy vectors directly and stub the LLM gate with a Python
callable.

Render-output tests assert the cue *includes* the matched stance
text (Aiko needs to see her own prior take) and *steers Aiko's
register toward owning her preference* rather than prescribing
behaviour (the anti-moralizing guardrail).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.core.affect import opinion_injection_detector
from app.core.affect.opinion_injection_detector import (
    DEFAULT_MIN_COSINE,
    DEFAULT_MIN_USER_WORDS,
    OpinionInjectionResult,
    _has_opinion_shape,
    detect,
    render_inner_life_block,
)


# Minimal ``Memory``-shaped stub. The detector only reads ``id``,
# ``content``, and ``embedding`` -- everything else on the real
# ``Memory`` dataclass is irrelevant to the K29 flow.
@dataclass(slots=True)
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray


def _vec(*values: float) -> np.ndarray:
    """Build a unit-normalized 1D vector for cosine math."""
    arr = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return arr
    return arr / norm


# Two canonical reference vectors for tests. Identity vectors keep
# the cosine math obvious; we vary the second-axis to control the
# alignment between user and stance.
_VEC_ALIGNED = _vec(1.0, 0.0, 0.0)
_VEC_AT_THRESHOLD = _vec(0.55, 0.835, 0.0)  # cosine ~0.55 vs aligned
_VEC_DISTANT = _vec(0.0, 1.0, 0.0)  # cosine 0 vs aligned


class OpinionShapeFilterTests(unittest.TestCase):
    """``_has_opinion_shape`` is the first gate against fact-shaped
    self-tags slipping into the contradiction loop."""

    def test_opinion_phrases_match(self) -> None:
        for phrase in (
            "I really don't like horror movies",
            "I prefer cozy stories over thrillers",
            "I love rainy mornings",
            "I hate the smell of smoke",
            "I find networking events exhausting",
            "I'd rather stay in tonight",
            "I'm not a fan of late-night coding",
        ):
            self.assertTrue(
                _has_opinion_shape(phrase),
                f"expected opinion shape: {phrase!r}",
            )

    def test_biographical_facts_dont_match(self) -> None:
        for phrase in (
            "I was born in Tokyo",
            "I live in a small apartment",
            "I work at a startup",
            "I have a cat named Mochi",
        ):
            self.assertFalse(
                _has_opinion_shape(phrase),
                f"unexpected opinion shape: {phrase!r}",
            )

    def test_empty_or_short_input_rejected(self) -> None:
        self.assertFalse(_has_opinion_shape(""))
        self.assertFalse(_has_opinion_shape("  "))
        self.assertFalse(_has_opinion_shape("hi"))


class ShortMessageGateTests(unittest.TestCase):
    def test_short_user_message_returns_none(self) -> None:
        # 2 words, well below default 4 -- K23 territory, not K29.
        result = detect(
            "ok cool",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=1,
                    content="I really don't like smoke",
                    embedding=_VEC_ALIGNED,
                )
            ],
        )
        self.assertIsNone(result)

    def test_empty_user_text_returns_none(self) -> None:
        self.assertIsNone(
            detect(
                "",
                user_vec=_VEC_ALIGNED,
                self_memories=[
                    _StubMemory(
                        id=1,
                        content="I really don't like smoke",
                        embedding=_VEC_ALIGNED,
                    )
                ],
            )
        )

    def test_message_at_min_words_threshold_passes(self) -> None:
        # 4 words -- exactly at default ``min_user_words``. The gate
        # is ">= min", so 4 fires. Pair triggers a definite
        # ``loves/hates`` antonym hit so the test focuses on the
        # length gate alone.
        result = detect(
            "I hate smoking always",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=1,
                    content="I love smoking",
                    embedding=_VEC_ALIGNED,
                )
            ],
        )
        self.assertIsNotNone(result)


class PredicateFilterTests(unittest.TestCase):
    def test_no_opinion_shaped_memories_returns_none(self) -> None:
        # Both candidate memories are biographical facts; predicate
        # filter drops them before cosine math runs.
        result = detect(
            "I love smoking, helps me think",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=1,
                    content="I was born in Tokyo",
                    embedding=_VEC_ALIGNED,
                ),
                _StubMemory(
                    id=2,
                    content="I work at a small studio",
                    embedding=_VEC_ALIGNED,
                ),
            ],
        )
        self.assertIsNone(result)

    def test_mixed_facts_and_opinions_filters_to_opinions(self) -> None:
        # The fact is highest-cosine, but the predicate filter drops
        # it; the opinion survives. The opinion + user message land
        # a clean negation flip (high content overlap, asymmetric
        # ``don't``) so the heuristic fires definite.
        result = detect(
            "I like horror movies a lot",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=1,
                    content="I was born in Tokyo",
                    embedding=_VEC_ALIGNED,
                ),
                _StubMemory(
                    id=2,
                    content="I don't like horror movies",
                    embedding=_VEC_ALIGNED,
                ),
            ],
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.stance_memory_id, 2)


class CosineGateTests(unittest.TestCase):
    def test_below_min_cosine_returns_none(self) -> None:
        # Aligned user, distant stance -> cosine ~0, well below 0.55.
        result = detect(
            "I love smoking, helps me think",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=1,
                    content="I really don't like horror movies",
                    embedding=_VEC_DISTANT,
                )
            ],
        )
        self.assertIsNone(result)

    def test_at_min_cosine_passes(self) -> None:
        # Construct a stance vector that hits the cosine threshold;
        # with content that contradicts on negation flip the result
        # fires. The content_word overlap is deliberately high so
        # the heuristic doesn't fall through to ``no``.
        result = detect(
            "I like smoke a lot",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=1,
                    content="I don't like smoke",
                    embedding=_VEC_AT_THRESHOLD,
                )
            ],
            min_cosine=0.5,
        )
        self.assertIsNotNone(result)


class HeuristicGateTests(unittest.TestCase):
    def test_definite_negation_flip_fires_immediately(self) -> None:
        # The conflict-heuristic flags this as ``definite`` via
        # negation-flip on the user "like" vs stance "don't like".
        # Both sides are short so content-word Jaccard clears the
        # 0.4 threshold cleanly.
        result = detect(
            "I like smoking a lot",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=42,
                    content="I don't like smoking",
                    embedding=_VEC_ALIGNED,
                )
            ],
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "contradiction_definite")
        self.assertEqual(result.stance_memory_id, 42)
        self.assertIsNone(result.llm_verdict)
        # negation_flip should land in the heuristic signals list.
        self.assertTrue(
            any("negation_flip" in sig for sig in result.heuristic_signals),
            f"expected negation_flip signal, got {result.heuristic_signals}",
        )

    def test_definite_antonym_fires_immediately(self) -> None:
        # ``loves`` vs ``hates`` is a definite antonym hit. Note the
        # user side has to also be opinion-shaped enough that the
        # predicate filter doesn't drop the stance from the candidate
        # set -- the stance "I hate horror" passes the filter via
        # the explicit "i hate" pattern.
        result = detect(
            "I love horror movies a lot",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=7,
                    content="I hate horror movies",
                    embedding=_VEC_ALIGNED,
                )
            ],
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "contradiction_definite")

    def test_borderline_with_llm_yes_fires(self) -> None:
        """Borderline heuristic + LLM YES path."""
        # Construct a pair with no negation flip and no antonym, but
        # with a numerical mismatch on the same topic -- that lands
        # the heuristic as ``borderline`` (see conflict_heuristics
        # ``_numerical_mismatch``).
        calls: list[tuple[str, str]] = []

        def fake_llm_gate(user_t: str, stance_t: str) -> str:
            calls.append((user_t, stance_t))
            return "YES"

        result = detect(
            "I've been jogging 8 kilometres every morning for years",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=11,
                    content="I prefer jogging 4 kilometres every morning",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=fake_llm_gate,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "contradiction_borderline")
        self.assertEqual(result.llm_verdict, "YES")
        self.assertEqual(len(calls), 1)

    def test_borderline_with_llm_no_silent(self) -> None:
        def llm_no(user_t: str, stance_t: str) -> str:
            return "NO"

        result = detect(
            "I've been jogging 8 kilometres every morning for years",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=11,
                    content="I prefer jogging 4 kilometres every morning",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=llm_no,
        )
        self.assertIsNone(result)

    def test_borderline_without_llm_gate_silent(self) -> None:
        # No llm_gate at all (Path C / minimal config) -- borderline
        # results stay silent rather than slipping into a hard fire.
        result = detect(
            "I've been jogging 8 kilometres every morning for years",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=11,
                    content="I prefer jogging 4 kilometres every morning",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=None,
        )
        self.assertIsNone(result)

    def test_defer_borderline_returns_pending(self) -> None:
        # P21: with defer_borderline the borderline candidate comes back
        # as a PENDING result (no llm_gate call) so the caller can run
        # the verdict off the hot path.
        gate_calls: list[Any] = []

        def gate(user_t: str, stance_t: str) -> str:
            gate_calls.append((user_t, stance_t))
            return "YES"

        result = detect(
            "I've been jogging 8 kilometres every morning for years",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=11,
                    content="I prefer jogging 4 kilometres every morning",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=gate,
            defer_borderline=True,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "contradiction_borderline")
        self.assertEqual(result.llm_verdict, "PENDING")
        self.assertEqual(result.stance_memory_id, 11)
        # The gate must NOT have been invoked on the hot path.
        self.assertEqual(gate_calls, [])

    def test_defer_borderline_definite_still_fires_inline(self) -> None:
        # A definite contradiction never needed the LLM, so it fires
        # immediately even under defer_borderline.
        result = detect(
            "I like horror movies a lot",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=7,
                    content="I don't like horror movies",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=None,
            defer_borderline=True,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "contradiction_definite")
        self.assertIsNone(result.llm_verdict)

    def test_require_definite_overrides_defer(self) -> None:
        # require_definite wins: borderline stays silent even with
        # defer_borderline set.
        result = detect(
            "I've been jogging 8 kilometres every morning for years",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=11,
                    content="I prefer jogging 4 kilometres every morning",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=None,
            defer_borderline=True,
            require_definite=True,
        )
        self.assertIsNone(result)

    def test_require_definite_skips_llm_gate(self) -> None:
        # ``require_definite=True`` is the strictest config -- even
        # if an llm_gate IS provided, borderline results are
        # silently dropped without spending an LLM call.
        calls: list[Any] = []

        def fake_gate(*args: Any, **kwargs: Any) -> str:
            calls.append(args)
            return "YES"

        result = detect(
            "I've been jogging 8 kilometres every morning for years",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=11,
                    content="I prefer jogging 4 kilometres every morning",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=fake_gate,
            require_definite=True,
        )
        self.assertIsNone(result)
        # LLM gate must NOT have been called -- this is the
        # zero-LLM-cost guarantee of require_definite.
        self.assertEqual(calls, [])

    def test_llm_gate_raising_returns_none(self) -> None:
        # A misbehaving llm_gate must never crash the turn; the
        # detector swallows + logs and returns None.
        def crash_gate(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("ollama crashed")

        result = detect(
            "I've been jogging 8 kilometres every morning for years",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=11,
                    content="I prefer jogging 4 kilometres every morning",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=crash_gate,
        )
        self.assertIsNone(result)

    def test_high_cosine_no_heuristic_with_llm_yes_fires(self) -> None:
        """The verbose-stance path: heuristic returns ``no`` because
        Jaccard overlap is diluted by descriptive context, but the
        LLM gate confirms the contradiction. This is exactly the
        canonical smoking scenario where Aiko has a long stored
        stance like "I really don't like smoking, it gives me a
        headache" and the user says "I like smoking, it helps me
        think clearly" -- short enough content-word overlap that
        the conservative heuristic drops it, but a real
        contradiction the LLM should catch.
        """
        calls: list[tuple[str, str]] = []

        def fake_llm_yes(user_t: str, stance_t: str) -> str:
            calls.append((user_t, stance_t))
            return "YES"

        result = detect(
            "I like smoking, it helps me think clearly",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=99,
                    content="I really don't like smoking, it gives me a headache",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=fake_llm_yes,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.trigger, "contradiction_borderline")
        self.assertEqual(result.llm_verdict, "YES")
        self.assertEqual(result.heuristic_label, "no")
        # LLM gate must have been called exactly once.
        self.assertEqual(len(calls), 1)

    def test_high_cosine_no_heuristic_with_llm_no_silent(self) -> None:
        """Mirror of the verbose-stance test with LLM saying NO --
        even though cosine is high and there's no obvious heuristic
        signal, the LLM's strict "prefer NO when uncertain" bias is
        what keeps Aiko from being contrarian on borderline cases.
        """
        def llm_no(user_t: str, stance_t: str) -> str:
            return "NO"

        result = detect(
            "I like smoking, it helps me think clearly",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=99,
                    content="I really don't like smoking, it gives me a headache",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=llm_no,
        )
        self.assertIsNone(result)

    def test_high_cosine_no_heuristic_without_llm_silent(self) -> None:
        """Without an LLM gate, the verbose-stance path stays silent --
        we never fire on a ``no`` heuristic without LLM confirmation."""
        result = detect(
            "I like smoking, it helps me think clearly",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=99,
                    content="I really don't like smoking, it gives me a headache",
                    embedding=_VEC_ALIGNED,
                )
            ],
            llm_gate=None,
        )
        self.assertIsNone(result)


class NullInputTests(unittest.TestCase):
    def test_user_vec_none_returns_none(self) -> None:
        self.assertIsNone(
            detect(
                "I like smoking, helps me think clearly",
                user_vec=None,
                self_memories=[
                    _StubMemory(
                        id=1,
                        content="I really don't like smoking",
                        embedding=_VEC_ALIGNED,
                    )
                ],
            )
        )

    def test_empty_memory_set_returns_none(self) -> None:
        self.assertIsNone(
            detect(
                "I like smoking, helps me think clearly",
                user_vec=_VEC_ALIGNED,
                self_memories=[],
            )
        )

    def test_unrelated_topics_return_none(self) -> None:
        # Cosine high (aligned vectors) but heuristic finds no
        # contradiction signal (no negation flip, no antonym, no
        # numerical mismatch on the same anchor) -> "no" verdict.
        result = detect(
            "I'm thinking about what to cook tonight for dinner",
            user_vec=_VEC_ALIGNED,
            self_memories=[
                _StubMemory(
                    id=1,
                    content="I really like spending evenings reading novels",
                    embedding=_VEC_ALIGNED,
                )
            ],
        )
        self.assertIsNone(result)


class RenderTests(unittest.TestCase):
    @staticmethod
    def _make_result(stance_text: str = "I really don't like smoke") -> OpinionInjectionResult:
        return OpinionInjectionResult(
            trigger="contradiction_definite",
            stance_text=stance_text,
            stance_memory_id=42,
            cosine=0.78,
            heuristic_label="definite",
            heuristic_signals=["negation_flip"],
            llm_verdict=None,
        )

    def test_render_contains_user_display_name(self) -> None:
        text = render_inner_life_block(
            self._make_result(), user_display_name="Jacob",
        )
        self.assertIn("Jacob", text)

    def test_render_uses_default_name_when_omitted(self) -> None:
        text = render_inner_life_block(self._make_result())
        self.assertIn("the user", text)

    def test_render_quotes_stance_for_aikos_reading(self) -> None:
        # The stance text is quoted in the cue so the LLM can read
        # Aiko's prior take. The persona block forbids quoting it
        # back at Jacob, but the cue itself must include it.
        text = render_inner_life_block(
            self._make_result("I really don't like smoke"),
        )
        self.assertIn("I really don't like smoke", text)

    def test_render_long_stance_is_truncated(self) -> None:
        long_stance = "I really prefer " + ("cozy stories " * 30)
        text = render_inner_life_block(self._make_result(long_stance))
        # Truncation marker should appear so the cue stays compact.
        self.assertIn("\u2026", text)

    def test_render_steers_toward_owning_taste_not_lecturing(self) -> None:
        # The cue's body MUST tell Aiko to share her preference,
        # not prescribe his behaviour. Anti-moralizing guardrail.
        text = render_inner_life_block(self._make_result()).lower()
        self.assertIn("preference", text)
        self.assertIn("don't lecture", text)
        # Common failure mode the cue explicitly steers AWAY from:
        # the persona block has the longer treatment, but the cue
        # itself should at minimum carry the anti-apology rail
        # and the per-turn cap of "one line".
        self.assertIn("one line", text)


class PublicSurfaceTests(unittest.TestCase):
    def test_defaults_are_reasonable(self) -> None:
        self.assertGreater(DEFAULT_MIN_COSINE, 0.0)
        self.assertLessEqual(DEFAULT_MIN_COSINE, 1.0)
        self.assertGreater(DEFAULT_MIN_USER_WORDS, 0)

    def test_module_exports_detect(self) -> None:
        self.assertTrue(callable(opinion_injection_detector.detect))
        self.assertTrue(
            callable(opinion_injection_detector.render_inner_life_block)
        )


if __name__ == "__main__":
    unittest.main()
