"""Tests for ``GET /api/sessions/{id}/messages`` pagination (I6).

The endpoint has two modes:

  - default (no ``before_id``): the most-recent ``limit`` rows, via
    ``ChatDatabase.get_messages`` — the existing initial-load contract.
  - ``before_id`` given: up to ``limit`` rows immediately older than
    that id, via ``ChatDatabase.get_messages_before`` — the keyset
    "load older" page.

We assert the routing + serialisation shape with a mocked ``_chat_db``;
the DB behaviour itself is covered in ``tests/test_chat_database.py``.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


def _row(mid: int, content: str):
    return SimpleNamespace(
        id=mid,
        role="user",
        content=content,
        created_at="2026-06-28T00:00:00Z",
        reactions=None,
        gestures=None,
        attachments=None,
    )


def _build_client() -> tuple[TestClient, MagicMock]:
    chat_db = MagicMock()
    session = MagicMock()
    session._chat_db = chat_db
    app = create_web_app(session)
    return TestClient(app), chat_db


class SessionMessagesPaginationTests(unittest.TestCase):
    def test_default_uses_get_messages_newest_page(self) -> None:
        client, chat_db = _build_client()
        chat_db.get_messages.return_value = [_row(1, "a"), _row(2, "b")]
        response = client.get("/api/sessions/s1/messages?limit=50")
        self.assertEqual(response.status_code, 200, response.text)
        chat_db.get_messages.assert_called_once_with("s1", limit=50)
        chat_db.get_messages_before.assert_not_called()
        body = response.json()
        self.assertEqual([r["content"] for r in body], ["a", "b"])
        self.assertEqual(body[0]["id"], 1)

    def test_before_id_uses_keyset_path(self) -> None:
        client, chat_db = _build_client()
        chat_db.get_messages_before.return_value = [_row(3, "older")]
        response = client.get("/api/sessions/s1/messages?limit=100&before_id=7")
        self.assertEqual(response.status_code, 200, response.text)
        chat_db.get_messages_before.assert_called_once_with(
            "s1", before_id=7, limit=100,
        )
        chat_db.get_messages.assert_not_called()
        self.assertEqual(response.json()[0]["content"], "older")

    def test_limit_is_capped(self) -> None:
        client, chat_db = _build_client()
        chat_db.get_messages.return_value = []
        client.get("/api/sessions/s1/messages?limit=99999")
        chat_db.get_messages.assert_called_once_with("s1", limit=1000)


if __name__ == "__main__":
    unittest.main()
