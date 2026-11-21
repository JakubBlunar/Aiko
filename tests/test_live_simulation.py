from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
import requests

from app.core.infra import settings, timephrase
from app.core.live import simulation
from app.core.live.simulation import run_live_scenario
from app.llm.chat_client import ChatUsage
from scripts.live_scenarios import (
    LoopbackPolicyClient, loopback_url, require_local_model, summarize_trials, trial_metrics,
)


SCENARIOS = Path(__file__).parent / "scenarios" / "live"


def read_scenario(name: str) -> dict:
    return json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", [
    "cue-and-floor", "wait-and-focus", "local-observation",
    "topic-choice", "quiet-cadence", "interruption", "requested-update", "requested-update-ready",
    "learn-coding-timing",
    "listening-posture",
])
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


def test_listening_posture_tracks_the_floor_without_speaking():
    report = run_live_scenario(read_scenario("listening-posture"))
    assert report["expectation_failures"] == []
    steps = {step["label"]: step for step in report["steps"]}
    for label in ("The user starts speaking", "The user starts a new utterance immediately"):
        plan = steps[label]["embodiment"]["behavior_plan"]
        assert plan["listen_posture"] == "lean_in"
        assert plan["gaze_class"] == "user_eye_contact"
        assert plan["attention_target"] == "user"
    assert report["spoken"] == []
    assert report["dispatches"] == []


@pytest.mark.parametrize("reference", ["2", "9"])
def test_policy_reference_resolves_only_a_displayed_cue(reference):
    def policy_reply(messages, **kwargs):
        schema = kwargs["json_schema"]["properties"]["selected_urge_id"]
        if 'ref="2"' not in messages[0]["content"]:
            intent, selection = "acknowledge_user", None
        else:
            assert schema["enum"] == [None, "1", "2", "3"]
            assert 'ref="2" kind=share_observation subject=bread is cool enough to pack' in (
                messages[0]["content"]
            )
            intent, selection = "request_main_speech", reference
        return json.dumps({
            "intent": intent,
            "selected_urge_id": selection,
            "arguments": {"speech_act": "share_observation"},
            "reason_code": "test",
            "context_refs": [],
        }), ChatUsage()

    report = run_live_scenario(
        read_scenario("requested-update"), policy_client=Mock(chat_json=policy_reply),
    )
    decision, = report["steps"][4]["decisions"]
    if reference == "2":
        assert decision["selected_urge_reference"] == "2"
        assert decision["selected_subject"] == "bread is cool enough to pack"
        assert decision["selected_urge_id"] in decision["eligible_urge_ids"]
        assert "Subject: bread is cool enough to pack" in report["dispatches"][0]["talk_about"]
    else:
        assert decision["reason"] == "unknown_urge"
        assert decision["selected_subject"] == ""
        assert report["steps"][4]["queued_count"] == 0


def test_initial_preference_is_visible_from_the_first_impulse():
    preference = json.loads(
        (SCENARIOS / "preferences" / "requested-update.json").read_text(encoding="utf-8")
    )
    scenario = read_scenario("requested-update")
    base = run_live_scenario(scenario)
    seeded = run_live_scenario({**scenario, "initial_concepts": [preference]})
    assert base["steps"][0]["policy_context"]["policy_concepts"] == []
    assert seeded["steps"][0]["policy_context"]["policy_concepts"][0]["label"] == (
        preference["label"]
    )
    assert [(s["event"], s["at_ms"]) for s in seeded["steps"]] == [
        (s["event"], s["at_ms"]) for s in base["steps"]
    ]


@pytest.mark.parametrize(("metadata", "purpose"), [
    ({"requested_update_ready": True}, "report"),
    ({"requested_update_ready": "true"}, "share"),
    ({"requested_update_ready": True, "session_id": "another-session"}, "share"),
])
def test_ready_requested_update_reaches_the_policy_at_the_first_opening(metadata, purpose):
    scenario = read_scenario("requested-update-ready")
    subject = "bread is cool enough to pack"
    cue = next(
        step["cue"] for step in scenario["steps"]
        if "cue" in step and step["cue"]["subject"] == subject
    )
    cue["payload"] = metadata

    def policy_reply(messages, **kwargs):
        prompt = messages[0]["content"]
        cue_row = next(
            (line for line in prompt.splitlines()
             if line.startswith('ref="') and f"subject={subject}" in line),
            "",
        )
        intent = "acknowledge_user"
        reference = None
        if cue_row:
            assert f"purpose={purpose}" in cue_row
            assert ("REQUESTED UPDATE READY" in prompt) is (purpose == "report")
            intent = "request_main_speech"
            reference = cue_row.split('"')[1]
        return json.dumps({
            "intent": intent,
            "selected_urge_id": reference,
            "arguments": {"speech_act": "share_observation"},
            "reason_code": "test",
            "context_refs": [],
        }), ChatUsage()

    report = run_live_scenario(scenario, policy_client=Mock(chat_json=policy_reply))
    assert report["expectation_failures"] == []
    opening = next(
        step for step in report["steps"] if step["label"] == "First safe opening after typing stops"
    )
    decision, = opening["decisions"]
    assert not opening["frame"]["interaction"]["typing_active"]
    assert decision["selected_subject"] == subject
    assert len(decision["eligible_urge_ids"]) == 2
    dispatch, = report["dispatches"]
    assert dispatch["reason"] == "simulated_reply"
    assert dispatch["at_ms"] == opening["at_ms"]
    assert f"Subject: {subject}" in dispatch["talk_about"]
    assert report["steps"][-1]["cues"][subject]["state"] == "used"
    assert report["steps"][-1]["cues"]["next chapter"]["surfaced"] == 0
    assert report["steps"][-1]["main_wakes"] == 1


def test_learned_coding_timing_carries_over_and_keeps_both_kinds_of_cues(monkeypatch):
    real_database = simulation.ChatDatabase
    database_paths = []

    def isolated_database(path):
        assert path == Path(":memory:")
        database_paths.append(path)
        return real_database(path)

    monkeypatch.setattr(simulation, "ChatDatabase", isolated_database)
    report = run_live_scenario(read_scenario("learn-coding-timing"))
    assert report["expectation_failures"] == []
    steps = {step["label"]: step for step in report["steps"]}
    coding_lull = steps["First coding lull with only a casual topic"]
    assert coding_lull["new_policy_calls"] == 0
    assert coding_lull["cues"]["sketchbook layout"]["available"]
    opening = steps["First safe opening for the requested result"]
    decision, = opening["decisions"]
    assert opening["frame"]["session_id"] == "scenario:second-work-session"
    assert opening["frame"]["constraints"]["defer_casual_speech"]
    assert decision["selected_subject"] == "render is ready"
    assert decision["eligible_urge_ids"] == [decision["selected_urge_id"]]
    assert [(d["subject"], d["reason"]) for d in report["dispatches"]] == [
        ("", "no_queued_wake"),
        ("render is ready", "simulated_reply"),
        ("sketchbook layout", "simulated_reply"),
    ]
    assert report["dispatches"][1]["at_ms"] == opening["at_ms"]
    fresh = run_live_scenario({"steps": [{"activity_sample": "Code"}]})
    assert not fresh["steps"][0]["frame"]["constraints"]["defer_casual_speech"]
    assert database_paths == [Path(":memory:"), Path(":memory:")]


def test_queued_casual_share_is_held_if_coding_starts_before_dispatch():
    report = run_live_scenario({
        "steps": [
            {"impulse": "user.message_sent", "payload": {
                "text": "Remember to save casual topics for breaks while I'm coding.",
            }},
            {"impulse": "user.typing_started"},
            {"cue": {
                "type": "away_activities", "subject": "sketchbook layout",
                "text": "Aiko found a new layout for the shared sketchbook.",
            }},
            {"impulse": "user.typing_stopped"},
            {"advance_ms": 10000, "proposal": {
                "intent": "request_main_speech", "select_subject": "sketchbook layout",
            }},
            {"activity_sample": "Code"},
            {"dispatch": {"if_queued": True, "text": "This should stay queued until a break."}},
        ],
    })
    assert report["steps"][4]["queued_count"] == 1
    dispatch, = report["dispatches"]
    assert dispatch["reason"] == "focus_timing_preference"
    assert report["spoken"] == []
    assert report["steps"][-1]["cues"]["sketchbook layout"]["available"]


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


def test_interruption_trials_distinguish_admission_from_dispatch():
    runs = [run_live_scenario(read_scenario("interruption")) for _ in range(2)]
    totals = summarize_trials(runs)["totals"]
    assert totals["main_requests"] == totals["main_admitted"] == 4
    assert totals["dispatch_outcomes"] == {"floor_busy": 2, "new_user_intent": 2}
    assert totals["admitted_topics"] == {"bookshelf": 2, "oolong tea": 2}
    assert totals["interruptions_with_queued_speech"] == {
        "user.voice_start": 2, "user.speech_final": 2,
    }
    assert totals["micro_delivered"] == 0
    assert all(len(report["spoken"]) == 1 for report in runs)


def test_quiet_trial_counts_one_opening_per_window_and_nonadmitted_proposals_separately():
    report = run_live_scenario(read_scenario("quiet-cadence"))
    metrics = trial_metrics(report)
    assert metrics["admitted_topics"] == {"sketchbook": 1, "window plants": 1}
    assert metrics["dispatch_outcomes"] == {"simulated_reply": 1, "simulated_silence": 1}
    assert metrics["micro_requests"] == 0
    assert report["steps"][9]["main_wakes"] == 1


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
