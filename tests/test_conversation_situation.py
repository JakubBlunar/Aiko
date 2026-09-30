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
from app.core.conversation.delivery import DeliveryLedger
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
from app.web.ws_live_commands import handle_live_ws_command


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


def test_media_thread_progress_correction_and_start_are_bounded() -> None:
    from app.core.conversation.media_thread import project_thread

    thread = project_thread(None, 'I finished chapter 4 of "Glass Harbor".', 10)
    assert thread is not None and thread.completed_through == 4
    started = project_thread(thread, 'I started "Glass Harbor" chapter 5.', 11)
    assert started.completed_through == 4
    corrected = project_thread(started, 'Actually, I only finished chapter 2.', 12)
    assert corrected.completed_through == 2
    assert project_thread(corrected, 'I finished chapter 8.', 9) == corrected
    assert project_thread(None, "I want to read chapter 4 of Glass Harbor.", 10) is None
    assert project_thread(None, "I have not finished chapter 4 of Glass Harbor.", 10) is None
    assert project_thread(None, 'She said "I finished chapter 4 of Glass Harbor".', 10) is None
    tonight = project_thread(None, "I started North Station ep 4 tonight", 10)
    assert tonight.title == "North Station" and tonight.completed_through == 3
    switched = project_thread(thread, "I finished episode 1 of North Station.", 13)
    assert switched.title == "North Station" and switched.exchanges == ()


def test_media_store_is_read_only_during_render_and_survives_restart(tmp_path) -> None:
    from app.core.conversation.media_thread import MediaThreadStore

    db = ChatDatabase(tmp_path / "media.db")
    store = MediaThreadStore(db, "media")
    user = db.add_message("media", "user", "I finished chapter 4 of Glass Harbor.")
    assistant = db.add_message(
        "media", "assistant", "My reading is that the narrator is unreliable.",
    )
    store.record(user, assistant)
    before = db.kv_get(store.key)
    block = store.render("I started Glass Harbor chapter 5.")
    assert "completed through chapter 4" in block
    assert "Aiko previously proposed" in block
    assert db.kv_get(store.key) == before
    assert store.render("Let's discuss coffee.") == ""
    assert store.render("Please stop tracking this book.") == ""
    assert MediaThreadStore(db, "other").render("What about Glass Harbor?") == ""
    restored = MediaThreadStore(db, "media")
    assert restored.load() == store.load()
    store.record(user, assistant)
    assert db.kv_get(store.key) == before
    correction = db.add_message("media", "user", "Actually, I only finished chapter 2.")
    store.record(correction)
    corrected = store.render("What do you think about Glass Harbor?")
    assert "completed through chapter 2" in corrected
    assert "unreliable" not in corrected
    stop = db.add_message("media", "user", "Stop tracking this book.")
    store.record(stop)
    store.record(correction)
    assert store.render("Glass Harbor") == ""


def test_media_cross_session_progress_is_user_scoped_and_correctable(tmp_path) -> None:
    from app.core.conversation.media_thread import MediaThreadStore

    db = ChatDatabase(tmp_path / "cross-media.db")
    first = MediaThreadStore(db, "first", user_id="reader")
    source = db.add_message("first", "user", "I finished chapter 4 of Glass Harbor.")
    reply = db.add_message("first", "assistant", "My reading is that the narrator is unreliable.")
    first.record(source, reply)
    second = MediaThreadStore(db, "second", user_id="reader")
    before = db.kv_get(second.key)
    assert "unreliable" in second.render("What about Glass Harbor?")
    assert db.kv_get(second.key) == before
    assert MediaThreadStore(db, "second", user_id="someone-else").load() is None
    ambiguous = db.add_message("second", "user", "I finished chapter 9.")
    second.record(ambiguous)
    assert second.load().completed_through == 4
    correction = db.add_message("second", "user", "I only finished chapter 2 of Glass Harbor.")
    second.record(correction)
    assert second.load().completed_through == 2
    assert "unreliable" not in second.render("Glass Harbor")
    first.record(source, reply)
    assert first.load().completed_through == 2
    stop = db.add_message("second", "user", "Stop tracking this book.")
    second.record(stop)
    assert first.render("Glass Harbor") == ""
    first.record(source, reply)
    assert first.load().active is False


def test_media_discussion_retains_changed_interpretations_without_wrong_speakers(tmp_path) -> None:
    from app.core.conversation.media_thread import MediaThreadStore

    db = ChatDatabase(tmp_path / "media-evidence.db")
    host = ConversationSituationMixin()
    host._chat_db = db
    host.session_key = "media"
    user = db.add_message("media", "user", "I finished chapter 2 of Glass Harbor.")
    first = db.add_message("media", "assistant", "My initial interpretation is a deliberate lie.")
    host._record_media_exchange(user, first)
    answer = db.add_message("media", "user", "Glass Harbor shows the narrator lacks the letter.")
    revision = db.add_message(
        "media", "assistant", "That changes my reading to a misunderstanding.",
    )
    host._record_media_exchange(answer, revision)
    block = host._render_media_context_block("What is your reading of Glass Harbor?")
    assert "deliberate lie" in block and "misunderstanding" in block
    assert "User said" in block and "Aiko previously proposed" in block
    foreign = db.add_message("other", "user", "I finished chapter 9 of Glass Harbor.")
    store = MediaThreadStore(db, "media")
    store.record(foreign)
    store.record(revision)
    assert store.load().completed_through == 2
    short = db.add_message("media", "user", 'I finished chapter 1 of "It".')
    store.record(short)
    assert store.render("It was a good lunch.") == ""
    assert "completed through chapter 1" in store.render('What about "It"?')


def test_durable_media_rejects_missing_or_wrong_session_source(tmp_path) -> None:
    from app.core.conversation.media_thread import MediaThreadStore

    db = ChatDatabase(tmp_path / "media-source.db")
    store = MediaThreadStore(db, "original", user_id="reader")
    source = db.add_message("original", "user", "I finished episode 3 of North Station.")
    store.record(source)
    valid = db.kv_get(store.key)
    payload = json.loads(valid)
    payload["source_session_id"] = "wrong-session"
    db.kv_set(store.key, json.dumps(payload))
    assert store.load() is None
    db.kv_set(store.key, valid)
    db.delete_session("original")
    assert MediaThreadStore(db, "new", user_id="reader").load() is None


def test_delivery_receipts_require_clip_owner_scope_and_send_completion() -> None:
    scope = ["main", 1]
    ledger = DeliveryLedger(lambda: tuple(scope))
    response = ledger.begin("main")
    ledger.set_spoken_context(response, "The first sentence.")
    first = ledger.offer_audio("owner")
    assert not ledger.receipt(first, "played", "owner", "owner")
    ledger.end_audio(first)
    assert not ledger.receipt(first, "played", "other", "owner")
    assert ledger.receipt(first, "played", "owner", "owner")
    assert not ledger.receipt(first, "played", "owner", "owner")
    ledger.set_spoken_context(response, "The key second sentence.")
    second = ledger.offer_audio("owner")
    ledger.end_audio(second)
    ledger.cancel_audio()
    assert not ledger.receipt(second, "played", "owner", "owner")
    ledger.finish(response, 10)
    assert "audio clips completed 1/2" in ledger.render()
    assert "interrupted=True" in ledger.render()
    assert "not proof of hearing" in ledger.render()
    scope[1] = 2
    assert not ledger.receipt(second, "played", "owner", "owner")
    assert ledger.render() == ""


def test_text_delivery_and_explicit_acknowledgement_are_distinct() -> None:
    ledger = DeliveryLedger(lambda: ("main", 1))
    response = ledger.begin("main")
    ledger.finish(response, 12)
    assert "text presentation unknown" in ledger.render()
    assert not ledger.receipt(response, "text_presented", "owner", "owner")
    assert ledger.offer_text(12, "owner") == response
    assert ledger.receipt(response, "text_presented", "owner", "owner")
    assert "explicit acknowledgement=False" in ledger.render()
    ledger.acknowledge(12)
    assert "explicit acknowledgement=True" in ledger.render()
    for message_id in range(20, 25):
        ledger.finish(ledger.begin("main"), message_id)
    assert not ledger.receipt(response, "text_presented", "owner", "owner")


def test_delivery_websocket_rejects_old_owner_and_old_generation() -> None:
    scope = ["main", 1]
    ledger = DeliveryLedger(lambda: tuple(scope))
    response = ledger.begin("main")
    ledger.set_spoken_context(response, "An identified sentence.")
    clip = ledger.offer_audio("first-window")
    ledger.end_audio(clip)
    ledger.finish(response, 24)
    drains = []
    session = SimpleNamespace(
        delivery_ledger=lambda: ledger, notify_playback_drained=lambda: drains.append(True),
    )
    hub = SimpleNamespace(audio_owner_id="second-window")
    message = {"delivery_id": clip, "state": "played"}
    for client_id in ("first-window", "second-window"):
        assert handle_live_ws_command(
            session, "delivery_receipt", message, client_id=client_id, hub=hub,
        )
    assert "audio clips completed 0/1" in ledger.render()
    handle_live_ws_command(
        session, "playback_drained", {}, client_id="first-window", hub=hub,
    )
    assert drains == []
    hub.audio_owner_id = "first-window"
    scope[1] = 2
    handle_live_ws_command(
        session, "delivery_receipt", message, client_id="first-window", hub=hub,
    )
    scope[1] = 1
    assert "audio clips completed 0/1" in ledger.render()


def test_text_only_receipt_does_not_invent_audio_or_understanding() -> None:
    ledger = DeliveryLedger(lambda: ("main", 1))
    response = ledger.begin("main")
    ledger.finish(response, 25)
    ledger.offer_text(25, "*")
    assert ledger.receipt(response, "text_presented", "text-window", "")
    assert "audio delivery unknown" in ledger.render()
    assert "explicit acknowledgement=False" in ledger.render()


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
    payload["working_set"]["recall_needed"] = True
    extraction = parse_extraction(json.dumps(payload), valid_message_ids={11, 12})
    assert extraction is not None
    assert extraction.working_set.recall_needed
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
        self.calls: list[dict[str, object]] = []

    def chat_json(self, messages, **_kwargs):
        self.messages.append(messages)
        self.calls.append(_kwargs)
        return self.responses.pop(0), ChatUsage()


def test_judgment_shadow_validates_evidence_and_retains_only_references(tmp_path) -> None:
    from app.core.conversation.judgment_shadow import (
        JudgmentShadowStore, comparisons, heuristic_replay, parse_review, target_exchange,
    )

    db = ChatDatabase(tmp_path / "shadow.db")
    user_id = db.add_message("main", "user", "That rules out the database.")
    assistant_id = db.add_message("main", "assistant", "Then inspect the request handler.")
    rows = db.get_messages("main", limit=14)
    target = target_exchange(rows)
    raw = json.dumps({"judgment_shadow": {
        "progress": {"value": "progressing", "evidence": [
            {"message_id": user_id, "quote": "rules out the database"},
        ]},
        "coverage": {"value": "answered", "evidence": [
            {"message_id": user_id, "quote": "That rules out"},
            {"message_id": assistant_id, "quote": "inspect the request handler"},
        ]},
        "need": {"value": "witness", "evidence": [
            {"message_id": user_id, "quote": "invented quote"},
        ]},
    }})
    review = parse_review(raw, rows, target)
    assert review["status"] == "partial"
    assert review["invalid_fields"] == ["need"]
    assert review["observations"]["progress"]["evidence"][0]["start"] == 5
    replay = heuristic_replay(rows, target)
    assert comparisons(review, replay)["progress"] == "disagree"
    store = JudgmentShadowStore(db)
    record = {**review, "user_message_id": user_id, "assistant_message_id": assistant_id,
              "comparisons": comparisons(review, replay), "heuristic": replay}
    assert store.record("main", record)
    assert not store.record("main", record)
    report = JudgmentShadowStore(db).report("main", include_rows=True)
    assert report["retained_runs"] == 1
    assert "database" not in json.dumps(report)
    assert "rows" not in store.report("main")
    assert store.report("other")["retained_runs"] == 0
    expanded = store.report("main", include_evidence=True)["rows"][0]
    assert expanded["observations"]["progress"]["evidence"][0]["quote"] == "rules out the database"
    assert "database" not in db.kv_get("judgment_shadow:main")


@pytest.mark.parametrize("max_tokens", [None, 2048])
def test_worker_budget_covers_expanded_working_set(tmp_path, max_tokens) -> None:
    db = ChatDatabase(tmp_path / "expanded.db")
    user_id = db.add_message("main", "user", "Compare portrait lenses for a small room.")
    payload = _working_extraction()
    payload["evidence_message_ids"] = [user_id]
    working = payload["working_set"]
    working["facts"].extend([
        {"text": "The user wants indoor portraits."},
        {"text": "The user is comparing two lenses."},
    ])
    working["recall_needed"] = True
    for note in [working["question"], *working["facts"],
                 working["interpretation"], working["unresolved"]]:
        note["evidence_message_ids"] = [user_id]
    client = _FakeSituationClient([json.dumps(payload)])
    worker = ConversationSituationWorker(
        client=client, chat_db=db, store=ConversationSituationStore(db),
        model="worker", world_snapshot_provider=dict,
        **({"max_tokens": max_tokens} if max_tokens is not None else {}),
    )
    state = worker.run("main")
    assert state is not None and state.working_set is not None
    assert len(state.working_set.facts) == 3
    assert state.working_set.recall_needed
    assert client.calls[0]["options"]["num_predict"] == (max_tokens or 1536)
    assert client.calls[0]["think"] is False
    assert worker.stats()["output_limit_hits"] == 0


@pytest.mark.parametrize("shadow", [None, {}, {"need": "invalid"}])
def test_shadow_is_one_call_and_never_enters_situation_state(tmp_path, shadow) -> None:
    db = ChatDatabase(tmp_path / "shadow_worker.db")
    user_id = db.add_message("main", "user", "That rules out the database.")
    assistant_id = db.add_message("main", "assistant", "Inspect the request handler.")
    payload = {"operation": "replace", "summary": "Investigating a request failure.",
               "evidence_message_ids": [user_id, assistant_id]}
    if shadow is not None:
        payload["judgment_shadow"] = shadow
    client = _FakeSituationClient([json.dumps(payload)] * 2)
    worker = ConversationSituationWorker(
        client=client, chat_db=db, store=ConversationSituationStore(db),
        model="worker", world_snapshot_provider=dict,
    )
    state = worker.run("main")
    assert state is not None and state.summary == payload["summary"]
    assert "judgment_shadow" not in json.dumps(state.to_payload())
    assert len(client.calls) == 1
    assert client.calls[0]["options"]["num_predict"] == 1536 + 1024
    assert "SHADOW TARGET" in client.messages[0][1]["content"]
    assert "text_only_replay" not in client.messages[0][1]["content"]
    report = worker.judgment_shadow_report("main", include_rows=True)
    assert report["retained_runs"] == 1
    assert report["rows"][0]["assistant_message_id"] == assistant_id
    worker.run("main")
    assert "SHADOW TARGET" not in client.messages[1][1]["content"]
    assert worker.judgment_shadow_report("main")["retained_runs"] == 1


def test_worker_records_output_limit_without_overwriting_valid_state(tmp_path) -> None:
    db = ChatDatabase(tmp_path / "truncated.db")
    db.add_message("main", "user", "Still comparing the lenses.")
    previous = reduce_situation(None, _replacement(), session_id="main", source_message_id=12)
    store = ConversationSituationStore(db)
    store.upsert(previous)

    class TruncatedClient:
        def chat_json(self, *_args, **_kwargs):
            return '{"operation":', ChatUsage(done_reason="length")

    worker = ConversationSituationWorker(
        client=TruncatedClient(), chat_db=db, store=store, model="worker",
        world_snapshot_provider=dict,
    )
    assert worker.run("main") == previous
    assert store.get("main") == previous
    assert worker.stats()["output_limit_hits"] == 1
    assert worker.stats()["invalid"] == 1
    assert worker.stats("main")["last_result"]["output_limit_hit"] is True


@pytest.mark.parametrize("field,value", [
    ("coverage", "answered"), ("progress", "progressing"), ("need", "problem_solve"),
    ("attunement", "matched"), ("depth", "appropriate"), ("completion", "open"),
])
def test_shadow_all_facets_require_valid_target_evidence(tmp_path, field, value) -> None:
    from app.core.conversation.judgment_shadow import parse_review, target_exchange

    db = ChatDatabase(tmp_path / "facets.db")
    user_id = db.add_message("main", "user", "Can you help me find the cause?")
    assistant_id = db.add_message("main", "assistant", "Check the error log first.")
    rows = db.get_messages("main", limit=14)
    target = target_exchange(rows)
    evidence = [{"message_id": user_id, "quote": "help me find the cause"},
                {"message_id": assistant_id, "quote": "Check the error log"}]
    review = parse_review(json.dumps({"judgment_shadow": {
        field: {"value": value, "evidence": evidence},
    }}), rows, target)
    assert review["observations"][field]["value"] == value
    assert not review["invalid_fields"]


@pytest.mark.parametrize("bad_evidence", [
    [{"message_id": True, "quote": "help"}],
    [{"message_id": 999, "quote": "help"}],
    [{"message_id": 1, "quote": "invented"}],
    [{"message_id": 2, "quote": "reply"}],
    [{"message_id": 3, "quote": "future"}],
    [{"message_id": 4, "quote": "foreign"}],
    [{"message_id": 1, "quote": " "}],
    [],
])
def test_shadow_rejects_wrong_speaker_window_session_and_quotes(tmp_path, bad_evidence) -> None:
    from app.core.conversation.judgment_shadow import parse_review, target_exchange

    db = ChatDatabase(tmp_path / "bad_evidence.db")
    db.add_message("main", "user", "help")
    db.add_message("main", "assistant", "reply")
    target = target_exchange(db.get_messages("main", limit=14))
    db.add_message("main", "user", "future")
    foreign = db.add_message("other", "user", "foreign")
    rows = db.get_messages("main", limit=14) + [db.get_message_row(foreign)]
    review = parse_review(json.dumps({"judgment_shadow": {
        "need": {"value": "witness", "evidence": bad_evidence},
    }}), rows, target)
    assert review["invalid_fields"] == ["need"]
    assert review["observations"] == {}


@pytest.mark.parametrize("failure", [
    "stale", "session", "edited", "truncated", "call", "storage", "disabled",
])
def test_shadow_failures_do_not_write_live_judgments(tmp_path, monkeypatch, failure) -> None:
    db = ChatDatabase(tmp_path / "shadow_failures.db")
    user_id = db.add_message("main", "user", "Can we compare lenses?")
    assistant_id = db.add_message("main", "assistant", "Start with focal length.")
    payload = {"operation": "replace", "summary": "Comparing lenses.",
               "evidence_message_ids": [user_id, assistant_id], "judgment_shadow": {}}
    token = ["main", 0]

    class Client:
        calls = 0

        def chat_json(self, messages, **kwargs):
            self.calls += 1
            if failure == "disabled":
                assert "judgment_shadow" not in messages[0]["content"]
                assert kwargs["options"]["num_predict"] == 1536
            if failure == "call":
                raise RuntimeError("test failure")
            if failure == "stale":
                db.add_message("main", "user", "Change the subject.")
            if failure == "session":
                token[0] = "other"
            if failure == "edited":
                db.update_message_content(user_id, "Never mind.")
            return json.dumps(payload), ChatUsage(
                done_reason="length" if failure == "truncated" else "stop",
                prompt_tokens=100, completion_tokens=80,
            )

    client = Client()
    worker = ConversationSituationWorker(
        client=client, chat_db=db, store=ConversationSituationStore(db),
        model="worker", world_snapshot_provider=dict, judgment_shadow_enabled=failure != "disabled",
        context_token_provider=lambda: tuple(token),
    )
    if failure == "storage":
        def fail_write(*_args):
            raise RuntimeError("test storage failure")
        monkeypatch.setattr(db, "kv_set", fail_write)
    state = worker.run("main")
    assert client.calls == 1
    report = worker.judgment_shadow_report("main", include_rows=True)
    if failure in {"storage", "disabled"}:
        assert state is not None
        assert report["retained_runs"] == 0
    else:
        assert report["retained_runs"] == 1
        assert report["rows"][0]["status"] == "discarded"
        assert report["rows"][0]["observations"] == {}
    if failure in {"stale", "session", "call"}:
        assert state is None
    if failure == "truncated":
        assert report["output_limit_hits"] == 1
        assert report["rows"][0]["completion_tokens"] == 80


def test_shadow_retention_abstentions_and_deleted_evidence(tmp_path, monkeypatch) -> None:
    from app.core.conversation import judgment_shadow

    monkeypatch.setattr(judgment_shadow, "MAX_RUNS", 2)
    db = ChatDatabase(tmp_path / "retention.db")
    store = judgment_shadow.JudgmentShadowStore(db)
    for index in range(3):
        user_id = db.add_message("main", "user", "Thanks.")
        assistant_id = db.add_message("main", "assistant", "Welcome.")
        rows = db.get_messages("main", limit=14)
        review = judgment_shadow.parse_review(
            json.dumps({"judgment_shadow": {"need": None, "progress": {
                "value": "unclear", "evidence": [{"message_id": user_id, "quote": "Thanks"}],
            }}}), rows, judgment_shadow.target_exchange(rows),
        )
        assert store.record("main", {
            **review, "user_message_id": user_id, "assistant_message_id": assistant_id,
            "comparisons": judgment_shadow.comparisons(review, {}), "call_ms": index,
        })
    report = store.report("main", include_rows=True, limit=1)
    assert report["retained_runs"] == 2
    assert len(report["rows"]) == 1
    assert report["comparisons"]["need:abstained"] == 2
    assert report["comparisons"]["coverage:not_reported"] == 2
    db.update_message_content(user_id, "Edited.")
    evidence = store.report("main", include_evidence=True)["rows"][-1]
    assert evidence["observations"]["progress"]["evidence"][0]["quote"] is None
    db.clear_messages("main")
    assert store.report("main")["retained_runs"] == 0


@pytest.mark.parametrize("enabled", [None, False, True])
def test_judgment_shadow_setting_round_trips(tmp_path, enabled) -> None:
    from pathlib import Path
    from app.core.infra.settings import load_settings

    defaults = Path(__file__).resolve().parents[1] / "config" / "default.json"
    payload = json.loads(defaults.read_text(encoding="utf-8"))
    payload["agent"].pop("conversation_judgment_shadow_enabled", None)
    if enabled is not None:
        payload["agent"]["conversation_judgment_shadow_enabled"] = enabled
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_settings(path).agent.conversation_judgment_shadow_enabled is (enabled is not False)


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


def test_recall_need_requires_a_fresh_open_goal_and_current_topic() -> None:
    payload = _working_extraction()
    payload["working_set"]["recall_needed"] = True
    extraction = parse_extraction(json.dumps(payload), valid_message_ids={11, 12})
    state = reduce_situation(None, extraction, session_id="main", source_message_id=12)
    host = _SnapshotHost(state)
    assert host.recall_information_need("Which lens?")["family"] == "recall"
    assert not host.recall_information_need("Tell me about tomorrow's weather")
    assert not host.recall_information_need("Forget about that lens")
    assert not host.recall_information_need("That lens is intended for a full-frame camera")
    stale = _SnapshotHost(replace(state, updated_at="2000-01-01T00:00:00+00:00"))
    assert not stale.recall_information_need("Which lens?")



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
    shadow_calls = []

    def shadow_report(**kwargs):
        shadow_calls.append(kwargs)
        return {"mode": "shadow", "retained_runs": 2}

    session = SimpleNamespace(
        conversation_situation_diagnostics=lambda: expected,
        conversation_judgment_shadow_diagnostics=shadow_report,
    )
    conversation_situation_tools.register(_Mcp(), session)
    assert json.loads(tools["get_conversation_situation"]()) == expected
    report = json.loads(tools["get_conversation_judgment_shadow"]())
    assert report == {"mode": "shadow", "retained_runs": 2}
    assert shadow_calls[-1] == {"include_rows": False, "include_evidence": False, "limit": 20}
    tools["get_conversation_judgment_shadow"](include_evidence=True, limit=3)
    assert shadow_calls[-1]["include_evidence"] is True
    assert shadow_calls[-1]["limit"] == 3
