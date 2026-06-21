"""End-to-end tests for the K10 persona-drift REST surface.

Uses a MagicMock-backed ``SessionController`` so we only exercise the
endpoint wiring -- the snapshot shape + scoring are covered by
``tests/test_persona_regression.py``.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


_SNAPSHOT = {
    "ran_at": "2026-06-21T12:00:00+00:00",
    "model": "fake-model",
    "ran_ms": 42.0,
    "total": 3,
    "passed": 2,
    "failed": 1,
    "results": [
        {
            "id": "rough_day",
            "scope": "minimal",
            "passed": False,
            "failures": ["missing tag: '[[reaction:'"],
            "reply_preview": "sorry to hear that",
        },
    ],
}


def _build_client(*, snapshot=None, run_result=None) -> tuple[TestClient, MagicMock]:
    session = MagicMock()
    session.persona_regression_snapshot.return_value = (
        snapshot if snapshot is not None else {}
    )
    session.run_persona_regression.return_value = (
        run_result if run_result is not None else _SNAPSHOT
    )
    return TestClient(create_web_app(session)), session


class PersonaDriftGetTests(unittest.TestCase):
    def test_empty_before_run(self) -> None:
        client, _ = _build_client(snapshot={})
        resp = client.get("/api/persona-drift")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {})

    def test_returns_persisted_snapshot(self) -> None:
        client, _ = _build_client(snapshot=_SNAPSHOT)
        resp = client.get("/api/persona-drift")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["passed"], 2)


class PersonaDriftRunTests(unittest.TestCase):
    def test_run_returns_snapshot(self) -> None:
        client, session = _build_client()
        resp = client.post("/api/persona-drift/run")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertGreater(body["total"], 0)
        self.assertIn("results", body)
        session.run_persona_regression.assert_called_once()


if __name__ == "__main__":
    unittest.main()
