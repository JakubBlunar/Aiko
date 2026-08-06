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
from web_fake_session import FakeSession


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
    session = FakeSession()
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
            limit=50,
            subject="aiko",
            event_type=None,
            before_id=9,
            concept_id=None,
        )

    def test_forwards_concept_id_for_one_beliefs_arc(self) -> None:
        client, session = _client()
        session.concept_timeline.return_value = _TIMELINE
        resp = client.get("/api/concepts/timeline?concept_id=42")
        self.assertEqual(resp.status_code, 200)
        session.concept_timeline.assert_called_once_with(
            limit=200,
            subject=None,
            event_type=None,
            before_id=None,
            concept_id=42,
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


class ConceptsQualityTests(unittest.TestCase):
    def test_returns_quality_report(self) -> None:
        client, session = _client()
        session.concept_quality.return_value = {
            "enabled": True,
            "totals": {"total": 3},
            "flow": {"promotion_rate_pct": 91.0, "demotion_events": 0},
            "register": {"identity/user": {"n": 3, "frame_pct": 72.0}},
        }
        resp = client.get("/api/concepts/quality")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["flow"]["promotion_rate_pct"], 91.0)
        self.assertEqual(body["register"]["identity/user"]["frame_pct"], 72.0)

    def test_disabled_shape(self) -> None:
        client, session = _client()
        session.concept_quality.return_value = {"enabled": False}
        resp = client.get("/api/concepts/quality")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])

    def test_quality_route_does_not_shadow_the_delete_path(self) -> None:
        # "/api/concepts/{concept_id}" is an int path param, so a literal
        # "/quality" segment must resolve to its own route rather than
        # 422-ing on the id coercion.
        client, session = _client()
        session.concept_quality.return_value = {"enabled": True}
        self.assertEqual(
            client.get("/api/concepts/quality").status_code, 200
        )
        session.concept_quality.assert_called_once()


_LEARNING = {
    "enabled": True,
    "total": 1,
    "counts": {"succession": 1},
    "events": [
        {
            "id": 1,
            "fingerprint": "abc",
            "shape": "succession",
            "concept_id": 7,
            "prior_concept_id": 3,
            "kind": "identity",
            "subject": "user",
            "old_label": "likes detailed answers",
            "new_label": "prefers depth calibrated to the topic",
            "because": "what looked like A turned out to be B",
            "resolution": "now held as B",
            "salience": 0.72,
            "plasticity": 0.3,
            "confidence_delta": 0.4,
            "cosine": 0.72,
            "decisive_event_id": 9,
            "trigger_event_ids": [4, 9],
            "evidence_refs": [["memory", "1"]],
            "evidence_labels": ["the evening he explained it"],
            "created_at": "2026-07-03T21:18:00+00:00",
        },
    ],
}


class ConceptLearningTests(unittest.TestCase):
    def test_returns_learning_feed(self) -> None:
        client, session = _client()
        session.concept_learning_events.return_value = _LEARNING
        resp = client.get("/api/concepts/learning")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["events"][0]["shape"], "succession")
        self.assertEqual(
            body["events"][0]["old_label"], "likes detailed answers"
        )

    def test_forwards_filters(self) -> None:
        client, session = _client()
        session.concept_learning_events.return_value = _LEARNING
        resp = client.get(
            "/api/concepts/learning"
            "?limit=10&subject=aiko&shape=relabel"
            "&concept_id=4&min_salience=0.5&before_id=8"
        )
        self.assertEqual(resp.status_code, 200)
        session.concept_learning_events.assert_called_once_with(
            limit=10,
            subject="aiko",
            shape="relabel",
            concept_id=4,
            min_salience=0.5,
            before_id=8,
        )

    def test_learning_route_does_not_shadow_the_delete_path(self) -> None:
        client, session = _client()
        session.concept_learning_events.return_value = _LEARNING
        self.assertEqual(
            client.get("/api/concepts/learning").status_code, 200
        )
        session.delete_concept.assert_not_called()

    def test_disabled_shape(self) -> None:
        client, session = _client()
        session.concept_learning_events.return_value = {
            "enabled": False, "total": 0, "counts": {}, "events": [],
        }
        resp = client.get("/api/concepts/learning")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])


class ConceptProvenanceTests(unittest.TestCase):
    def test_returns_provenance(self) -> None:
        client, session = _client()
        session.concept_provenance.return_value = {
            "enabled": True,
            "concept_id": 7,
            "resolved_id": 7,
            "exists": True,
            "label": "prefers depth",
            "prior_labels": ["likes detail", "prefers depth"],
            "learning_events": [],
            "lifecycle": [],
        }
        resp = client.get("/api/concepts/7/provenance")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["prior_labels"][0], "likes detail")
        session.concept_provenance.assert_called_once_with(7)

    def test_merged_away_concept_still_resolves(self) -> None:
        client, session = _client()
        session.concept_provenance.return_value = {
            "enabled": True,
            "concept_id": 3,
            "resolved_id": 7,
            "exists": False,
            "alias_chain": [3, 7],
        }
        body = client.get("/api/concepts/3/provenance").json()
        self.assertEqual(body["resolved_id"], 7)
        self.assertFalse(body["exists"])

    def test_disabled_shape(self) -> None:
        client, session = _client()
        session.concept_provenance.return_value = {
            "enabled": False, "concept_id": 1,
        }
        resp = client.get("/api/concepts/1/provenance")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])


class ConceptDriftRouteTests(unittest.TestCase):
    def test_state(self) -> None:
        client, session = _client()
        session.concept_drift_state.return_value = {
            "enabled": True, "watermark": 12, "latest_event_id": 30,
        }
        body = client.get("/api/concepts/drift").json()
        self.assertEqual(body["watermark"], 12)

    def test_forced_run_returns_stats(self) -> None:
        client, session = _client()
        session.run_concept_drift.return_value = {
            "enabled": True, "stats": {"relabel_applied": 1},
        }
        resp = client.post("/api/concepts/drift/run")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["stats"]["relabel_applied"], 1)
        session.run_concept_drift.assert_called_once()


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
