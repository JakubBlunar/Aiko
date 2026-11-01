"""REST archive uses the real facade and real isolated SQLite ownership."""

from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.web.rest.archive_routes import register
from web_fake_session import FakeSession


@pytest.fixture
def archive_client(tmp_path):
    db = ChatDatabase(tmp_path / "routes.db")
    with patch.object(
        timephrase, "utcnow", return_value=datetime.fromisoformat("2026-10-20T12:00:00+00:00")
    ):
        own = db.add_message("alice:main", "user", "Guitar plans")
        foreign = db.add_message("bob:main", "user", "Private material")
    session = FakeSession(
        _chat_db=db,
        _user_id="alice",
        _idle_scheduler=object(),
        _settings=SimpleNamespace(
            tools=SimpleNamespace(
                enabled=True, daily_overviews=True, archive_timezone="Europe/Bratislava"
            )
        ),
    )
    app = FastAPI()
    register(app, session)
    with TestClient(app) as client:
        yield client, session, own, foreign
    db._get_conn().close()


def test_browse_missing_day_read_transcript_and_cross_owner_drilldown(archive_client):
    client, _, own, foreign = archive_client
    result = client.get("/api/archive", params={"start": "2026-10-20", "end": "2026-10-20"})
    assert result.status_code == 200
    assert result.json()["missing_days"] == ["2026-10-20"]
    assert result.json()["generation_available"]
    messages = client.get(
        "/api/archive",
        params={
            "mode": "messages",
            "start": "2026-10-20",
            "end": "2026-10-20",
            "topic": "Guitar",
        },
    ).json()["messages"]
    assert [message["message_id"] for message in messages] == [own]
    assert (
        client.get("/api/archive", params={"mode": "message", "message_id": foreign}).json()[
            "status"
        ]
        == "not_found"
    )


def test_request_cancel_and_disabled_generation(archive_client):
    client, session, _, _ = archive_client
    payload = {"day": "2026-10-20"}
    assert client.post("/api/archive/summary", json=payload).status_code == 202
    assert client.get("/api/archive").json()["jobs"][0]["state"] == "pending"
    assert client.request("DELETE", "/api/archive/summary", json=payload).status_code == 200
    assert not client.get("/api/archive").json()["jobs"]
    session._settings.tools.daily_overviews = False
    assert client.post("/api/archive/summary", json=payload).status_code == 409
    # Disabling generation does not prevent inspecting one's retained messages.
    assert client.get("/api/archive").status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        {"start": "2026-10-20"},
        {"timezone": "Missing/Zone"},
        {"mode": "message", "message_id": -1},
        {"topic": "x" * 201},
        {"mode": "bad"},
    ],
)
def test_invalid_queries_do_not_become_server_errors(archive_client, query):
    client, _, _, _ = archive_client
    assert client.get("/api/archive", params=query).status_code in {400, 422}


def test_invalid_job_day_and_zone_are_rejected(archive_client):
    client, _, _, _ = archive_client
    assert client.post("/api/archive/summary", json={"day": "2026-99-01"}).status_code == 400
    assert (
        client.post(
            "/api/archive/summary", json={"day": "2026-10-20", "timezone": "Missing/Zone"}
        ).status_code
        == 400
    )


def test_unavailable_database_returns_service_unavailable(archive_client):
    client, session, _, _ = archive_client
    session._chat_db = None
    result = client.get("/api/archive")
    assert result.status_code == 503
    assert not result.json()["generation_available"]
