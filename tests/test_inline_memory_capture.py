"""Important interaction agreements survive both deliberate notes and extraction."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.inline_memory import capture_inline_memories, memory_awareness
from app.core.memory.memory_store import MemoryStore
from app.core.services.response_text_service import safe_visible_prefix, strip_all_meta_tags
from app.core.session.session_text_utils import prepare_tts_text
from tests.test_turn_outcomes import _runner


def tag(content, quote, *, self_note=False, **kwargs):
    return "[[remember:" + ("self:" if self_note else "") + json.dumps({
        "content": content, "quote": quote, **kwargs,
    }) + "]]"


@pytest.fixture
def memory(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    store = MemoryStore(tmp_path / "chat.db")
    def embed(text):
        return np.array([1., float(len(text)), float(sum(map(ord, text)) % 97)])
    yield db, store, SimpleNamespace(embed=embed)
    store.close()
    db._get_conn().close()


def test_completed_brain_reply_keeps_user_need_and_own_intention_with_separate_sources(memory):
    db, store, embedder = memory
    user = "A playful cue helps me recognize teasing when we joke."
    declaration = "I will use a playful cue when we joke."
    raw = declaration + " " + tag(
        "Noah finds a playful cue helpful for recognizing teasing when we joke.", user,
    ) + " " + tag("I intend to use a playful cue when we joke.", declaration,
                   self_note=True, temporal_type="future_plan")
    runner = _runner(db, raw)
    runner._memory_store, runner._embedder = store, embedder
    result = runner.run("s", user)
    saved = store.iter_by_kinds(["self", "self_tagged"])
    assert len(saved) == 2
    notes = {row.kind: row for row in saved}
    assert notes["self"].temporal_type == "future_plan"
    assert notes["self"].source_message_id == result.assistant_message_id
    assert notes["self_tagged"].source_message_id == db.get_messages("s")[0].id
    # A sourced paraphrase still retains the conservative inferred/scratchpad contract.
    assert notes["self_tagged"].provenance == "inferred"
    assert notes["self_tagged"].tier == "scratchpad"
    for row in saved:
        assert row.metadata["claim_fidelity"]["sources"][0]["quote"] in (user, declaration)
        assert row.metadata["admission"]["writer"] == "inline"
    from app.core.memory.memory_origin import claim_source_refusal
    from app.core.memory.observation_receipts import longitudinal_sources_current

    assert claim_source_refusal(notes["self_tagged"], subject="user",
                                kind="communication_style") is None
    assert claim_source_refusal(notes["self"], subject="aiko", kind="communication_style") is None
    assert all(longitudinal_sources_current(row, store) for row in saved)
    assert db.get_message_row(result.assistant_message_id).content == declaration
    assert result.text == declaration


@pytest.mark.parametrize("quote", ["I prefer a clear playful signal.", "A made-up user statement."])
def test_user_note_cannot_quote_assistant_or_fabricated_testimony(memory, quote):
    db, store, embedder = memory
    db.add_message("s", "user", "What helps you understand a joke?")
    aid = db.add_message("s", "assistant", "I prefer a clear playful signal.")
    capture_inline_memories(tag("Noah prefers a clear playful signal.", quote),
        db=db, store=store, embedder=embedder, session_key="s",
        assistant_message_id=aid, salience=.7)
    assert store.count() == 0


@pytest.mark.parametrize("wrong", ["missing", "other_session", "user_row"])
def test_inline_capture_requires_the_owned_persisted_assistant_reply(memory, wrong):
    db, store, embedder = memory
    user = db.add_message("s", "user", "I prefer numbered steps.")
    aid = (999999 if wrong == "missing" else user if wrong == "user_row"
           else db.add_message("other", "assistant", "I can do that."))
    capture_inline_memories(tag("Noah prefers numbered steps.", "I prefer numbered steps."),
        db=db, store=store, embedder=embedder, session_key="s",
        assistant_message_id=aid, salience=.7)
    assert store.count() == 0


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_source_change_during_embedding_blocks_the_note(memory, change):
    db, store, _ = memory
    quote = "I prefer short explanations."
    mid = db.add_message("s", "user", quote)
    aid = db.add_message("s", "assistant", "That helps me understand what you need.")
    def embed(text):
        if change == "edit":
            db.execute_commit("UPDATE messages SET content='A correction' WHERE id=?", (mid,))
        else:
            db.execute_commit("DELETE FROM messages WHERE id=?", (mid,))
        return np.ones(3)
    capture_inline_memories(tag("Noah prefers short explanations.", quote), db=db, store=store,
        embedder=SimpleNamespace(embed=embed), session_key="s",
        assistant_message_id=aid, salience=.7)
    assert store.count() == 0


def test_qualified_testimony_survives_when_a_paraphrase_omits_its_condition(memory):
    db, store, embedder = memory
    quote = "Keep the steps numbered when we set things up."
    mid = db.add_message("s", "user", quote)
    aid = db.add_message("s", "assistant", "I can do that.")
    capture_inline_memories(tag("Noah wants numbered steps.", quote),
        db=db, store=store, embedder=embedder, session_key="s",
        assistant_message_id=aid, salience=.7)
    saved = store.iter_by_kind("self_tagged")
    assert len(saved) == 1
    assert saved[0].content == "The user said: " + quote
    assert saved[0].source_message_id == mid
    assert saved[0].metadata["source_note_fallback"] == "lost_condition"
    assert saved[0].metadata["claim_fidelity"]["sources"][0]["quote"] == quote


def test_rejected_extractor_proposals_do_not_hide_a_later_supported_preference():
    from tests.test_memory_extractor_watermark import _FakeDb, _FakeOllama, _FakeStore, _build

    db = _FakeDb()
    row = db.append("user", "Numbered steps help me when we set things up.")
    db.append("assistant", "I can keep that format.")
    missing = {"kind": "preference", "content": "Noah prefers clear steps."}
    valid = {"kind": "preference", "content": "Noah finds numbered steps helpful when setting up.",
             "evidence": [{"message_id": row.id, "quote": row.content}], "basis": "reported"}
    model = _FakeOllama([json.dumps({"memories": [missing, missing, valid]})])
    store = _FakeStore()
    worker = _build(db, model, store=store, min_window_messages=1, max_new_per_run=1)
    assert worker.extract_for_session("s") == 1
    assert store.added[0]["content"] == valid["content"]


def test_source_fallback_preserves_whitespace_and_qualifying_negation(memory):
    db, store, embedder = memory
    source = "When we set things up,\nkeep the steps numbered, but do not hide the warnings."
    mid = db.add_message("s", "user", source)
    aid = db.add_message("s", "assistant", "Numbered steps with the warnings kept visible.")
    capture_inline_memories(tag("Noah wants numbered steps.", "keep the steps numbered"),
        db=db, store=store, embedder=embedder, session_key="s",
        assistant_message_id=aid, salience=.7)
    saved = store.iter_by_kind("self_tagged")
    assert len(saved) == 1
    assert source in saved[0].content
    assert saved[0].metadata["claim_fidelity"]["sources"][0]["quote"] == source
    assert saved[0].source_message_id == mid


def test_extractor_retains_qualified_source_without_accepting_its_incomplete_paraphrase():
    from tests.test_memory_extractor_watermark import _FakeDb, _FakeOllama, _FakeStore, _build

    db = _FakeDb()
    row = db.append("user", "A playful gesture helps me when we are teasing.")
    db.append("assistant", "I can signal that clearly.")
    value = {"content": "Noah finds playful gestures helpful.", "kind": "preference",
             "evidence": [{"message_id": row.id, "quote": row.content}], "basis": "reported"}
    store = _FakeStore()
    worker = _build(db, _FakeOllama([json.dumps({"memories": [value]})]), store=store,
                    min_window_messages=1)
    assert worker.extract_for_session("s") == 1
    assert store.added[0]["content"] == "The user said: " + row.content
    assert store.added[0]["metadata"]["source_note_fallback"] == "lost_condition"


def test_literal_fallback_cannot_inherit_proposed_outcomes_or_supersession():
    from app.core.memory.memory_admission import admit_source_note

    row = SimpleNamespace(id=1, session_id="s", role="user",
        content="A playful cue helps me when we joke.", created_at="2026-11-17T12:00:00Z")
    value = {"content": "Noah benefits from playful cues.", "kind": "preference",
        "evidence": [{"message_id": 1, "quote": row.content}], "basis": "reported",
        "supersedes_memory_id": 42, "observation": {"unsupported": True},
        "contextual_relation": {"relation": "counterexample"},
        "follow_through": {"completed": True}, "event_time": "2050-01-01T12:00Z"}
    candidate, admitted = admit_source_note(value, [row], session_key="s", writer="extractor")
    assert admitted.accepted
    assert candidate["content"] == "The user said: " + row.content
    assert not {"observation", "contextual_relation", "follow_through"} & candidate.keys()
    assert admitted.metadata["admission"]["supersedes_memory_id"] is None
    assert admitted.metadata["claim_fidelity"]["event_time"] is None


@pytest.mark.parametrize("body", [
    '{"content":"Noah prefers this","quote":"not in the source"}',
    '{"content":"unfinished JSON',
    '{"content":"Noah prefers this","quote":true}',
    '{"content":"Noah prefers this","quote":"Hello","temporal_type":[]}',
    '{"content":"Noah currently likes this","quote":"Hello"}',
])
def test_malformed_or_unsupported_structured_note_never_becomes_a_plain_memory(memory, body):
    db, store, embedder = memory
    db.add_message("s", "user", "Hello")
    aid = db.add_message("s", "assistant", "Hi there.")
    capture_inline_memories("[[remember:" + body + "]]", db=db, store=store, embedder=embedder,
        session_key="s", assistant_message_id=aid, salience=.7)
    assert store.count() == 0


def test_note_with_brackets_and_newlines_stays_private_during_streaming_and_tts():
    hidden = tag("Noah uses the [quiet] label.", "I use the [quiet] label.")
    raw = "Got it. " + hidden + " We can keep talking."
    assert strip_all_meta_tags(raw) == "Got it.  We can keep talking."
    assert "quiet" not in prepare_tts_text(raw)
    for end in range(len(raw) + 1):
        visible = safe_visible_prefix(raw[:end])
        assert '"content"' not in visible and "quiet" not in visible


def test_two_note_limit_and_legacy_plain_notes_remain_available(memory):
    db, store, embedder = memory
    db.add_message("s", "user", "I enjoy tea.")
    aid = db.add_message("s", "assistant", "I have a few small preferences.")
    notes = " ".join(f"[[remember:self:I like {word}]]" for word in ("jazz", "tea", "books"))
    # Disable similarity dedupe so this checks the per-reply bound, not embedding coincidences.
    added = []
    original = store.add
    def save(**kwargs):
        added.append(kwargs)
        return original(**kwargs, skip_dedupe=True)
    store.add = save
    capture_inline_memories(notes, db=db, store=store, embedder=embedder,
        session_key="s", assistant_message_id=aid, salience=.7)
    assert len(added) == 2
    assert all(row["kind"] == "self" for row in added)


@pytest.mark.parametrize("reason", ["length", "refusal"])
def test_noncompleted_brain_reply_cannot_save_a_quoted_note(memory, reason):
    db, store, embedder = memory
    user = "I prefer quiet explanations."
    runner = _runner(db, "I understand. " + tag("Noah prefers quiet explanations.", user),
                     reason=reason)
    runner._memory_store, runner._embedder = store, embedder
    assert not runner.run("s", user).outcome.learning_eligible
    assert store.count() == 0


def test_memory_instructions_survive_custom_persona_and_follow_resource_availability(tmp_path):
    from app.core.session.prompt_assembler import PromptAssembler, _PROMPT_BLOCK_TIERS

    persona = tmp_path / "persona.txt"
    persona.write_text("You are Aiko.", encoding="utf-8")
    assembler = PromptAssembler(ChatDatabase(tmp_path / "prompt.db"), persona_path=persona)
    enabled = [True]
    assembler.set_memory_awareness_provider(lambda: memory_awareness(enabled[0], "Noah"))
    messages, telemetry = assembler.assemble_with_budget("s", "Hello", context_window=8192,
                                                        response_budget=256)
    prompt = messages[0]["content"]
    assert "Your private memory notebook" in prompt and "communication need" in prompt
    assert "memory_awareness_block" in _PROMPT_BLOCK_TIERS["T0_stable"]
    enabled[0] = False
    messages, _ = assembler.assemble_with_budget("s", "Hello", context_window=8192,
                                                response_budget=256)
    assert "Your private memory notebook" not in messages[0]["content"]


def test_structured_self_note_spends_for_content_rather_than_its_support_quote():
    from app.core.affect import vulnerability_budget as vb
    from tests.test_vulnerability_budget_post_turn import _make_settings

    now = timephrase.utcnow()
    state = vb.BudgetState(spent=0, last_decay_at=now.isoformat())
    plain = vb.compute_spend_for_self_tags("[[remember:self:I like jazz]]", state, now,
                                           settings=_make_settings())
    quoted_tag = tag("I like jazz", "it matters to me more than I let on", self_note=True)
    quoted = vb.compute_spend_for_self_tags(quoted_tag, state, now, settings=_make_settings())
    assert quoted.total_cost == plain.total_cost
