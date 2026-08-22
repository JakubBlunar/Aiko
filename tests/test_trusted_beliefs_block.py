"""K2: corroborated beliefs as standing context.

The counterpart to ``belief_gaps_block``. Before it existed a belief
could reach the prompt only by being *wrong* -- measured on the live
install, the gap block had rendered on 3 turns out of 851, so a store of
correct reads about the user changed nothing about how Aiko spoke.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.relationship.belief_store import (
    BeliefStore,
    KIND_MOOD,
    KIND_OPINION,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


class _Agent:
    belief_tracking_enabled = True
    belief_trusted_block_enabled = True
    belief_trusted_block_max = 4


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self, store: BeliefStore) -> None:
        self._settings = _Settings()
        self._belief_store = store
        self._user_id = "u1"

    @property
    def user_display_name(self) -> str:
        return "Jacob"


def _build() -> tuple[_Host, BeliefStore]:
    path = Path(tempfile.mkdtemp()) / "test.db"
    store = BeliefStore(ChatDatabase(path))
    return _Host(store), store


def _confirm(store: BeliefStore, **kwargs) -> None:
    """Write a belief and corroborate it the way the worker would."""
    store.upsert(user_id="u1", **kwargs)
    store.upsert(user_id="u1", **kwargs)


class TrustedBeliefsBlockTests(unittest.TestCase):
    def test_empty_store_renders_nothing(self) -> None:
        host, _ = _build()
        self.assertEqual(host._render_trusted_beliefs_block(), "")

    def test_uncorroborated_belief_is_not_spoken(self) -> None:
        """A single extraction is a guess, not something to assert."""
        host, store = _build()
        store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="rust",
            predicted_state="overhyped",
        )
        self.assertEqual(host._render_trusted_beliefs_block(), "")

    def test_corroborated_belief_reaches_the_prompt(self) -> None:
        host, store = _build()
        _confirm(
            store, kind=KIND_OPINION, topic="rust",
            predicted_state="overhyped",
        )
        out = host._render_trusted_beliefs_block()
        self.assertIn("rust is overhyped", out)
        self.assertIn("Jacob", out)

    def test_mood_and_opinion_read_in_their_own_frame(self) -> None:
        host, store = _build()
        _confirm(
            store, kind=KIND_MOOD, topic="the tokyo trip",
            predicted_state="quietly excited",
        )
        _confirm(
            store, kind=KIND_OPINION, topic="open-plan offices",
            predicted_state="a mistake",
        )
        out = host._render_trusted_beliefs_block()
        self.assertIn("Jacob is quietly excited about the tokyo trip", out)
        self.assertIn("open-plan offices is a mistake, to him", out)

    def test_block_tells_her_not_to_recite_it(self) -> None:
        """It is background she speaks from, not a list to bring up.

        Without the guard this reads as a checklist and she performs
        insight at the user instead of just knowing things.
        """
        host, store = _build()
        _confirm(
            store, kind=KIND_OPINION, topic="rust",
            predicted_state="overhyped",
        )
        out = host._render_trusted_beliefs_block()
        self.assertIn("not a list to bring up", out)
        self.assertIn("drop any of it the moment he says otherwise", out)

    def test_respects_the_line_cap(self) -> None:
        host, store = _build()
        for n in range(8):
            _confirm(
                store, kind=KIND_OPINION, topic=f"topic {n}",
                predicted_state="fine",
            )
        host._settings.agent.belief_trusted_block_max = 3
        out = host._render_trusted_beliefs_block()
        self.assertEqual(out.count("\n- "), 3)

    def test_toggles_off(self) -> None:
        host, store = _build()
        _confirm(
            store, kind=KIND_OPINION, topic="rust",
            predicted_state="overhyped",
        )
        host._settings.agent.belief_trusted_block_enabled = False
        self.assertEqual(host._render_trusted_beliefs_block(), "")
        host._settings.agent.belief_trusted_block_enabled = True
        host._settings.agent.belief_tracking_enabled = False
        self.assertEqual(host._render_trusted_beliefs_block(), "")
        host._settings.agent.belief_tracking_enabled = True
        host._settings.agent.belief_trusted_block_max = 0
        self.assertEqual(host._render_trusted_beliefs_block(), "")

    def test_contradicted_belief_stops_being_spoken(self) -> None:
        host, store = _build()
        _confirm(
            store, kind=KIND_OPINION, topic="rust",
            predicted_state="overhyped",
        )
        self.assertIn("rust", host._render_trusted_beliefs_block())
        row = store.list_trusted(user_id="u1")[0]
        store.mark_contradicted(row.id)
        self.assertEqual(host._render_trusted_beliefs_block(), "")

    def test_store_failure_is_not_fatal(self) -> None:
        host, _ = _build()

        class _Broken:
            def list_trusted(self, **_kwargs):
                raise RuntimeError("boom")

        host._belief_store = _Broken()
        self.assertEqual(host._render_trusted_beliefs_block(), "")


if __name__ == "__main__":
    unittest.main()
