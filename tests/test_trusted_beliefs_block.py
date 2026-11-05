"""K2 readings are useful while tentative, relevant and within their situation."""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.relationship.belief_store import BeliefStore
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


class _Agent:
    belief_tracking_enabled = True
    belief_trusted_block_enabled = True
    belief_trusted_block_max = 4


class _Settings:
    def __init__(self):
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    session_key = "u1:session-1"
    user_display_name = "Sample User"

    def __init__(self, store):
        self._settings = _Settings()
        self._belief_store = store
        self._user_id = "u1"


def _build():
    store = BeliefStore(ChatDatabase(Path(tempfile.mkdtemp()) / "test.db"))
    return _Host(store), store


def _reading(store, *, topic="quiet company", **changes):
    now = timephrase.utcnow()
    meta = {
        "interpretation_version": 2, "scope": "exchange", "session_key": "u1:session-1",
        "reason": "They asked Aiko to stay while describing tiredness.",
        "situation": "Winding down with quiet company.",
        "uncertainty": "Tiredness does not specify how much conversation they want.",
        "revise_when": "They request a different subject or active help.",
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "evidence": [{"message_id": 1, "quote": "I am tired, but stay with me."}],
    }
    meta.update(changes)
    return store.upsert(user_id="u1", kind="mood", topic=topic,
                        predicted_state=("They may welcome company without "
                                         "a demanding conversation."),
                        source="worker", source_message_id=1, metadata=meta)


class BeliefContextTests(unittest.TestCase):
    def test_empty_store_and_unrelated_exchange_are_quiet(self):
        host, store = _build()
        self.assertEqual(host._render_trusted_beliefs_block("quiet company"), "")
        _reading(store)
        self.assertEqual(host._render_trusted_beliefs_block("database index performance"), "")

    def test_a_tentative_new_interpretation_can_help_before_a_concept_exists(self):
        host, store = _build()
        row = _reading(store)
        out = host._render_trusted_beliefs_block("stay with me")
        self.assertIn(row.predicted_state, out)
        self.assertIn("tentative", out)
        self.assertIn("Unknown/alternative", out)
        self.assertIn("not established facts", out)
        self.assertIn("not a list to bring up", out)
        self.assertIn("don't recite", out)
        self.assertIn("mixed feelings", out)
        self.assertNotIn("just assume", out)

    def test_confirming_a_temporary_reading_does_not_extend_its_lifetime(self):
        host, store = _build()
        row = _reading(store, expires_at=(timephrase.utcnow() - timedelta(minutes=1)).isoformat())
        store.update(row.id, status="confirmed")
        self.assertEqual(host._render_trusted_beliefs_block("quiet company"), "")

    def test_a_different_session_does_not_inherit_a_local_reading(self):
        host, store = _build()
        _reading(store)
        host.session_key = "u1:other-session"
        self.assertEqual(host._render_trusted_beliefs_block("quiet company"), "")

    def test_legacy_repetition_is_not_new_authority(self):
        host, store = _build()
        row = store.upsert(user_id="u1", kind="mood", topic="quiet company",
                           predicted_state="affectionate", source="worker")
        # Simulate the historical status; do not retroactively certify it.
        store.mark_confirmed(row.id, method="auto_confirm")
        self.assertEqual(host._render_trusted_beliefs_block("quiet company"), "")
        store.update(row.id, status="confirmed")
        self.assertIn("user confirmed", host._render_trusted_beliefs_block("quiet company"))

    def test_contradicted_readings_are_absent(self):
        host, store = _build()
        row = _reading(store)
        store.update(row.id, status="contradicted")
        self.assertEqual(host._render_trusted_beliefs_block("quiet company"), "")

    def test_relevance_filter_runs_before_the_output_cap(self):
        host, store = _build()
        _reading(store)
        for i in range(8):
            store.upsert(user_id="u1", kind="opinion", topic=f"database {i}",
                         predicted_state="Useful", source="worker")
        host._settings.agent.belief_trusted_block_max = 1
        out = host._render_trusted_beliefs_block("quiet company")
        self.assertEqual(out.count("\n- "), 1)
        self.assertIn("demanding conversation", out)

    def test_feature_flags_and_read_failure_remain_safe(self):
        host, store = _build()
        _reading(store)
        host._settings.agent.belief_trusted_block_enabled = False
        self.assertEqual(host._render_trusted_beliefs_block("quiet company"), "")
        host._settings.agent.belief_trusted_block_enabled = True
        host._belief_store = object()
        self.assertEqual(host._render_trusted_beliefs_block("quiet company"), "")
