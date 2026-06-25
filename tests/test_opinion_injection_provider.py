"""Controller-level tests for the K29 opinion-injection provider.

Exercises
:meth:`InnerLifeProvidersMixin._render_opinion_injection_block` by
building a minimal stub that simulates the controller surface it
reads from. Avoids spinning up the full
:class:`SessionController` which would import half the world.

The detector itself is covered in
``tests/test_opinion_injection_detector.py``; this module focuses on
the provider plumbing -- cooldown decrement / arming, per-session
cap, master-switch gate, force-next bypass, and the memory_store
+ embedder dependency surface.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


@dataclass(slots=True)
class _StubMemory:
    """Memory-shaped stub matching the fields the detector reads."""

    id: int
    content: str
    embedding: np.ndarray


class _FakeMemoryStore:
    def __init__(self, rows: list[_StubMemory]) -> None:
        self._rows = rows

    def iter_by_kind(self, kind: str) -> list[_StubMemory]:
        if kind != "self":
            return []
        return list(self._rows)


class _FakeEmbedder:
    def __init__(self, vec: np.ndarray | None) -> None:
        self._vec = vec
        self.calls = 0

    def embed(self, text: str) -> np.ndarray:
        self.calls += 1
        if self._vec is None:
            raise RuntimeError("embedder unavailable")
        return self._vec


def _vec(*values: float) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


_VEC_ALIGNED = _vec(1.0, 0.0, 0.0)


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        opinion_injection_enabled=True,
        opinion_injection_require_definite=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        opinion_injection_min_cosine=0.55,
        opinion_injection_min_user_words=4,
        opinion_injection_cooldown_turns=5,
        opinion_injection_per_session_cap=3,
        opinion_injection_per_hour_cap=6,
        opinion_injection_per_day_cap=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    """Minimal mixin host with the attributes the provider reads."""

    def __init__(
        self,
        *,
        memories: list[_StubMemory] | None = None,
        embedder_vec: np.ndarray | None = _VEC_ALIGNED,
        cooldown: int = 0,
        session_count: int = 0,
        force_next: bool = False,
        agent_settings: SimpleNamespace | None = None,
        memory_settings: SimpleNamespace | None = None,
        rate_limiter: Any = None,
        ollama: Any = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._memory_store = _FakeMemoryStore(memories or [])
        self._embedder = _FakeEmbedder(embedder_vec)
        self._ollama = ollama  # P21: non-None enables the deferred path
        self._effective_chat_model = "stub-model"
        self._effective_worker_model = "stub-worker-model"
        self._fact_check_cancel = None
        self._opinion_injection_cooldown = cooldown
        self._opinion_injection_session_count = session_count
        self._opinion_injection_force_next = force_next
        self._last_opinion_injection: Any = None
        self._opinion_injection_rate_limiter = rate_limiter
        self._opinion_injection_pending_borderline: Any = None
        self._opinion_injection_pending_cue: Any = None
        self.user_display_name = "Jacob"

    # No-op so the K59 tease-bank call in the resolver doesn't error.
    def _bank_tease_debt(self, **_kwargs: Any) -> None:
        return None


def _contradicting_stance(memory_id: int = 1) -> _StubMemory:
    """Stance memory that triggers a definite negation-flip vs the
    user message ``"I like horror movies a lot"``."""
    return _StubMemory(
        id=memory_id,
        content="I don't like horror movies",
        embedding=_VEC_ALIGNED,
    )


CONTRADICTING_USER_MSG = "I like horror movies a lot"


# A user message that does NOT contradict any opinion stance (just
# a neutral observation). Used for cooldown-decrement tests so the
# decrement is the only observable change.
NON_FIRING_USER_MSG = (
    "I'm thinking about what to cook tonight for the family dinner"
)


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty_without_touching_cooldown(self) -> None:
        # When the master switch is off, the provider must short-
        # circuit BEFORE the cooldown decrement so an off switch
        # doesn't quietly drain any pending cooldown.
        agent = _make_agent_settings(opinion_injection_enabled=False)
        host = _Host(
            memories=[_contradicting_stance()],
            cooldown=2,
            agent_settings=agent,
        )
        self.assertEqual(
            host._render_opinion_injection_block(CONTRADICTING_USER_MSG),
            "",
        )
        self.assertEqual(host._opinion_injection_cooldown, 2)
        self.assertIsNone(host._last_opinion_injection)


class FirePathTests(unittest.TestCase):
    def test_fires_on_definite_contradiction(self) -> None:
        host = _Host(memories=[_contradicting_stance(memory_id=42)])
        block = host._render_opinion_injection_block(CONTRADICTING_USER_MSG)
        self.assertNotEqual(block, "")
        self.assertIn("Jacob", block)
        # Cooldown armed to configured 5 turns.
        self.assertEqual(host._opinion_injection_cooldown, 5)
        # Session count bumped by one.
        self.assertEqual(host._opinion_injection_session_count, 1)
        # last_opinion_injection stashed for the MCP debug tool.
        last = host._last_opinion_injection
        self.assertIsNotNone(last)
        self.assertEqual(last.trigger, "contradiction_definite")
        self.assertEqual(last.stance_memory_id, 42)

    def test_silent_on_unrelated_message(self) -> None:
        host = _Host(memories=[_contradicting_stance()])
        block = host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(block, "")
        self.assertEqual(host._opinion_injection_cooldown, 0)
        self.assertEqual(host._opinion_injection_session_count, 0)
        self.assertIsNone(host._last_opinion_injection)


class CooldownPlumbingTests(unittest.TestCase):
    def test_cooldown_decrements_each_call(self) -> None:
        # NON_FIRING_USER_MSG ensures the detector never fires so the
        # decrement is the only observable behaviour.
        host = _Host(memories=[_contradicting_stance()], cooldown=3)
        host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(host._opinion_injection_cooldown, 2)
        host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(host._opinion_injection_cooldown, 1)
        host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(host._opinion_injection_cooldown, 0)
        # Floor at 0 -- further non-firing calls stay at 0.
        host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(host._opinion_injection_cooldown, 0)

    def test_cooldown_blocks_fire(self) -> None:
        # Trigger conditions satisfied BUT cooldown > 0 -> no cue.
        host = _Host(memories=[_contradicting_stance()], cooldown=2)
        block = host._render_opinion_injection_block(CONTRADICTING_USER_MSG)
        self.assertEqual(block, "")
        # Cooldown decremented by 1; no re-arm because we didn't fire.
        self.assertEqual(host._opinion_injection_cooldown, 1)
        self.assertEqual(host._opinion_injection_session_count, 0)


class SessionCapTests(unittest.TestCase):
    def test_cap_blocks_after_threshold(self) -> None:
        # session_count already at cap (3) -> next contradicting
        # message silently suppresses the cue.
        host = _Host(
            memories=[_contradicting_stance()],
            session_count=3,
        )
        block = host._render_opinion_injection_block(CONTRADICTING_USER_MSG)
        self.assertEqual(block, "")
        # Cap-blocked path must NOT bump the session count further.
        self.assertEqual(host._opinion_injection_session_count, 3)

    def test_cap_zero_means_disabled(self) -> None:
        # Per-session cap of 0 means K29 fires unboundedly per session
        # (operator override). The provider must NOT silently
        # suppress in that case.
        mem_settings = _make_memory_settings(
            opinion_injection_per_session_cap=0,
        )
        host = _Host(
            memories=[_contradicting_stance()],
            session_count=999,
            memory_settings=mem_settings,
        )
        block = host._render_opinion_injection_block(CONTRADICTING_USER_MSG)
        self.assertNotEqual(block, "")

    def test_cap_just_under_threshold_still_fires(self) -> None:
        host = _Host(
            memories=[_contradicting_stance()],
            session_count=2,  # cap=3, allow=2 more
        )
        block = host._render_opinion_injection_block(CONTRADICTING_USER_MSG)
        self.assertNotEqual(block, "")
        self.assertEqual(host._opinion_injection_session_count, 3)


class ForceNextTests(unittest.TestCase):
    def test_force_next_bypasses_cooldown(self) -> None:
        host = _Host(
            memories=[_contradicting_stance()],
            cooldown=4,
            force_next=True,
        )
        block = host._render_opinion_injection_block(CONTRADICTING_USER_MSG)
        self.assertNotEqual(block, "")
        # Cooldown re-armed to configured value after fire.
        self.assertEqual(host._opinion_injection_cooldown, 5)
        # Force flag consumed (one-shot semantics).
        self.assertFalse(host._opinion_injection_force_next)

    def test_force_next_bypasses_session_cap(self) -> None:
        host = _Host(
            memories=[_contradicting_stance()],
            session_count=99,  # well above cap=3
            force_next=True,
        )
        block = host._render_opinion_injection_block(CONTRADICTING_USER_MSG)
        self.assertNotEqual(block, "")
        # Session count still bumped (the bypass is for the GATE,
        # not for the counter -- accurate debug telemetry).
        self.assertEqual(host._opinion_injection_session_count, 100)
        self.assertFalse(host._opinion_injection_force_next)

    def test_force_next_consumed_when_trigger_misses(self) -> None:
        # Force-next on an unrelated message -- the flag must still
        # be consumed (one-turn semantics) even though no cue fires.
        host = _Host(
            memories=[_contradicting_stance()],
            cooldown=2,
            force_next=True,
        )
        block = host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(block, "")
        self.assertFalse(host._opinion_injection_force_next)


class DependencySurfaceTests(unittest.TestCase):
    def test_no_memory_store_returns_empty(self) -> None:
        host = _Host(memories=[_contradicting_stance()])
        host._memory_store = None
        self.assertEqual(
            host._render_opinion_injection_block(CONTRADICTING_USER_MSG),
            "",
        )

    def test_no_embedder_returns_empty(self) -> None:
        host = _Host(memories=[_contradicting_stance()])
        host._embedder = None
        self.assertEqual(
            host._render_opinion_injection_block(CONTRADICTING_USER_MSG),
            "",
        )

    def test_empty_self_memories_returns_empty(self) -> None:
        # No stored stance to contradict -> no fire.
        host = _Host(memories=[])
        self.assertEqual(
            host._render_opinion_injection_block(CONTRADICTING_USER_MSG),
            "",
        )

    def test_embedder_failure_returns_empty(self) -> None:
        # embedder.embed raising must not crash the turn.
        host = _Host(
            memories=[_contradicting_stance()],
            embedder_vec=None,  # _FakeEmbedder raises on None
        )
        self.assertEqual(
            host._render_opinion_injection_block(CONTRADICTING_USER_MSG),
            "",
        )


class _AllowRateLimiter:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.calls = 0

    def allow(self) -> bool:
        self.calls += 1
        return self._allow


def _borderline_stance(memory_id: int = 11) -> _StubMemory:
    """Opinion-shaped stance that classifies as borderline (numerical
    mismatch) against ``BORDERLINE_USER_MSG`` -- not a definite flip."""
    return _StubMemory(
        id=memory_id,
        content="I prefer jogging 4 kilometres every morning",
        embedding=_VEC_ALIGNED,
    )


BORDERLINE_USER_MSG = "I've been jogging 8 kilometres every morning for years"


class DeferredBorderlineTests(unittest.TestCase):
    """P21: borderline verdict deferred off the hot path."""

    def _host(self, **kw: Any) -> _Host:
        kw.setdefault("rate_limiter", _AllowRateLimiter(allow=True))
        return _Host(
            memories=[_borderline_stance()],
            ollama=object(),
            **kw,
        )

    def test_borderline_arms_pending_not_cooldown(self) -> None:
        host = self._host()
        block = host._render_opinion_injection_block(BORDERLINE_USER_MSG)
        # No cue on the hot path; nothing armed yet.
        self.assertEqual(block, "")
        self.assertEqual(host._opinion_injection_cooldown, 0)
        self.assertEqual(host._opinion_injection_session_count, 0)
        # The candidate is stashed for the post-turn resolver.
        pending = host._opinion_injection_pending_borderline
        self.assertIsNotNone(pending)
        self.assertEqual(pending["stance_memory_id"], 11)
        self.assertEqual(pending["user_text"], BORDERLINE_USER_MSG)

    def test_no_ollama_stays_definite_only(self) -> None:
        # Without a worker client there's no way to resolve the verdict,
        # so the provider must not arm a pending borderline.
        host = _Host(
            memories=[_borderline_stance()],
            rate_limiter=_AllowRateLimiter(allow=True),
            ollama=None,
        )
        block = host._render_opinion_injection_block(BORDERLINE_USER_MSG)
        self.assertEqual(block, "")
        self.assertIsNone(host._opinion_injection_pending_borderline)

    def test_resolver_yes_arms_cue_and_cooldown(self) -> None:
        host = self._host()
        host._render_opinion_injection_block(BORDERLINE_USER_MSG)
        host._opinion_injection_llm_verdict = lambda u, s: "YES"  # type: ignore[assignment]
        host._resolve_opinion_injection_pending()
        # Pending consumed; cue armed for the next turn.
        self.assertIsNone(host._opinion_injection_pending_borderline)
        self.assertTrue(host._opinion_injection_pending_cue)
        self.assertIn("Jacob", host._opinion_injection_pending_cue)
        # Cooldown + session count arm only now (on confirmed fire).
        self.assertEqual(host._opinion_injection_cooldown, 5)
        self.assertEqual(host._opinion_injection_session_count, 1)

    def test_resolver_no_drops_silently(self) -> None:
        host = self._host()
        host._render_opinion_injection_block(BORDERLINE_USER_MSG)
        host._opinion_injection_llm_verdict = lambda u, s: "NO"  # type: ignore[assignment]
        host._resolve_opinion_injection_pending()
        self.assertIsNone(host._opinion_injection_pending_borderline)
        self.assertIsNone(host._opinion_injection_pending_cue)
        self.assertEqual(host._opinion_injection_cooldown, 0)
        self.assertEqual(host._opinion_injection_session_count, 0)

    def test_pending_cue_renders_next_turn_one_shot(self) -> None:
        host = self._host()
        host._opinion_injection_pending_cue = "Heads-up: deferred cue."
        # Next turn: the cue renders and clears, bypassing detection.
        block = host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(block, "Heads-up: deferred cue.")
        self.assertIsNone(host._opinion_injection_pending_cue)
        # Second turn: nothing left to render.
        block2 = host._render_opinion_injection_block(NON_FIRING_USER_MSG)
        self.assertEqual(block2, "")

    def test_resolver_rate_limited_drops(self) -> None:
        host = self._host(rate_limiter=_AllowRateLimiter(allow=False))
        host._render_opinion_injection_block(BORDERLINE_USER_MSG)
        # Even with a YES verdict available, the limiter blocks the spend.
        host._opinion_injection_llm_verdict = lambda u, s: "YES"  # type: ignore[assignment]
        host._resolve_opinion_injection_pending()
        self.assertIsNone(host._opinion_injection_pending_cue)
        self.assertEqual(host._opinion_injection_cooldown, 0)

    def test_resolver_no_pending_is_noop(self) -> None:
        host = self._host()
        # Nothing armed -> resolver does nothing, no crash.
        host._resolve_opinion_injection_pending()
        self.assertIsNone(host._opinion_injection_pending_cue)


if __name__ == "__main__":
    unittest.main()
