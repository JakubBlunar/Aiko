"""End-to-end tests for the concept debug REST surface.

Uses a MagicMock-backed ``SessionController`` so we only exercise the
endpoint wiring; the snapshot shape itself is covered by
``tests/test_concept_snapshot.py``.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


_SNAPSHOT = {
    "enabled": True,
    "total": 1,
    "counts": {"by_status": {"candidate": 1}, "by_subject": {"user": 1}},
    "concepts": [
        {
            "id": 1,
            "label": "Systems thinker",
            "kind": "identity",
            "subject": "user",
            "status": "candidate",
            "confidence": 0.7,
            "evidence_count": 2,
            "evidence": [
                {"src_type": "cluster", "src_id": "100", "label": "debugging"},
            ],
        },
    ],
}


def _client(snapshot: dict | None = None) -> tuple[TestClient, MagicMock]:
    session = MagicMock()
    session.concepts_snapshot.return_value = (
        snapshot if snapshot is not None else _SNAPSHOT
    )
    return TestClient(create_web_app(session)), session


class ConceptsGetTests(unittest.TestCase):
    def test_returns_snapshot(self) -> None:
        client, _ = _client()
        resp = client.get("/api/concepts")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["concepts"][0]["label"], "Systems thinker")

    def test_disabled_shape(self) -> None:
        client, _ = _client({
            "enabled": False,
            "total": 0,
            "counts": {"by_status": {}, "by_subject": {}},
            "concepts": [],
        })
        resp = client.get("/api/concepts")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["concepts"], [])


class ConceptsRunTests(unittest.TestCase):
    def test_run_returns_stats(self) -> None:
        client, session = _client()
        session._concept_synthesis_worker.run.return_value = {
            "added": 2, "reinforced": 1
        }
        resp = client.post("/api/concepts/run")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["result"]["added"], 2)

    def test_run_503_when_worker_absent(self) -> None:
        client, session = _client()
        session._concept_synthesis_worker = None
        resp = client.post("/api/concepts/run")
        self.assertEqual(resp.status_code, 503)


_TIMELINE = {
    "enabled": True,
    "total": 2,
    "events": [
        {
            "id": 2,
            "concept_id": 7,
            "event_type": "discovered",
            "kind": "identity",
            "subject": "aiko",
            "label": "I value being direct",
            "confidence": 0.8,
            "novelty": 1.0,
            "evidence_count": 2,
            "distinct_source_count": 2,
            "source_kinds": "memory",
            "reason": "First self-concept linking 2 reflection/diary memories.",
            "created_at": "2026-07-03T21:18:00+00:00",
        },
    ],
}


class ConceptsTimelineTests(unittest.TestCase):
    def test_returns_timeline(self) -> None:
        client, session = _client()
        session.concept_timeline.return_value = _TIMELINE
        resp = client.get("/api/concepts/timeline")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["events"][0]["event_type"], "discovered")

    def test_forwards_query_params(self) -> None:
        client, session = _client()
        session.concept_timeline.return_value = _TIMELINE
        resp = client.get(
            "/api/concepts/timeline?limit=50&subject=aiko&before_id=9"
        )
        self.assertEqual(resp.status_code, 200)
        session.concept_timeline.assert_called_once_with(
            limit=50, subject="aiko", event_type=None, before_id=9
        )

    def test_disabled_shape(self) -> None:
        client, session = _client()
        session.concept_timeline.return_value = {
            "enabled": False,
            "total": 0,
            "events": [],
        }
        resp = client.get("/api/concepts/timeline")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])


class ConceptsDeleteTests(unittest.TestCase):
    def test_delete_ok(self) -> None:
        client, session = _client()
        session.delete_concept.return_value = 1
        resp = client.delete("/api/concepts/5")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted"], 1)
        session.delete_concept.assert_called_once_with(5)

    def test_delete_404_when_missing(self) -> None:
        client, session = _client()
        session.delete_concept.return_value = 0
        resp = client.delete("/api/concepts/999")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
