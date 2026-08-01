"""``GET /api/cue-pool`` and the ``cue_pool_updated`` frame.

The panel's whole point is that terminal rows stay on the table, so the
cases worth pinning are the filters that let you ask "what did she
actually use" and the live event that fires the moment a cue flips.

A real :class:`CueStore` behind a real :class:`CuePoolMixin`, with only
the transport mocked -- the response shape is assembled from the store's
own queries and would be worth nothing stubbed.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.web.server import create_web_app


class _Host(CuePoolMixin):
    def __init__(self, store: CueStore) -> None:
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))
        self.host = _Host(self.store)
        session = MagicMock()
        session.list_cue_pool.side_effect = self.host.list_cue_pool
        session.add_cue_pool_listener.side_effect = self.host.add_cue_pool_listener
        self.client = TestClient(create_web_app(session))


class ListTests(_Fixture):
    def test_a_fresh_cue_shows_up_pending(self) -> None:
        self.store.add("curiosity_seed", "film photography", "cue text")
        body = self.client.get("/api/cue-pool").json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["total"], 1)
        cue = body["cues"][0]
        self.assertEqual(cue["subject"], "film photography")
        self.assertEqual(cue["state"], "pending")
        self.assertNotIn("embedding", cue)

    def test_the_type_list_covers_every_policy(self) -> None:
        from app.core.proactive.cue_accounting import CUE_POLICIES

        body = self.client.get("/api/cue-pool").json()
        self.assertEqual(body["types"], sorted(CUE_POLICIES))

    def test_filtering_by_state_separates_spent_from_waiting(self) -> None:
        kept = self.store.add("curiosity_seed", "bread", "a")
        self.store.add("curiosity_seed", "kites", "b")
        self.store.mark_surfaced(kept)
        self.store.mark_used(kept, evidence="lexical:1.00")

        used = self.client.get("/api/cue-pool?state=used").json()
        self.assertEqual(used["total"], 1)
        self.assertEqual(used["cues"][0]["subject"], "bread")

        pending = self.client.get("/api/cue-pool?state=pending").json()
        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["cues"][0]["subject"], "kites")

    def test_filtering_by_type_narrows_the_total(self) -> None:
        self.store.add("curiosity_seed", "bread", "a")
        self.store.add("dormant_interest", "kites", "b")
        body = self.client.get("/api/cue-pool?cue_type=dormant_interest").json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["cues"][0]["cue_type"], "dormant_interest")

    def test_paging_reports_the_page_and_the_whole(self) -> None:
        for i in range(5):
            self.store.add("curiosity_seed", f"subject {i}", "cue")
        body = self.client.get("/api/cue-pool?limit=2&offset=2").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["total"], 5)

    def test_stats_carry_the_mean_surfacings_before_use(self) -> None:
        cue_id = self.store.add("curiosity_seed", "bread", "a")
        self.store.mark_surfaced(cue_id)
        self.store.mark_surfaced(cue_id)
        self.store.mark_used(cue_id, evidence="lexical:1.00")
        stats = self.client.get("/api/cue-pool").json()["stats"]
        entry = next(s for s in stats if s["cue_type"] == "curiosity_seed")
        self.assertEqual(entry["used"], 1)
        self.assertEqual(entry["mean_surfacings_before_use"], 2.0)

    def test_no_store_is_an_empty_pool_not_an_error(self) -> None:
        session = MagicMock()
        session.list_cue_pool.side_effect = _Host(None).list_cue_pool
        client = TestClient(create_web_app(session))
        body = client.get("/api/cue-pool").json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["cues"], [])


class BroadcastTests(_Fixture):
    def test_spending_a_cue_reaches_the_wire(self) -> None:
        seen: list[dict] = []
        self.host.add_cue_pool_listener(seen.append)
        cue_id = self.store.add("curiosity_seed", "film photography", "cue")
        self.store.mark_surfaced(cue_id)
        row = self.store.get(cue_id)
        self.host._mark_cue_used(self.store, row, evidence="lexical:1.00")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["id"], cue_id)
        self.assertEqual(seen[0]["state"], "used")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
