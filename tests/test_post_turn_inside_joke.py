"""Wiring tests for K80 -- inside-joke birth.

The detector itself (echo + amusement, phrase choice, blessing writes)
is covered in ``test_catchphrase_miner``. What's exercised here is the
seam between it and the session: the post-turn helper
:meth:`PostTurnHelpersMixin._maybe_bless_inside_joke` -- master switch,
watermark, reaction lookup, slot arming -- and the one-shot consumption
contract on the provider side
(:meth:`InnerLifePart4Mixin._render_inside_joke_block`).

Both are tested against minimal mixin hosts; the real
:class:`SessionController` is far too heavy to stand up for a gate
matrix.
"""
from __future__ import annotations

import unittest
from collections import deque
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from app.core.infra import timephrase
from app.core.memory.catchphrase_miner import InsideJokeBirth
from app.core.session.inner_life_part4 import InnerLifePart4Mixin
from app.core.session.post_turn_helpers_mixin import (
    _KV_INSIDE_JOKE_AT,
    PostTurnHelpersMixin,
)

_ORIGIN = "honestly it's just a fish-shaped cookie situation at this point"
_ECHO = "lmao a fish-shaped cookie situation, exactly"


class _Kv:
    """``ChatDatabase``-shaped stub with just the kv pair we touch."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.store = dict(initial or {})

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


class _MemoryStore:
    def __init__(self, top: list[Any] | None = None) -> None:
        self.top = list(top or [])
        self.added: list[dict[str, Any]] = []

    def iter_by_kind(self, kind: str) -> list[Any]:
        # P33: the dedupe guard now asks the store for the complete
        # catchphrase set instead of post-filtering an unfiltered top-N.
        return [m for m in self.top if m.kind == kind]

    def add(self, **kwargs: Any) -> Any:
        self.added.append(kwargs)
        return SimpleNamespace(id=len(self.added))


class _Moments:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add(self, **kwargs: Any) -> Any:
        self.added.append(kwargs)
        return SimpleNamespace(id=len(self.added))


def _agent(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        inside_joke_birth_enabled=True,
        inside_joke_birth_cooldown_hours=24.0,
        inside_joke_birth_min_words=3,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Host(PostTurnHelpersMixin):
    """Minimal host exposing what the K80 helper reads."""

    def __init__(
        self,
        *,
        agent: SimpleNamespace | None = None,
        origins: list[tuple[int | None, str]] | None = None,
        reactions: dict[int, dict[str, int]] | None = None,
        kv: _Kv | None = None,
        catchphrases: list[str] | None = None,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent or _agent())
        self._recent_assistant_turns = deque(
            origins if origins is not None else [(7, _ORIGIN)],
        )
        self._reactions = (
            {7: {"laugh": 1}} if reactions is None else reactions
        )
        self._chat_db = kv if kv is not None else _Kv()
        self._memory_store = _MemoryStore(
            [
                SimpleNamespace(kind="catchphrase", content=p)
                for p in (catchphrases or [])
            ],
        )
        self._embedder = SimpleNamespace(embed=lambda text: [0.1, 0.2])
        self._shared_moments_store = _Moments()
        self.session_key = "s1"
        self._pending_inside_joke: Any = None

    def _load_message_reactions(self, message_id: int) -> dict[str, int]:
        return self._reactions.get(int(message_id), {})


class _Provider(InnerLifePart4Mixin):
    def __init__(self, birth: Any, *, enabled: bool = True) -> None:
        self._settings = SimpleNamespace(
            agent=_agent(inside_joke_birth_enabled=enabled),
        )
        self._pending_inside_joke = birth
        self.user_display_name = "Sam"


# ── Detection seam ─────────────────────────────────────────────────────


class BlessTests(unittest.TestCase):
    def test_laughed_echo_arms_the_slot_and_writes(self) -> None:
        host = _Host()
        host._maybe_bless_inside_joke(user_text=_ECHO, user_message_id=8)
        birth = host._pending_inside_joke
        self.assertIsNotNone(birth)
        self.assertIn("fish-shaped cookie", birth.phrase)
        kinds = [m["kind"] for m in host._memory_store.added]
        self.assertEqual(kinds, ["catchphrase"])
        self.assertEqual(
            [m["vibe"] for m in host._shared_moments_store.added], ["playful"],
        )

    def test_disabled_does_nothing(self) -> None:
        host = _Host(agent=_agent(inside_joke_birth_enabled=False))
        host._maybe_bless_inside_joke(user_text=_ECHO, user_message_id=8)
        self.assertIsNone(host._pending_inside_joke)
        self.assertEqual(host._memory_store.added, [])

    def test_flat_echo_without_amusement_is_not_a_birth(self) -> None:
        """Repetition alone is how conversations work, not a joke."""
        host = _Host(reactions={})
        host._maybe_bless_inside_joke(
            user_text="a fish-shaped cookie situation, sure",
            user_message_id=8,
        )
        self.assertIsNone(host._pending_inside_joke)

    def test_no_recent_assistant_turns_is_a_no_op(self) -> None:
        host = _Host(origins=[])
        host._maybe_bless_inside_joke(user_text=_ECHO, user_message_id=8)
        self.assertIsNone(host._pending_inside_joke)

    def test_recent_watermark_suppresses(self) -> None:
        recent = (timephrase.utcnow() - timedelta(hours=2)).isoformat()
        host = _Host(kv=_Kv({_KV_INSIDE_JOKE_AT: recent}))
        host._maybe_bless_inside_joke(user_text=_ECHO, user_message_id=8)
        self.assertIsNone(host._pending_inside_joke)

    def test_stale_watermark_allows_and_is_refreshed(self) -> None:
        stale = (timephrase.utcnow() - timedelta(days=9)).isoformat()
        kv = _Kv({_KV_INSIDE_JOKE_AT: stale})
        host = _Host(kv=kv)
        host._maybe_bless_inside_joke(user_text=_ECHO, user_message_id=8)
        self.assertIsNotNone(host._pending_inside_joke)
        self.assertNotEqual(kv.store[_KV_INSIDE_JOKE_AT], stale)

    def test_already_known_phrase_is_skipped(self) -> None:
        """A bit can only be born once; reuse is K22's job."""
        host = _Host(catchphrases=["fish-shaped cookie situation"])
        host._maybe_bless_inside_joke(user_text=_ECHO, user_message_id=8)
        self.assertIsNone(host._pending_inside_joke)


# ── Provider seam ──────────────────────────────────────────────────────


class RenderTests(unittest.TestCase):
    def _birth(self) -> InsideJokeBirth:
        return InsideJokeBirth(
            phrase="fish-shaped cookie situation",
            origin_message_id=7,
            lag_turns=0,
            laughed=True,
            amused=False,
        )

    def test_renders_once_then_clears(self) -> None:
        provider = _Provider(self._birth())
        first = provider._render_inside_joke_block()
        self.assertIn("fish-shaped cookie situation", first)
        self.assertIn("Sam", first)
        self.assertEqual(provider._render_inside_joke_block(), "")

    def test_empty_slot_renders_nothing(self) -> None:
        self.assertEqual(_Provider(None)._render_inside_joke_block(), "")

    def test_disabled_leaves_the_slot_alone(self) -> None:
        provider = _Provider(self._birth(), enabled=False)
        self.assertEqual(provider._render_inside_joke_block(), "")
        self.assertIsNotNone(provider._pending_inside_joke)


if __name__ == "__main__":
    unittest.main()
