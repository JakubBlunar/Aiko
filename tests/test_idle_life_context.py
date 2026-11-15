"""Optional inspiration remains bounded, current, source-linked and non-compulsory."""
from datetime import datetime, timedelta, timezone
import json
import random
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.memory_settings import parse_memory_settings
from app.core.world.activity_selection import compute_weights, weighted_pick
from app.core.world.day_intention import propose
from app.core.world.idle_activity_worker import IdleAwayActivityWorker
from app.core.world.idle_life_context import IdleLifeContext, IdleLifeContextProvider
from app.core.world.idle_life_store import IdleLifeStore
from app.core.world.idle_life_trace import trace_run
from app.core.world.world_store import WorldStore
from app.llm.token_utils import estimate_tokens

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def message(i, text, *, days=0, role="user"):
    return NS(id=i, content=text, created_at=(NOW - timedelta(days=days)).isoformat(),
              role=role, session_id="test")


def memory(i, text, *, source=None, days=0, **metadata):
    return NS(id=i, content=text, created_at=(NOW - timedelta(days=days)).isoformat(),
              kind="reflection", metadata=metadata, source_message_id=source,
              source_session="test", event_time=None, temporal_type="past_event",
              relevance_until=None, provenance="test")


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    db = NS(rows=[message(10, "Try separating the skyline rooftops with shadows."),
                  message(11, "I sketched those skyline rooftops already.", role="assistant"),
                  message(12, "A dinosaur in a spacesuit!", days=10)],
            state={})
    db.get_messages = lambda session, limit: db.rows[-limit:]
    db.get_message_row = lambda mid: next((r for r in db.rows if r.id == mid), None)
    db.kv_get = lambda key: json.dumps(db.state) if db.state else None
    memories = NS(rows=[memory(20, "An extracted skyline idea.", source=10)])
    memories.list_recent = lambda limit: memories.rows[:limit]
    memories.get = lambda mid: next((r for r in memories.rows if r.id == mid), None)
    concept = NS(concept_id=3, label="I like drawing and playing with perspective.",
                 subject="aiko", status="active", kind="taste", confidence=.8)
    view = NS(rows=[concept])
    view.for_consumer = lambda consumer: view.rows
    view.evidence_packet = lambda c, limit: {"revision": "version1", "source_issues": [],
        "validity": {"viable": True}, "evidence": [{"memory_id": 20}],
        "uncertainty": "Narrow support is tentative."}
    settings = NS(idle_life_context_enabled=True, idle_life_context_ratio=1.,
                  idle_life_context_max_tokens=900)
    value = IdleLifeContextProvider(chat_db=db, session_provider=lambda: "test",
        view_provider=lambda: view, memory_provider=lambda: memories,
        settings_provider=lambda: settings, rng=random.Random(4))
    return value, db, memories, view, settings


def test_free_choice_and_disabled_routes_do_not_fetch_context(provider):
    value, db, _, _, settings = provider
    db.get_messages = Mock(side_effect=AssertionError("Must not retrieve on free route"))
    settings.idle_life_context_ratio = 0
    result = value.build(available_keys={"doodle"})
    assert result.reason == "free_choice" and result.text == "" and result.biases == ()
    settings.idle_life_context_enabled = False
    settings.idle_life_context_ratio = 1
    assert value.build().reason == "disabled"
    db.get_messages.assert_not_called()


def test_default_sampling_leaves_many_independent_choices(provider):
    value, _, _, _, settings = provider
    del settings.idle_life_context_ratio
    sampled = [value.build(available_keys={"doodle"}).sampled for _ in range(100)]
    assert 30 < sum(sampled) < 70


def test_current_age_tagged_sources_are_budgeted_and_role_preserving(provider):
    value, _, _, _, _ = provider
    context = value.build(available_keys={"doodle", "wander"}, project="skyline drawing")
    assert "message:10" in context.text and "User:" in context.text and "Aiko:" in context.text
    assert "may ignore all" in context.text
    assert "dinosaur" not in context.text
    assert "[just now] User:" in context.text
    assert estimate_tokens(context.text) <= 900
    metadata = json.dumps(context.trace())
    assert "rooftops with shadows" not in metadata  # private passages never copied to the trace.
    assert "source_message_id" in metadata


def test_deleted_or_edited_sources_do_not_survive_a_context_cache(provider):
    value, db, _, _, _ = provider
    assert "shadows" in value.build(available_keys={"doodle"}).text
    db.rows[0].content = "The skyline should use clear silhouettes instead."
    fresh = value.build(available_keys={"doodle"})
    assert "silhouettes" in fresh.text and "with shadows" not in fresh.text
    db.rows = []
    fresh = value.build(available_keys={"doodle"})
    assert all(ref["ref"] != "memory:20" for ref in fresh.sources)


def test_invalid_and_user_concepts_do_not_become_aikos_personality(provider):
    value, _, _, view, _ = provider
    view.rows[0].subject = "user"
    assert all(ref["type"] != "concept" for ref in value.build().sources)
    view.rows[0].subject = "aiko"
    view.evidence_packet = lambda c, limit: {"validity": {"viable": False},
                                           "evidence": [{"memory_id": 20}]}
    assert all(ref["type"] != "concept" for ref in value.build().sources)


def test_retracted_expired_memory_and_unavailable_context_are_optional(provider):
    value, db, memories, _, _ = provider
    memories.rows[0].metadata["superseded_by"] = 30
    assert all(ref["ref"] != "memory:20" for ref in value.build().sources)
    memories.rows[0].metadata = {}
    memories.rows[0].relevance_until = (NOW - timedelta(hours=1)).isoformat()
    assert all(ref["ref"] != "memory:20" for ref in value.build().sources)
    db.get_messages = Mock(side_effect=RuntimeError("DB unavailable"))
    assert value.build().reason == "context_unavailable"


def test_project_finding_can_be_older_but_cannot_award_practice(provider):
    value, db, memories, _, _ = provider
    db.state = {"key": "skyline", "started_at": "start", "research_result_ids": [99]}
    memories.rows.append(memory(99, "Shadows can separate roof shapes.", days=8,
        research_evidence=[{"url": "https://example.test/source"}],
        hobby_key="skyline", hobby_started_at="start"))
    context = value.build(project="skyline")
    assert "memory:99" in context.text
    assert "not verified knowledge or completed practice" in context.text
    assert any(r.get("mode") == "research_excerpt" for r in context.sources)


def test_budget_and_empty_inputs_never_force_a_source(provider):
    value, db, memories, view, settings = provider
    settings.idle_life_context_max_tokens = 200
    view.rows[0].label = "sketch " * 2000
    context = value.build(available_keys={"doodle"})
    assert estimate_tokens(context.text) <= 200
    db.rows, memories.rows, view.rows = [], [], []
    result = value.build()
    assert not result.text and not result.biases and result.reason == "no_relevant_sources"


def test_inspiration_cannot_remove_unrelated_choices_or_override_repetition():
    keys = ["doodle", "wander", "nap"]
    bias = {"doodle": 9999, "impossible": 9999}
    weights = compute_weights(keys, context_weights=bias)
    assert weights == {"doodle": 1.25, "wander": 1., "nap": 1.}
    assert compute_weights(keys, recent_keys=["doodle"] * 4,
                           context_weights=bias)["doodle"] < 1
    rng = random.Random(3)
    chosen = [weighted_pick(keys, rng=rng, context_weights=bias) for _ in range(200)]
    assert set(chosen) == set(keys)
    assert chosen.count("doodle") < 120


def test_only_offered_refs_are_attributed_and_no_reasoning_is_logged(tmp_path):
    db = ChatDatabase(tmp_path / "attribution.db")
    store = IdleLifeStore(db)
    context = IdleLifeContext("private chat text", ({"ref": "message:10", "id": 10},))
    with trace_run(store, "away_activity"):
        context.attribution({"inspired_by": ["message:10", "message:999", {"bad": "type"}],
                             "choice_reason": "my secret detailed reasoning"})
    payload = store.history(kind="context_attribution")["events"][0]["payload"]
    assert payload["model_selected_refs"] == ["message:10"]
    assert payload["invalid_reference_count"] == 1
    assert payload["choice_reason"] == "other"
    assert "private" not in json.dumps(payload) and "secret" not in json.dumps(payload)


def test_model_can_ignore_context_and_save_unrelated_real_work(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    db = ChatDatabase(tmp_path / "writing.db")
    world = WorldStore(db._db_path)
    world.seed_default()
    calls = []
    def chat(messages, **kwargs):
        calls.append(messages)
        return json.dumps({"activity": "writing", "location_slug": "desk", "posture": "sitting",
            "creation": {"title": "The lamp", "form": "poem", "item_id": None,
                         "base_revision": 0, "text": "A small lamp waits beside the sea. " * 3},
            "inspired_by": [], "choice_reason": "own_interest"}), None
    context = IdleLifeContext("Optional rooftop shadow idea. You may ignore this.",
                              ({"ref": "message:10"},), sampled=True)
    worker = IdleAwayActivityWorker(world_store=world, kv_get=db.kv_get, kv_set=db.kv_set,
        user_display_name_provider=lambda: "Jacob", ollama=NS(chat_json=chat), model="test",
        llm_activity_ratio=1, context_provider=lambda *args, **kwargs: context)
    result = worker.run()
    assert result["fired"] == 1 and result["item_effect"]["effect"] == "save_creation"
    saved = world.get_item(result["item_effect"]["item_id"])
    assert saved.state["revisions"][0]["title"] == "The lamp"
    assert "rooftop shadow" in calls[0][1]["content"]
    assert len(calls) == 1  # no context-summarization LLM call.
    world.close()


def test_hobby_context_reaches_existing_model_call_without_extra_inference():
    from test_hobby_worker import _FakeChatDb, _FakeOllama, _worker
    db, llm = _FakeChatDb(), _FakeOllama()
    worker = _worker(db=db, ollama=llm)
    worker._context_provider = lambda *args, **kwargs: IdleLifeContext(
        "Optional ceramic handle design. Choosing something else is fine.")
    assert worker._compose_next_llm({"kind": "reading"}, ()) is not None
    assert len(llm.calls) == 1
    assert "ceramic handle" in llm.calls[0]["messages"][1]["content"]


def test_daily_context_hints_still_leave_room_for_other_intentions():
    rng = random.Random(11)
    results = [propose([], now=NOW, hobby="astronomy", rng=rng,
                       inspirations=(("take a break to stretch", "stretching"),),
                       available_keys={"stretching", "wander"}) for _ in range(100)]
    assert {row.beat_key for row in results} == {"hobby", "stretching"}
    assert sum(row.beat_key == "stretching" for row in results) < 60


def test_context_settings_parse_and_clamp():
    defaults = parse_memory_settings({})
    assert defaults.idle_life_context_ratio == .5
    assert defaults.idle_life_context_max_tokens == 900
    extreme = parse_memory_settings({"idle_life_context_ratio": -5,
                                    "idle_life_context_max_tokens": 100000})
    assert extreme.idle_life_context_ratio == 0
    assert extreme.idle_life_context_max_tokens == 1400


def test_added_context_respects_small_worker_window(provider):
    value, _, _, _, _ = provider
    value._window = lambda: 4096
    context = value.build(available_keys={"doodle"})
    assert estimate_tokens(context.text) <= 245
