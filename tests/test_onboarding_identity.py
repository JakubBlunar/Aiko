"""First-run identity through the same REST and persistence paths as the UI."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.infra import settings
from app.core.session import lifecycle_mixin
from app.core.session.session_controller import SessionController
from app.web.rest import sessions_settings_routes


@pytest.fixture
def identity(tmp_path, monkeypatch):
    path = tmp_path / "user.json"
    monkeypatch.setattr(settings, "USER_CONFIG_PATH", path)
    session = SessionController.__new__(SessionController)
    session._settings = SimpleNamespace(assistant=SimpleNamespace(user_display_name=""))
    session._identity_lock = threading.Lock()
    listener = Mock()
    session.add_identity_listener(listener)
    hub = Mock()
    app = FastAPI()
    sessions_settings_routes.register(app, session, hub, None, None)
    with TestClient(app) as client:
        yield client, session, path, listener, hub


URL = "/api/settings/identity"


def test_onboarding_persists_first_name_and_idempotent_retry(identity):
    client, _, path, listener, hub = identity
    assert client.get(URL).json()["needs_onboarding"]
    for invalid in (" ", None, 42, "x" * 33):
        assert client.put(URL, json={"user_display_name": invalid}).status_code == 400
    for name in ("  Noor  ", "Noor"):
        result = client.put(URL, json={"user_display_name": name})
        assert result.status_code == 200
        assert result.json() == {"user_display_name": "Noor", "needs_onboarding": False}
    assert client.put(URL, json={"user_display_name": "Mira"}).status_code == 409
    assert client.get(URL).json()["user_display_name"] == "Noor"
    assert json.loads(path.read_text())["assistant"]["user_display_name"] == "Noor"
    listener.assert_called_once_with("Noor")
    hub.broadcast.assert_called_once_with({"type": "identity_changed", **result.json()})


def test_existing_profile_never_reopens_name_step(identity):
    client, session, _, listener, hub = identity
    session.settings.assistant.user_display_name = "Existing"
    assert client.put(URL, json={"user_display_name": "Another"}).status_code == 409
    assert client.get(URL).json() == {"user_display_name": "Existing", "needs_onboarding": False}
    listener.assert_not_called()
    hub.broadcast.assert_not_called()


def test_persistence_failure_leaves_onboarding_retryable(identity, monkeypatch):
    client, _, _, listener, hub = identity
    with monkeypatch.context() as m:
        m.setattr(lifecycle_mixin, "persist_user_overrides", Mock(side_effect=OSError))
        assert client.put(URL, json={"user_display_name": "Noor"}).status_code == 503
    assert client.get(URL).json()["needs_onboarding"]
    listener.assert_not_called()
    hub.broadcast.assert_not_called()
    assert client.put(URL, json={"user_display_name": "Noor"}).status_code == 200


def test_competing_windows_agree_on_the_first_saved_name(identity):
    client, _, path, listener, hub = identity
    start = threading.Barrier(2)

    def submit(name):
        start.wait(timeout=5)
        return client.put(URL, json={"user_display_name": name})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["Noor", "Mira"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    name = next(r.json()["user_display_name"] for r in responses if r.status_code == 200)
    assert client.get(URL).json()["user_display_name"] == name
    assert json.loads(path.read_text())["assistant"]["user_display_name"] == name
    listener.assert_called_once_with(name)
    hub.broadcast.assert_called_once()
