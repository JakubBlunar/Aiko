from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
import requests

from app.core.infra import settings, timephrase
from app.core.live.simulation import run_live_scenario
from scripts.live_scenarios import LoopbackPolicyClient, loopback_url, require_local_model


SCENARIOS = Path(__file__).parent / "scenarios" / "live"


def read_scenario(name: str) -> dict:
    return json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["cue-and-floor", "wait-and-focus", "local-observation"])
def test_fixture_runs_without_network(name, monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("scripted replay made an HTTP request")

    monkeypatch.setattr(requests.Session, "request", unexpected_request)
    report = run_live_scenario(read_scenario(name))
    assert report["expectation_failures"] == []
    assert report["steps"][-1]["policy_failures"] == 0


def test_handoff_exposes_topic_and_cue_and_learns_from_the_next_user_message():
    report = run_live_scenario(read_scenario("cue-and-floor"))
    assert [item["at_ms"] for item in report["spoken"]] == [30000]
    observed = report["dispatches"][0]
    assert observed["speech_act"] == "gentle_question"
    assert "Subject: balcony herbs" in observed["talk_about"]
    assert "Ask whether the balcony herbs recovered" in observed["talk_about"]
    ledger = report["steps"][-1]["policy_context"]["policy_ledger"]
    assert any(row["completed_action"] == "request_main_speech" for row in ledger)
    assert report["steps"][-1]["cues"]["balcony herbs"]["state"] == "used"


def test_new_user_intent_preempts_a_queued_topic_before_the_fake_speaker():
    scenario = read_scenario("cue-and-floor")
    scenario["steps"] = [
        *scenario["steps"][:5],
        {"impulse": "user.message_sent", "payload": {"text": "Can we talk about something else?"}},
        {"dispatch": {"text": "This is stale and cannot reach the speaker."}},
        {"reply": "Sure, what’s on your mind?"},
    ]
    report = run_live_scenario(scenario)
    assert report["expectation_failures"] == []
    assert report["dispatches"][0]["reason"] == "new_user_intent"
    assert "talk_about" not in report["dispatches"][0]
    assert report["steps"][-1]["cues"]["balcony herbs"]["surfaced"] == 0
    assert [event["delivery"] for event in report["spoken"]] == ["Assistant (scripted reply)"]


def test_virtual_time_expires_impulse_and_cue_without_leaking_the_clock():
    real = datetime(2030, 7, 1, tzinfo=timezone.utc)
    with timephrase.scoped_now(lambda: real):
        report = run_live_scenario({
            "start_at": "2026-03-15T14:00:00+00:00",
            "steps": [
                {"impulse": "user.typing_started"},
                {"cue": {"type": "forward_curiosity", "subject": "oranges",
                         "text": "Ask about the oranges.", "ttl_hours": 0.002}},
                {"advance_ms": 10_000},
            ],
        })
        assert timephrase.utcnow() == real
    assert report["steps"][1]["cues"]["oranges"]["available"]
    last = report["steps"][-1]
    assert last["now"] == "2026-03-15T14:00:10+00:00"
    assert last["impulse_bus"]["expired"] == 1
    assert not last["cues"]["oranges"]["available"]


@pytest.mark.parametrize(
    "url", ["https://ollama.com", "http://192.168.1.2:11434",
            "http://localhost.example.com", "http://user@localhost", "file:///tmp/model"],
)
def test_only_loopback_policy_endpoints_are_accepted(url):
    with pytest.raises(ValueError, match="loopback"):
        loopback_url(url)
    assert loopback_url("http://localhost:11434/") == "http://127.0.0.1:11434"


def test_policy_transport_disables_proxies_and_refuses_redirects():
    session = Mock(spec=requests.Session)
    response = Mock(status_code=302)
    session.post.return_value = response
    base_url = "http://localhost:11434"
    client = LoopbackPolicyClient(settings.OllamaSettings(base_url=base_url), base_url, session)
    with pytest.raises(requests.HTTPError, match="redirect"):
        client.chat_json([{"role": "user", "content": "Synthetic input"}], think=False)
    assert session.trust_env is False
    assert session.post.call_args.args[0] == "http://127.0.0.1:11434/api/chat"
    assert session.post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("remote", [{"remote_model": "upstream"}, {"remote_host": "https://host"}])
def test_loopback_ollama_remote_alias_cannot_generate(remote):
    session = MagicMock()
    response = session.get.return_value.__enter__.return_value
    response.status_code = 200
    response.json.return_value = {"models": [{"name": "qwen:latest", "size": 12345, **remote}]}
    with pytest.raises(ValueError, match="not a local"):
        require_local_model(session, "http://localhost:11434", "qwen")
    session.post.assert_not_called()


def test_read_only_settings_resolves_migrations_without_writing(tmp_path, monkeypatch):
    user = tmp_path / "user.json"
    raw = '{"audio":{"microphone_device":99},"llm":{"routes":{"live_policy":{"max_tokens":64}}}}'
    user.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(settings, "USER_CONFIG_PATH", user)
    loaded = settings.load_settings(read_only=True)
    assert loaded.llm.routes["live_policy"].max_tokens != 64
    assert user.read_text(encoding="utf-8") == raw
