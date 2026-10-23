from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.infra import timephrase

from app.core.conversation.conversation_situation import (
    ConversationSituationStore, SituationExtraction, parse_extraction, reduce_situation,
)
from app.core.conversation.conversation_situation_worker import ConversationSituationWorker
from app.core.conversation.social_understanding import current_repair_block, social_context_block
from app.core.infra.chat_database import ChatDatabase
from app.core.session.conversation_situation_mixin import ConversationSituationMixin
from app.core.session.prompt_assembler import PromptAssembler, _BLOCK_TIER_OF
from app.llm.chat_client import ChatUsage
from scripts.evaluate_social_reasoning import CORPUS, probe, score_response

_NOW = timephrase.utcnow()

def note(text, *ids):
    return {"text": text, "evidence_message_ids": list(ids)}


def proposal():
    return {
        "operation": "replace", "summary": "The server referent was corrected.",
        "evidence_message_ids": [1, 2, 3],
        "pragmatic_frame": {
            "referents": note("Birch is staging; Atlas is production.", 1, 3),
            "scope": note("The servers are literal, not a pretend scene.", 1),
            "reading": note("The user is correcting the server, not rejecting Aiko.", 3),
            "alternative": None,
        },
        "repair_target": {
            "target": note("Aiko proposed inspecting Atlas.", 2),
            "correction": note("The user meant Birch; production must stay untouched.", 3),
            "replacement": note("Inspect Birch only; Atlas remains untouched.", 3),
        },
    }


def parse(payload):
    return parse_extraction(json.dumps(payload), valid_message_ids={1, 2, 3, 4},
                            valid_user_message_ids={1, 3})


def state():
    return reduce_situation(None, parse(proposal()), session_id="synthetic", source_message_id=4,
                            now=_NOW)


@pytest.mark.parametrize("field,value,reason", [
    ("pragmatic_frame", [], "pragmatic_frame.shape"),
    ("repair_target", [], "repair_target.shape"),
    ("repair_target.correction", note("Assistant guess.", 2),
     "repair_target.correction_not_later"),
    ("repair_target.correction", note("Assistant guess.", 4),
     "repair_target.correction.user_evidence_missing"),
    ("repair_target.replacement", note("Unsupported replacement.", 1),
     "repair_target.replacement.correction_evidence_missing"),
    ("pragmatic_frame.reading", note("Unknown source.", 99), "pragmatic_frame.evidence_id"),
    ("pragmatic_frame.reading", note("The user is joking today.", 3),
     "pragmatic_frame.relative_time"),
])
def test_social_notes_reject_unscoped_or_wrong_speaker_evidence(field, value, reason):
    payload = proposal()
    payload["evidence_message_ids"].append(4)
    parts = field.split(".")
    destination = payload if len(parts) == 1 else payload[parts[0]]
    destination[parts[-1]] = value
    reasons = []
    parsed = parse_extraction(json.dumps(payload), valid_message_ids={1, 2, 3, 4},
                              valid_user_message_ids={1, 3}, rejection_reasons=reasons)
    assert parsed is not None
    assert getattr(parsed, parts[0]) is None
    assert reasons == [reason]


def test_social_notes_share_top_level_evidence_envelope():
    payload = proposal()
    payload["evidence_message_ids"] = [1, 2]
    parsed = parse(payload)
    assert parsed is not None and parsed.pragmatic_frame is None and parsed.repair_target is None


def test_bad_optional_frame_preserves_valid_repair_and_working_constraints():
    payload = proposal()
    payload["pragmatic_frame"]["scope"] = note("An ungrounded placeholder.")
    payload["working_set"] = {
        "question": note("Which staging server should be inspected?", 1),
        "constraints": [note("Production must stay untouched.", 3)],
    }
    parsed = parse(payload)
    assert parsed.pragmatic_frame is None
    assert parsed.repair_target is not None
    assert parsed.working_set.constraints[0].text == "Production must stay untouched."


@pytest.mark.parametrize("operation,explicit", [("keep", False), ("clear", False),
                                                 ("clear", True), ("replace", False)])
def test_omission_resolution_or_pivot_retires_social_notes(operation, explicit):
    old = state()
    extraction = SituationExtraction(operation, summary="A new subject.",
                                    evidence_message_ids=(3,), explicit_end=explicit)
    updated = reduce_situation(old, extraction, session_id="synthetic", source_message_id=5)
    assert updated.pragmatic_frame is None
    assert updated.repair_target is None
    assert old.repair_target is not None  # immutable prior evidence


def test_round_trip_uses_existing_situation_row_without_memory_or_relationship_writes(tmp_path):
    db = ChatDatabase(tmp_path / "state.db")
    store = ConversationSituationStore(db)
    store.upsert(state())
    assert store.get("synthetic") == state()
    assert store.get("other") is None
    assert db.execute_fetchall("SELECT * FROM memories") == []
    assert db.execute_fetchall("SELECT * FROM kv_meta") == []
    store.clear("synthetic")
    assert store.get("synthetic") is None


def fill_db(tmp_path):
    db = ChatDatabase(tmp_path / "turn.db")
    for role, text in (
        ("user", "Atlas is production, Birch is staging; both run Linux."),
        ("assistant", "Let's inspect Atlas."),
        ("user", "I meant Birch, the other server. Production must stay untouched."),
        ("assistant", "Which timeout on Birch?"),
    ):
        db.add_message("synthetic", role, text)
    return db


class Host(ConversationSituationMixin):
    session_key = "synthetic"

    def __init__(self, db):
        self._chat_db = db
        self._settings = SimpleNamespace(agent=SimpleNamespace())
        self._conversation_situation_store = ConversationSituationStore(db)


def test_current_correction_guides_its_own_reply_and_does_not_reopen_on_pivot(tmp_path):
    db = fill_db(tmp_path)
    db.execute_commit("DELETE FROM messages WHERE id = 4")
    host = Host(db)
    assembler = PromptAssembler(db, continuity_max_messages=0)
    host._prompt_assembler = assembler
    assembler.set_inner_life_providers(
        conversation_situation=host._render_conversation_situation_block,
    )
    correction = db.get_message_row(3).content
    messages, telemetry = assembler.assemble_with_budget(
        "synthetic", correction, context_window=32768, response_budget=512,
    )
    assert "Current conversational repair" in telemetry.system_prompt
    assert '"earlier_assistant_message_id": 2' in telemetry.system_prompt
    assert '"correction_message_id": 3' in telemetry.system_prompt
    assert "Reading context and repairing a misunderstanding:" in telemetry.system_prompt
    assert messages[-1]["role"] == "user"
    assert _BLOCK_TIER_OF["conversation_situation_block"] == "T6_detectors"
    db.add_message("synthetic", "assistant", "Birch, staging. Atlas stays untouched.")
    _, following = assembler.assemble_with_budget(
        "synthetic", "Anyway, tell me about jigsaws.", context_window=32768, response_budget=512,
    )
    assert "Current conversational repair" not in following.system_prompt
    assert "Reading context and repairing a misunderstanding:" not in following.system_prompt
    assert host._conversation_situation_store.get("synthetic") is None


@pytest.mark.parametrize("disabled", ["clarification_repair_enabled",
                                      "conversation_situation_enabled", "unavailable_store"])
def test_immediate_repair_respects_settings_and_legacy_fallback(tmp_path, disabled):
    host = Host(fill_db(tmp_path))
    if disabled == "unavailable_store":
        host._conversation_situation_store = None
    else:
        setattr(host._settings.agent, disabled, False)
    assert host._render_conversation_situation_block("I meant Birch, the other server.") == ""


@pytest.mark.parametrize("text,expected", [
    ("I meant Birch, the other server.", True),
    ("I was joking.", True),
    ("Sorry, what? I couldn't hear the second step.", True),
    ("Myslel som Brezu, nie Atlas.", True),
    ("I meant well, sorry.", False),
    ('Mira said "I meant the other server." What did she mean?', False),
    ("Mira said 'I was joking.'", False),
    ("`you misunderstood` is a sample string.", False),
    ("Hello!", False),
    ("Thanks, good night.", False),
])
def test_immediate_gate_is_local_and_ignores_quoted_corrections(tmp_path, text, expected):
    db = fill_db(tmp_path)
    before = db.execute_fetchall("SELECT * FROM messages")
    assert bool(current_repair_block(text, db, "synthetic")) is expected
    assert current_repair_block(text, db, "other") == ""
    assert db.execute_fetchall("SELECT * FROM messages") == before


def test_social_context_drops_deleted_sources_and_has_a_bounded_lease(tmp_path):
    db = fill_db(tmp_path)
    assert "Birch" in social_context_block(state(), "What OS does it use?", db)
    assert social_context_block(state(), "Drop that, let's discuss coffee.", db) == ""
    for _ in range(3):
        db.add_message("synthetic", "user", "A new subject.")
    assert social_context_block(state(), "And now?", db) == ""
    db.execute_commit("DELETE FROM messages WHERE id = 2")
    assert social_context_block(state(), "Birch?", db) == ""


def test_worker_to_store_to_prompt_uses_one_existing_budget_and_no_legacy_cue(tmp_path):
    db = fill_db(tmp_path)
    host = Host(db)

    class Client:
        calls = []

        def chat_json(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return json.dumps(proposal()), ChatUsage(completion_tokens=300)

    client = Client()
    worker = ConversationSituationWorker(
        client=client, chat_db=db, store=host._conversation_situation_store,
        model="scripted", world_snapshot_provider=dict, judgment_shadow_enabled=False,
    )
    assert not worker.should_run("synthetic")
    worker.notify_user_turn("synthetic", message_id=1)
    assert not worker.should_run("synthetic")
    worker.notify_user_turn("synthetic", message_id=3)
    assert worker.should_run("synthetic")
    worker.mark_scheduled("synthetic")
    result = worker.run("synthetic")
    assert result.repair_target.correction.evidence_message_ids == (3,)
    assert len(client.calls) == 1
    assert client.calls[0][1]["options"]["num_predict"] == 1536
    assert "[message_id=3]" in client.calls[0][0][1]["content"]
    block = host._render_conversation_situation_block("What OS does it use?")
    assert "Tentative reading" in block and "Birch" in block
    assert "Current conversational repair" not in block
    assert "permission or durable memory evidence" in block
    assert worker.stats("synthetic")["last_result"]["has_pragmatic_frame"]
    # A happier unrelated turn does not mutate or resolve the specific repair.
    assert host._conversation_situation_store.get("synthetic") == result


def test_complete_but_truncated_worker_json_preserves_previous_state(tmp_path):
    db = fill_db(tmp_path)
    store = ConversationSituationStore(db)
    store.upsert(state())

    class Client:
        def chat_json(self, *args, **kwargs):
            return json.dumps({"operation": "clear"}), ChatUsage(done_reason="length")

    worker = ConversationSituationWorker(client=Client(), chat_db=db, store=store,
                                         model="scripted", world_snapshot_provider=dict)
    assert worker.run("synthetic") == state()
    assert worker.stats()["invalid_reasons"] == {"output_truncated": 1}


def test_stale_and_ended_frames_are_not_projected(tmp_path):
    host = Host(fill_db(tmp_path))
    for changed in (replace(state(), updated_at="2000-01-01T00:00:00+00:00"),
                    replace(state(), status="ended")):
        host._conversation_situation_store.upsert(changed)
        host._conversation_situation_snapshot_cache = None
        assert host._render_conversation_situation_block("Birch?") == ""


def test_evaluation_prepare_uses_isolated_real_assembler_without_model_calls(tmp_path):
    from app.core.infra.settings import LlmRoute

    case = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"][5]
    result = probe(None, LlmRoute("test", "test"), case, 4, tmp_path, prepare_only=True)
    assert result["messages"][-1]["role"] == "user"
    assert "Current conversational repair" in result["messages"][0]["content"]
    # The probe closes its connection even when only preparing prompt inputs.
    (tmp_path / "chat.db").unlink()


def test_evaluation_closes_owned_database_on_failed_generation(tmp_path):
    from app.core.infra.settings import LlmRoute

    class Client:
        def chat_stream(self, *args, **kwargs):
            raise RuntimeError("synthetic provider failure")

    case = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"][0]
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        probe(Client(), LlmRoute("test", "test"), case, 6, tmp_path)
    (tmp_path / "chat.db").unlink()


def test_corpus_checks_are_frozen_indicators_with_separate_human_review():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert len({case["id"] for case in corpus["cases"]}) == len(corpus["cases"])
    assert {case["split"] for case in corpus["cases"]} == {"regression", "heldout"}
    assert all(7 <= len(case["turns"]) <= 12 for case in corpus["cases"])
    result = score_response("Sorry, sorry. Restart Atlas?", {
        "required": [["Birch"]], "forbidden": ["Restart Atlas"],
    })
    assert result == {"required": [False], "forbidden": [True], "apologies": 2,
                      "question_marks": 1, "human_review": "pending"}
    assert Path(CORPUS).is_file()
