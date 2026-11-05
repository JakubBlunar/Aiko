"""A forwarded question keeps its source and has one shared delivery lifetime."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.conversation import wants_ledger as wl
from app.core.conversation.cue_wants import claim_forward_wants, refresh_forward_wants
from app.core.conversation.wants_ledger_worker import WantsLedgerWorker
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.proactive.forward_curiosity_worker import render_question_cue
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.surfacing_attempt import SurfaceAttempt, current_attempt


class Host(CuePoolMixin):
    def __init__(self, store):
        self._cue_store = store
        self._surfaced_pool_cues = []
        self._settings = SimpleNamespace(agent=SimpleNamespace())
        self.session_key = "owner:session"


@pytest.fixture
def setup_cue(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    store = CueStore(db, user_id="owner")
    entry = {"source": "future_plan", "source_id": "42", "source_at": "2026-11-02T08:00:00Z",
             "topic": "Have morning coffee together after waking on 3 November",
             "question": "Did the morning activity happen?"}
    ident = store.add("forward_curiosity", entry["topic"], render_question_cue(entry),
                      payload=entry, ttl_hours=72)
    now = timephrase.utcnow().isoformat()
    want = wl.Want("want", "ask about the morning thing", "ask", "forward_curiosity",
                   "fc:42", now, 0.8, now)
    return db, store, ident, wl.LedgerState(wants=(want,))


def test_vague_question_keeps_activity_and_original_date(setup_cue):
    _, store, ident, state = setup_cue
    projected = refresh_forward_wants(state, store)
    assert projected.wants[0].source_ref == f"cue:{ident}"
    assert "morning coffee together" in projected.wants[0].text
    assert "3 November" in projected.wants[0].text
    assert "2026-11-02T08:00:00Z" in projected.wants[0].text


@pytest.mark.parametrize("terminal", ["used", "expired", "awaiting", "superseded"])
def test_unavailable_question_cannot_survive_in_second_ledger(setup_cue, terminal):
    db, store, ident, state = setup_cue
    db.execute_commit("UPDATE cue_pool SET state=? WHERE id=?", (terminal, ident))
    assert refresh_forward_wants(state, store).wants == ()


def test_worker_reads_pool_instead_of_historical_journal(setup_cue):
    db, store, ident, _ = setup_cue
    db.kv_set("aiko.forward_curiosity", '[{"source_id":"99","question":"stale question"}]')
    worker = WantsLedgerWorker(kv_get=db.kv_get, kv_set=db.kv_set,
                               user_display_name_provider=lambda: "User",
                               cue_store_provider=lambda: store)
    worker.run()
    state = wl.deserialize(db.kv_get(wl.KV_WANTS_LEDGER))
    assert [w.source_ref for w in state.wants] == [f"cue:{ident}"]
    assert "coffee" in state.wants[0].text


def test_initiative_claim_answer_and_worker_do_not_reask(setup_cue):
    db, store, ident, state = setup_cue
    host = Host(store)
    state = refresh_forward_wants(state, store)
    attempt = SurfaceAttempt()
    token = current_attempt.set(attempt)
    try:
        selected = claim_forward_wants(host, state.wants, block="initiative_block")
        block = selected[0].text
        assert store.get(ident).state == "pending"
        attempt.finish(block, included_blocks={"initiative_block": len(block)})
    finally:
        current_attempt.reset(token)
    assert store.get(ident).state == "surfaced"
    host._settle_pool_cues(user_text="hello", assistant_text=(
        "Did we have morning coffee together after waking on 3 November?"
    ), assistant_message_id=10)
    assert store.get(ident).state == "awaiting"
    host._settle_awaiting_cues(user_text="No morning coffee together: I overslept instead.",
                              user_message_id=11)
    assert store.get(ident).state == "used"
    assert refresh_forward_wants(state, store).wants == ()


def test_omitted_wants_block_does_not_spend_cue(setup_cue):
    _, store, ident, state = setup_cue
    host = Host(store)
    state = refresh_forward_wants(state, store)
    attempt = SurfaceAttempt()
    token = current_attempt.set(attempt)
    try:
        claim_forward_wants(host, state.wants, block="wants_block")
        attempt.finish("ordinary reply", included_blocks={"wants_block": 0})
    finally:
        current_attempt.reset(token)
    assert store.get(ident).surfaced_count == 0


def test_changed_question_is_not_counted_as_an_ask(setup_cue):
    _, store, ident, state = setup_cue
    host = Host(store)
    state = refresh_forward_wants(state, store)
    claim_forward_wants(host, state.wants, block="initiative_block")
    host._settle_pool_cues(user_text="hello", assistant_text="What shall we watch next?",
                            assistant_message_id=10)
    assert store.get(ident).ask_count == 0


def test_unrelated_answer_does_not_create_helpfulness_or_repeat(setup_cue):
    _, store, ident, state = setup_cue
    host = Host(store)
    claim_forward_wants(host, refresh_forward_wants(state, store).wants,
                        block="initiative_block")
    host._settle_pool_cues(user_text="hello", assistant_text=(
        "Did we have morning coffee together after waking?"
    ), assistant_message_id=10)
    host._settle_awaiting_cues(user_text="The graphics card arrived.", user_message_id=11)
    assert store.get(ident).state == "expired"
    assert refresh_forward_wants(state, store).wants == ()


def test_other_wants_keep_their_own_lifetime(setup_cue):
    _, store, _, state = setup_cue
    other = replace(state.wants[0], source="goal", source_ref="goal:4")
    assert refresh_forward_wants(wl.LedgerState(wants=(other,)), store).wants == (other,)


@pytest.mark.parametrize("state_name", ["pending", "expired"])
def test_unreviewed_generated_seed_cannot_resurface_through_wants(setup_cue, state_name):
    db, store, _, state = setup_cue
    ident = store.add("curiosity_seed", "unmotivated topic", "bare topic",
                      payload={"source": "llm", "prompt_text": "bare topic"})
    db.execute_commit("UPDATE cue_pool SET state=? WHERE id=?", (state_name, ident))
    want = replace(state.wants[0], source="curiosity_seed", source_ref=f"cue:{ident}")
    assert refresh_forward_wants(wl.LedgerState(wants=(want,)), store).wants == ()
    assert claim_forward_wants(Host(store), (want,), block="wants_block") == ()


def test_motivated_seed_want_can_outlive_expiry_with_its_full_direction(setup_cue):
    db, store, _, state = setup_cue
    ident = store.add("curiosity_seed", "desk layouts", "short label", payload={
        "source": "llm", "prompt_text": "Explore desk layouts for drawing and coding",
        "why": "Both activities share the same small desk",
        "quality": {"version": 1, "useful_because": "Find a layout with fewer interruptions"},
    })
    db.execute_commit("UPDATE cue_pool SET state='expired' WHERE id=?", (ident,))
    want = replace(state.wants[0], source="curiosity_seed", source_ref=f"cue:{ident}")
    refreshed = refresh_forward_wants(wl.LedgerState(wants=(want,)), store)
    shown = claim_forward_wants(Host(store), refreshed.wants, block="wants_block")
    assert len(shown) == 1
    assert "drawing and coding" in shown[0].text
    assert "same small desk" in shown[0].text
    assert "fewer interruptions" in shown[0].text
    assert store.get(ident).state == "expired"


def test_final_prompt_claims_want_with_its_handling(setup_cue, tmp_path):
    from app.core.session.prompt_assembler import PromptAssembler

    db, store, ident, state = setup_cue
    host = Host(store)
    persona = tmp_path / "persona.txt"
    persona.write_text("Aiko", encoding="utf-8")
    (tmp_path / "conditional_handling.txt").write_text(
        "Things I've been wondering about you:\n- Name the specific activity.\n", encoding="utf-8",
    )
    assembler = PromptAssembler(db, persona_path=persona)

    def wants():
        shown = claim_forward_wants(host, refresh_forward_wants(state, store).wants,
                                    block="wants_block")
        return wl.render_block(wl.LedgerState(wants=shown), timephrase.utcnow())

    assembler.set_inner_life_providers(wants=wants)
    messages, metrics = assembler.assemble_with_budget(
        host.session_key, "hello again", context_window=8192, response_budget=256,
    )
    assert "morning coffee together" in messages[0]["content"]
    assert "Name the specific activity." in messages[0]["content"]
    assert store.get(ident).state == "surfaced"
    assert metrics.surfacing_trace[-1]["claims"][0]["state"] == "committed"


def test_aikos_own_subject_is_consumed_as_a_share_even_without_legacy_hers_flag(setup_cue):
    _, store, ident, state = setup_cue
    store.patch_payload(ident, {"source": "wondering"})
    host = Host(store)
    shown = refresh_forward_wants(state, store)
    claim_forward_wants(host, shown.wants, block="wants_block")
    host._settle_pool_cues(user_text="hello", assistant_text=(
        "I tried having morning coffee together after waking on 3 November."
    ), assistant_message_id=10)
    assert store.get(ident).state == "used"
    assert store.get(ident).ask_count == 0
