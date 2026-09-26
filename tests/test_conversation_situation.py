from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.conversation.conversation_situation import (
    ACTIVE,
    ENDED,
    UNCERTAIN,
    ConversationSituationStore,
    SituationExtraction,
    parse_extraction,
    reduce_situation,
)
from app.core.conversation.conversation_situation_worker import (
    ConversationSituationWorker,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_producer import CueProducer
from app.core.proactive.cue_store import CueStore
from app.core.session.conversation_situation_mixin import ConversationSituationMixin
from app.core.session.prompt_assembler import PromptAssembler, _PROMPT_BLOCK_TIERS
from app.core.conversation.stance import _OFFERS
from app.core.world.circadian_settle_worker import CircadianSettleWorker
from app.core.world.garden_visit_worker import GardenVisitWorker
from app.core.world.idle_activity_worker import IdleAwayActivityWorker
from app.core.world.world_mutation_guard import WorldMutationGuard
from app.llm.chat_client import ChatUsage
from app.mcp.server_tools import conversation_situation_tools


def _replacement() -> SituationExtraction:
    return SituationExtraction(
        operation="replace",
        summary="Aiko and the user are sitting together on the beanbag.",
        shared=True,
        shared_activity="talking together",
        place_ref="beanbag",
        aiko_activity="talking with the user",
        evidence_message_ids=(11, 12),
    )


def test_parse_open_ended_replacement() -> None:
    raw = """{
      "operation": "replace",
      "summary": "Aiko and the user are curled up for a sci-fi marathon.",
      "shared": true,
      "shared_activity": "watching a fan-made space opera",
      "place_ref": "beanbag",
      "aiko_activity": "watching with the user",
      "evidence_message_ids": [11, 12]
    }"""
    parsed = parse_extraction(raw, valid_message_ids={10, 11, 12})
    assert parsed is not None
    assert parsed.shared_activity == "watching a fan-made space opera"
    assert parsed.evidence_message_ids == (11, 12)


def _working_extraction():
    return {
        "operation": "replace", "summary": "Comparing two camera lenses.",
        "evidence_message_ids": [11, 12],
        "working_set": {
            "question": {
                "text": "Which lens suits indoor portraits?", "evidence_message_ids": [11],
            },
            "facts": [{"text": "The room is small.", "evidence_message_ids": [11]}],
            "interpretation": {
                "text": "The shorter lens may fit the room.", "evidence_message_ids": [11, 12],
            },
            "unresolved": {"text": "Camera sensor size is unknown.", "evidence_message_ids": [11]},
        },
    }


def test_working_set_round_trip_and_correction(tmp_path) -> None:
    payload = _working_extraction()
    extraction = parse_extraction(json.dumps(payload), valid_message_ids={11, 12})
    assert extraction is not None
    state = reduce_situation(None, extraction, session_id="main", source_message_id=12)
    store = ConversationSituationStore(ChatDatabase(tmp_path / "working.db"))
    store.upsert(state)
    assert store.get("main") == state
    payload["working_set"]["facts"][0]["text"] = "The room is large, correcting the earlier report."
    payload["working_set"]["facts"][0]["evidence_message_ids"] = [14]
    payload["evidence_message_ids"].append(14)
    payload["working_set"]["interpretation"] = None
    payload["operation"] = "keep"
    corrected = parse_extraction(json.dumps(payload), valid_message_ids={11, 12, 14})
    revised = reduce_situation(state, corrected, session_id="main", source_message_id=14)
    assert revised.working_set.interpretation is None
    assert "large" in revised.working_set.facts[0].text
    store.upsert(revised)
    store.upsert(state)
    assert store.get("main") == revised
    assert reduce_situation(revised, extraction, session_id="main", source_message_id=12) == revised
    assert reduce_situation(revised, extraction, session_id="other", source_message_id=15) is None


def test_working_set_rejects_untraceable_or_unbounded_claims() -> None:
    assert parse_extraction(
        json.dumps(_working_extraction()), valid_message_ids={11, 12},
        valid_user_message_ids={12},
    ) is None
    payload = _working_extraction()
    payload["working_set"]["interpretation"]["evidence_message_ids"] = [99]
    assert parse_extraction(json.dumps(payload), valid_message_ids={11, 12}) is None
    payload = _working_extraction()
    payload["working_set"]["facts"] *= 4
    assert parse_extraction(json.dumps(payload), valid_message_ids={11, 12}) is None


def test_working_set_is_cleared_on_resolution_or_unconfirmed_update() -> None:
    extraction = parse_extraction(json.dumps(_working_extraction()), valid_message_ids={11, 12})
    state = reduce_situation(None, extraction, session_id="main", source_message_id=12)
    for operation in ("keep", "clear"):
        updated = reduce_situation(
            state, SituationExtraction(operation=operation),
            session_id="main", source_message_id=14,
        )
        assert updated.working_set is None


def test_interest_successor_requires_bounded_answer_evidence() -> None:
    payload = _working_extraction()
    payload["successor"] = {
        "kind": "comparison", "subject": "portrait lenses",
        "change": {"text": "The room size favors the shorter lens.", "evidence_message_ids": [12]},
    }
    parsed = parse_extraction(json.dumps(payload), valid_message_ids={11, 12})
    assert parsed.successor.change.evidence_message_ids == (12,)
    payload["successor"]["change"]["text"] = "What lens would you choose for that room?"
    assert parse_extraction(json.dumps(payload), valid_message_ids={11, 12}) is None
    payload["successor"]["change"]["text"] = "The room size favors the shorter lens."
    payload["successor"]["kind"] = "generic_chat"
    assert parse_extraction(json.dumps(payload), valid_message_ids={11, 12}) is None


def test_parse_rejects_model_confidence_and_unknown_evidence() -> None:
    with_confidence = (
        '{"operation":"replace","summary":"Together on the beanbag.",'
        '"evidence_message_ids":[1],"confidence":0.9}'
    )
    assert parse_extraction(with_confidence, valid_message_ids={1}) is None
    unknown_evidence = (
        '{"operation":"replace","summary":"Together on the beanbag.",'
        '"evidence_message_ids":[99]}'
    )
    assert parse_extraction(unknown_evidence, valid_message_ids={1}) is None


def test_parse_rejects_relative_time_in_stored_text() -> None:
    raw = (
        '{"operation":"replace","summary":"They are watching anime tonight.",'
        '"evidence_message_ids":[1]}'
    )
    assert parse_extraction(raw, valid_message_ids={1}) is None


def test_implicit_clear_requires_two_misses() -> None:
    now = datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)
    state = reduce_situation(
        None,
        _replacement(),
        session_id="main",
        source_message_id=12,
        now=now,
    )
    assert state is not None
    assert state.status == ACTIVE

    missing = SituationExtraction(operation="clear")
    first = reduce_situation(
        state, missing, session_id="main", source_message_id=14, now=now
    )
    assert first is not None
    assert first.status == UNCERTAIN
    second = reduce_situation(
        first, missing, session_id="main", source_message_id=16, now=now
    )
    assert second is not None
    assert second.status == ENDED


def test_explicit_end_clears_immediately() -> None:
    state = reduce_situation(
        None, _replacement(), session_id="main", source_message_id=12
    )
    ended = reduce_situation(
        state,
        SituationExtraction(operation="clear", explicit_end=True),
        session_id="main",
        source_message_id=14,
    )
    assert ended is not None
    assert ended.status == ENDED


def test_store_round_trip_is_per_session(tmp_path) -> None:
    db = ChatDatabase(tmp_path / "chat.db")
    store = ConversationSituationStore(db)
    state = reduce_situation(
        None, _replacement(), session_id="thread-a", source_message_id=12
    )
    assert state is not None
    store.upsert(state)

    loaded = store.get("thread-a")
    assert loaded == state
    assert store.get("thread-b") is None


def test_v41_database_gains_conversation_situation_table(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version(version) VALUES (41)")
    conn.commit()
    conn.close()

    db = ChatDatabase(path)
    tables = {
        row[0]
        for row in db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "conversation_situation" in tables
    assert "live_experience_journal" in tables


class _FakeSituationClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.messages: list[list[dict[str, object]]] = []

    def chat_json(self, messages, **_kwargs):
        self.messages.append(messages)
        return self.responses.pop(0), ChatUsage()


@pytest.mark.parametrize("answer,expected", [
    ("I prefer the shorter lens because the room is small and cramped.", 1),
    ("That sounds good to me, thanks for asking.", 0),
    ("No thanks, stop comparing those portrait lenses and change the topic.", 0),
    ("The weather forecast predicts heavy rainfall across the mountain region next week.", 0),
])
def test_answer_can_earn_only_one_successor(tmp_path, answer, expected) -> None:
    db = ChatDatabase(tmp_path / "successor.db")
    question_id = db.add_message("main", "user", "Can we compare portrait lenses together?")
    payload = _working_extraction()
    payload["evidence_message_ids"] = [question_id]
    payload["working_set"] = {
        "question": {"text": "Compare portrait lenses", "evidence_message_ids": [question_id]},
    }
    previous = reduce_situation(
        None, parse_extraction(json.dumps(payload), valid_message_ids={question_id}),
        session_id="main", source_message_id=question_id,
    )
    answer_id = db.add_message("main", "user", answer)
    output = {
        "operation": "keep", "working_set": None, "evidence_message_ids": [answer_id],
        "successor": {
            "kind": "comparison", "subject": "portrait lenses",
            "change": {
                "text": "The small room favors the shorter portrait lens.",
                "evidence_message_ids": [answer_id],
            },
        },
    }
    store = ConversationSituationStore(db)
    store.upsert(previous)
    cues = CueStore(db)
    worker = ConversationSituationWorker(
        client=_FakeSituationClient([json.dumps(output)] * 2), chat_db=db, store=store,
        model="worker", world_snapshot_provider=dict,
        successor_producer=CueProducer("interest_continuation", lambda: cues),
    )
    assert worker.run("main").working_set is None
    worker.run("main")
    assert cues.count_pending("interest_continuation") == expected
    if expected:
        cue = cues.pending("interest_continuation")[0]
        assert cue.payload["evidence_message_ids"] == [answer_id]
        assert cues.has_source("interest_continuation", cue.payload["source_id"])


@pytest.mark.parametrize("change_session", [False, True])
def test_worker_discards_intervening_input_or_session_change(tmp_path, change_session) -> None:
    db = ChatDatabase(tmp_path / "stale.db")
    message_id = db.add_message("main", "user", "Compare these lenses.")
    token = ["main", 0]

    class ChangingClient:
        def chat_json(self, *_args, **_kwargs):
            if change_session:
                token[0] = "other"
            else:
                db.add_message("main", "user", "Forget the lenses.")
            return json.dumps({
                "operation": "replace", "summary": "Comparing lenses.",
                "evidence_message_ids": [message_id],
            }), ChatUsage()

    store = ConversationSituationStore(db)
    worker = ConversationSituationWorker(
        client=ChangingClient(), chat_db=db, store=store, model="worker",
        world_snapshot_provider=dict, context_token_provider=lambda: tuple(token),
    )
    assert worker.run("main") is None
    assert store.get("main") is None
    assert worker.stats()["stale"] == 1


def test_worker_runs_on_configured_user_turn_cadence(tmp_path) -> None:
    db = ChatDatabase(tmp_path / "worker.db")
    user_id = db.add_message("main", "user", "Come sit with me on the beanbag.")
    assistant_id = db.add_message("main", "assistant", "Settling in beside you.")
    client = _FakeSituationClient(
        [
            (
                '{"operation":"replace","summary":"Aiko and the user are sitting '
                'together on the beanbag.","shared":true,"shared_activity":"talking",'
                '"place_ref":"beanbag","aiko_activity":"sitting with the user",'
                f'"evidence_message_ids":[{user_id},{assistant_id}],"explicit_end":false}}'
            )
        ]
    )
    store = ConversationSituationStore(db)
    worker = ConversationSituationWorker(
        client=client,
        chat_db=db,
        store=store,
        model="small-worker",
        world_snapshot_provider=lambda: {"location": {"slug": "beanbag"}},
        every_n_user_turns=2,
    )
    worker.notify_user_turn("main")
    assert worker.should_run("main") is False
    worker.notify_user_turn("main")
    assert worker.should_run("main") is True
    worker.mark_scheduled("main")

    state = worker.run("main")
    assert state is not None
    assert state.shared is True
    assert store.get("main") == state
    assert "message_id=" in str(client.messages[0][1]["content"])
    assert worker.should_run("main") is False


def test_worker_preserves_previous_state_on_malformed_output(tmp_path) -> None:
    db = ChatDatabase(tmp_path / "fallback.db")
    db.add_message("main", "user", "Still here.")
    previous = reduce_situation(
        None, _replacement(), session_id="main", source_message_id=12
    )
    assert previous is not None
    store = ConversationSituationStore(db)
    store.upsert(previous)
    worker = ConversationSituationWorker(
        client=_FakeSituationClient(["not-json"]),
        chat_db=db,
        store=store,
        model="small-worker",
        world_snapshot_provider=dict,
    )
    assert worker.run("main") == previous
    assert store.get("main") == previous


class _ValueStore:
    def __init__(self, value) -> None:
        self.value = value

    def get(self, _key):
        return self.value


class _TrackingSituationStore(_ValueStore):
    def __init__(self, value) -> None:
        super().__init__(value)
        self.cleared = False

    def upsert(self, value) -> None:
        self.value = value

    def clear(self, _session_id) -> None:
        self.value = None
        self.cleared = True


class _FakeWorld:
    def __init__(self, *, location: str = "beanbag", activity: str = "talking") -> None:
        self.state = SimpleNamespace(
            location_id=4,
            scene_id=1,
            posture="sitting",
            activity=activity,
            updated_at="2026-09-07T20:00:00+00:00",
        )
        self.location = SimpleNamespace(slug=location, name=location.title())
        self.scene = SimpleNamespace(name="Aiko's apartment")

    def get_state(self):
        return self.state

    def get_location_by_id(self, _location_id):
        return self.location

    def current_scene(self):
        return self.scene


class _SnapshotHost(ConversationSituationMixin):
    def __init__(self, state, *, location: str = "beanbag") -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                conversation_situation_stale_seconds=21600.0,
                activity_awareness_enabled=True,
            )
        )
        self._prompt_assembler = SimpleNamespace(_assembly_seq=3)
        self._conversation_situation_store = _ValueStore(state)
        self._world_store = _FakeWorld(location=location)
        self._arc_store = _ValueStore(
            SimpleNamespace(arc="reflection", confidence=0.8)
        )
        self._affect_store = _ValueStore(SimpleNamespace(mood_label="content"))
        self._user_id = "default"
        self._last_turn_mode = "typed"
        self._turn_in_progress = True
        self._connected_clients = 1
        self._user_active_app = "Cursor"
        self._last_user_activity_at = 0.0

    @property
    def session_key(self):
        return "main"

    def vitality_snapshot(self):
        return {"band": "steady"}


def test_snapshot_joins_same_turn_dialogue_and_world_truth() -> None:
    state = reduce_situation(
        None, _replacement(), session_id="main", source_message_id=12
    )
    host = _SnapshotHost(state)
    snapshot = host.conversation_situation_snapshot(
        user_text="What should we watch next?"
    )
    assert snapshot.dialogue_act == "question"
    assert snapshot.input_mode == "typed"
    assert snapshot.world.location_slug == "beanbag"
    assert snapshot.world_compatible is True
    assert snapshot.shared_commitment_active is True
    assert snapshot.floor_owner == "neither"
    assert snapshot.typing_active is False
    assert snapshot.attention_target == "none"
    assert snapshot.live_frame_generation == 0


def test_working_set_renders_as_evidenced_not_authoritative() -> None:
    extraction = parse_extraction(json.dumps(_working_extraction()), valid_message_ids={11, 12})
    state = reduce_situation(None, extraction, session_id="main", source_message_id=12)
    host = _SnapshotHost(state)
    block = host._render_conversation_situation_block("")
    assert "Reported fact [messages 11]" in block
    assert "Tentative, not established [messages 11,12]" in block
    assert "latest user message overrides this" in block


def test_snapshot_text_voice_parity_and_world_conflict() -> None:
    state = reduce_situation(
        None, _replacement(), session_id="main", source_message_id=12
    )
    host = _SnapshotHost(state, location="desk")
    typed = host.conversation_situation_snapshot(input_mode="typed", fresh=True)
    voice = host.conversation_situation_snapshot(input_mode="live", fresh=True)
    assert typed.inferred == voice.inferred
    assert typed.input_mode == "typed"
    assert voice.input_mode == "voice"
    assert voice.world_compatible is False
    assert voice.shared_commitment_active is False
    assert "conflicts" in voice.conflict_reason
    assert "do not present" in host._render_conversation_situation_block("")


def test_stale_inference_cannot_hold_world_or_render() -> None:
    state = reduce_situation(
        None, _replacement(), session_id="main", source_message_id=12
    )
    assert state is not None
    stale = replace(state, updated_at="2026-01-01T00:00:00+00:00")
    host = _SnapshotHost(stale)
    snapshot = host.conversation_situation_snapshot(fresh=True)
    assert snapshot.inferred_stale is True
    assert snapshot.shared_commitment_active is False
    assert host._render_conversation_situation_block("") == ""


def test_conversation_situation_block_is_registered_in_t6() -> None:
    assert "conversation_situation_block" in _PROMPT_BLOCK_TIERS["T6_detectors"]
    assert "conversation_situation_block" not in _OFFERS


def test_conversation_situation_t6_block_survives_aggressive_assembly(
    tmp_path,
) -> None:
    db = ChatDatabase(tmp_path / "prompt.db")
    persona = tmp_path / "persona.txt"
    persona.write_text("Aiko persona.", encoding="utf-8")
    assembler = PromptAssembler(db, persona_path=persona)
    assembler.set_inner_life_providers(
        conversation_situation=lambda user_text: (
            f"[Conversation situation]\nShared context for {user_text}."
        )
    )

    messages, telemetry = assembler.assemble_with_budget(
        "main",
        "this turn",
        context_window=4096,
        response_budget=512,
        aggressive=True,
    )
    assert "Shared context for this turn." in str(messages[0]["content"])
    assert telemetry.block_chars["conversation_situation_block"] > 0


class _MoverWorld:
    def __init__(self, *, in_garden: bool = False) -> None:
        self.garden = SimpleNamespace(id=9, slug="garden", name="Garden")
        self.desk = SimpleNamespace(id=2, slug="desk", name="Desk")
        self.state = SimpleNamespace(
            location_id=9 if in_garden else 4,
            scene_id=1,
            posture="sitting",
            activity="talking",
            updated_at="2026-09-01T10:00:00+00:00",
        )

    def get_location(self, slug):
        if slug == "garden":
            return self.garden
        if slug == "desk":
            return self.desk
        return None

    def get_state(self):
        return self.state


def _active_shared_guard() -> WorldMutationGuard:
    old_stamp = "2026-09-01T10:00:00+00:00"
    return WorldMutationGuard(
        kv_get=lambda _key: old_stamp,
        situation_snapshot_provider=lambda: SimpleNamespace(
            shared_commitment_active=True,
            generation=7,
        ),
        intentional_hold_seconds=7200.0,
    )


def test_shared_beanbag_blocks_all_three_autonomous_movers() -> None:
    guard = _active_shared_guard()
    now = datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)
    away = IdleAwayActivityWorker(
        world_store=_MoverWorld(),
        kv_get=lambda _key: None,
        kv_set=lambda _key, _value: None,
        user_display_name_provider=lambda: "Jacob",
        cooldown_seconds=0,
        mutation_guard=guard,
    )
    away_demand = away.demand(now=now, last_run_at=None)
    assert away_demand is not None
    assert away_demand.pressure == 0.0
    assert away_demand.reason == "active_shared_conversation_situation"
    assert away.run()["skipped_active_shared_conversation_situation"] is True

    garden = GardenVisitWorker(
        _MoverWorld(),
        mutation_guard=guard,
        circadian_period_provider=lambda: "afternoon",
    )
    garden_demand = garden.demand(now=now, last_run_at=None)
    assert garden_demand is not None
    assert garden_demand.pressure == 0.0
    assert garden.run()["reason"] == "active_shared_conversation_situation"

    circadian = CircadianSettleWorker(
        _MoverWorld(),
        mutation_guard=guard,
        circadian_period_provider=lambda: "night",
        settle_after_seconds=0,
    )
    circadian_demand = circadian.demand(now=now, last_run_at=None)
    assert circadian_demand is not None
    assert circadian_demand.pressure == 0.0
    assert circadian.run()["skipped_active_shared_conversation_situation"] is True


def test_world_guard_allows_item_only_work() -> None:
    decision = _active_shared_guard().check_autonomous({"item"})
    assert decision.allowed is True
    assert decision.reason == "item_only"


def test_garden_auto_return_is_cancelled_by_shared_scene() -> None:
    garden = GardenVisitWorker(
        _MoverWorld(in_garden=True),
        mutation_guard=_active_shared_guard(),
    )
    result = garden.run()
    assert result["cancelled_shared_situation"] is True
    assert garden._load_return_at() is None


def test_deliberate_incompatible_world_move_releases_commitment() -> None:
    state = reduce_situation(
        None, _replacement(), session_id="main", source_message_id=12
    )
    host = _SnapshotHost(state, location="desk")
    store = _TrackingSituationStore(state)
    host._conversation_situation_store = store

    host._reconcile_conversation_situation_after_world_mutation()
    assert store.cleared is True
    assert store.value is None


def test_mcp_diagnostic_uses_public_situation_facade() -> None:
    tools = {}

    class _Mcp:
        @staticmethod
        def tool():
            def decorator(func):
                tools[func.__name__] = func
                return func

            return decorator

    expected = {"snapshot": {"generation": 4}, "worker": {"completed": 2}}
    session = SimpleNamespace(
        conversation_situation_diagnostics=lambda: expected
    )
    conversation_situation_tools.register(_Mcp(), session)
    assert json.loads(tools["get_conversation_situation"]()) == expected
