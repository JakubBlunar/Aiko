"""End-to-end tests for the K9 ``GET /api/topic-graph`` REST surface.

Uses a MagicMock-backed ``SessionController`` so we only exercise the
endpoint wiring -- the snapshot shape itself is covered by
``tests/test_topic_graph.py``.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


_SNAPSHOT = {
    "enabled": True,
    "total_memories": 6,
    "total_clusters": 2,
    "clustered_memories": 6,
    "similarity": 0.55,
    "min_cluster_size": 3,
    "filter_threshold": 0.65,
    "clusters": [
        {
            "cluster_id": 0,
            "summary": "cat naps in sunbeams",
            "size": 3,
            "representative_id": 1,
            "kind_counts": {"fact": 3},
            "members": [
                {
                    "id": 1,
                    "content": "cat naps in sunbeams",
                    "kind": "fact",
                    "salience": 0.5,
                    "tier": "long_term",
                },
            ],
        },
    ],
}


def _build_client(snapshot: dict | None = None) -> TestClient:
    session = MagicMock()
    session.topic_graph_snapshot.return_value = (
        snapshot if snapshot is not None else _SNAPSHOT
    )
    return TestClient(create_web_app(session))


class TopicGraphEndpointTests(unittest.TestCase):
    def test_returns_snapshot(self) -> None:
        client = _build_client()
        resp = client.get("/api/topic-graph")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["total_clusters"], 2)
        self.assertEqual(len(body["clusters"]), 1)
        self.assertEqual(body["clusters"][0]["members"][0]["id"], 1)
        for key in (
            "total_memories",
            "clustered_memories",
            "similarity",
            "min_cluster_size",
            "filter_threshold",
        ):
            self.assertIn(key, body)

    def test_disabled_shape(self) -> None:
        client = _build_client({
            "enabled": False,
            "total_memories": 0,
            "total_clusters": 0,
            "clustered_memories": 0,
            "similarity": 0.0,
            "min_cluster_size": 0,
            "filter_threshold": 0.0,
            "clusters": [],
        })
        resp = client.get("/api/topic-graph")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["clusters"], [])


if __name__ == "__main__":
    unittest.main()
