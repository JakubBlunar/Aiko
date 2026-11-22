"""Durable L46 checkpoints through normal synthesis and restart."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from app.core.concepts.concept_learning_event_store import ConceptLearningEventStore
from app.core.concepts.concept_store import ConceptStore
from app.core.concepts.concept_support import revision_hash
from app.core.concepts.concept_synthesis_routing import ROUTING_KEY
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from app.core.concepts.generalization_reviews import last_premise_reviews, review_key
from app.core.concepts.proposers.generalization_user import SPEC
from app.core.infra.chat_database import ChatDatabase
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from tests.test_concept_synthesis_worker import MemStub, WorkerHarness
from tests.test_generalization_concepts import _add_active, _add_l1_gen, _gen_responder


def run(worker, *, force=False):
    with patch("app.core.concepts.concept_synthesis_worker.CONCEPT_PROPOSERS", [SPEC]):
        return worker.run(force=force)


def harness(responder):
    return WorkerHarness(responder, clusters=[], self_memories=[])


def bases(h, labels):
    return [_add_active(h.store, subject="user", kind="value", label=s) for s in labels]


def restart(h):
    db = ChatDatabase(h.path)
    store = ConceptStore(db)
    store.load_all()
    return ConceptSynthesisWorker(
        concept_store=store, topic_graph=h.topic, memory_store=h.mem,
        embedder=h.worker._embedder, ollama=h.ollama, chat_model="test",
        cancel_event=threading.Event(), agent_settings=h.worker._agent_settings,
        memory_settings=h.worker._memory_settings, kv_get=db.kv_get, kv_set=db.kv_set,
    )


def reviews(h, cid):
    return last_premise_reviews(h.db.kv_get, cid, "user")


def test_second_pass_failure_preserves_first_result_and_retries_after_restart():
    ids = []
    fail = True

    def responder(system, user):
        if "already-named" in system.lower() and fail:
            raise RuntimeError("stacked generation unavailable")
        return _gen_responder(ids)(system, user)

    h = harness(responder)
    ids.extend(bases(h, ["build", "save an original", "preserve context"]))
    g1, g2 = [_add_l1_gen(h.store, subject="user", label=name, child_ids=children)
              for name, children in [("copies", ids[:2]), ("keeps notes", ids[1:])]]
    stats = run(h.worker)
    assert stats["added"] == 1
    assert reviews(h, ids[0])[0]["outcome"]["added"] == 1
    assert reviews(h, g1) == []
    fail = False
    stats = run(restart(h))
    assert stats["generalization_dirty"] is False
    assert stats["generalization_l2_dirty"] is True
    assert stats["added"] == 0
    receipt, = reviews(h, g1)
    assert set(receipt["premises"]) == {str(g1), str(g2)}
    assert receipt["outcome"]["proposals"] == 0
    assert receipt["completed_at"] and receipt["target_depth"] == 2


def test_old_signature_correction_and_new_partners_keep_exact_batches():
    h = harness(lambda *_: {"concepts": []})
    h.worker._memory_settings.concept_synthesis_max_generalization_concepts = 2
    a, b = bases(h, ["asks why", "asks when"])
    key = review_key("user", 1)
    h.db.kv_set(key, json.dumps({"count": 2, "fingerprint": "legacy"}))
    assert reviews(h, a) == []
    run(h.worker)
    first, = reviews(h, a)
    assert set(first["premises"]) == {str(a), str(b)}
    host = SimpleNamespace(_chat_db=h.db, _concept_learning_store=ConceptLearningEventStore(h.db),
                           _concept_store=h.store, _concept_event_store=h.events)
    assert MemoryFacadeMixin.concept_provenance(host, a)["generalization_reviews"] == [first]
    assert run(h.worker)["generalization_dirty"] is False
    base = h.store.get(b)
    base.rationale = "Only when checking the original."
    h.store.update(base)
    c, = bases(h, ["keeps an undo path"])
    run(h.worker)
    changed, = reviews(h, b)
    assert set(changed["premises"]) == {str(b), str(c)}
    assert first["premises"][str(b)] != changed["premises"][str(b)]
    assert reviews(h, a) == [first]
    assert run(h.worker)["generalization_dirty"] is False


def test_stale_and_cancelled_generations_leave_no_completed_reviews():
    h = harness(lambda *_: {"concepts": []})
    a, b = bases(h, ["compares", "rechecks"])

    def change(*_):
        row = h.store.get(a)
        row.label = "compares only reversible changes"
        h.store.update(row)
        return {"concepts": []}

    h.ollama._responder = change
    run(h.worker)
    assert not reviews(h, b)

    def cancel(*_):
        h.worker._cancel_event.set()
        return {"concepts": []}

    h.ollama._responder = cancel
    run(h.worker)
    assert not reviews(h, b)
    h.ollama._responder = lambda *_: {"concepts": []}
    run(restart(h))
    assert reviews(h, b)[0]["outcome"]["proposals"] == 0


def test_partial_batch_does_not_reapply_handled_proposal():
    ids = []

    def respond(*_):
        return {"concepts": [{"label": label, "evidence_concept_ids": ids}
                             for label in ["protects an original", "keeps choices open"]]}

    h = harness(respond)
    ids.extend(bases(h, ["checks copies", "reads choices"]))
    persist = h.worker._persist

    def fail_second(proposal, stats):
        if proposal.label == "keeps choices open":
            raise RuntimeError("temporary write failure")
        persist(proposal, stats)

    with patch.object(h.worker, "_persist", side_effect=fail_second):
        assert run(h.worker)["added"] == 1
    assert reviews(h, ids[0]) == []
    worker = restart(h)
    with patch.object(worker, "_persist", wraps=worker._persist) as resumed:
        assert run(worker)["added"] == 1
    assert resumed.call_count == 1
    assert reviews(h, ids[0])[0]["outcome"] == {"proposals": 2, "added": 2, "reinforced": 0}


def test_changed_support_set_keeps_other_alternative_and_dirties_only_stacked_level():
    h = harness(lambda *_: {"concepts": []})
    a, b, c, d = bases(h, ["keeps copies", "saves drafts", "uses undo", "saves settings"])
    g1 = _add_l1_gen(h.store, subject="user", label="keeps a draft", child_ids=[a, b, c])
    g2 = _add_l1_gen(h.store, subject="user", label="keeps options open", child_ids=[c, d])
    def node(cid):
        return ("concept", str(cid))

    h.store.dependencies.add(g1, [[node(a), node(b)], [node(b), node(c)]])
    h.store.dependencies.add(g2, [[node(c), node(d)]])
    run(h.worker)
    first, = reviews(h, g1)
    h.store.dependencies.withdraw_premise(g1, node(a))
    assert h.store.is_assertable(g1)
    stats = run(h.worker)
    assert not stats["generalization_dirty"] and stats["generalization_l2_dirty"]
    changed, = reviews(h, g1)
    assert first["premises"][str(g1)] != changed["premises"][str(g1)]
    assert first["premises"][str(g2)] == changed["premises"][str(g2)]


def test_focused_retry_has_own_receipt_and_cannot_complete_a_normal_batch():
    h = harness(lambda *_: {"concepts": []})
    a, b = bases(h, ["keeps copies", "checks defaults"])
    run(h.worker)
    normal, = reviews(h, a)
    queue = json.loads(h.db.kv_get(ROUTING_KEY))
    assert len(queue) == 1
    row = queue[0]
    row["next_at"] = "2000-01-01T00:00:00+00:00"
    # Legacy manifests predate target_depth. Exercise their real decode path.
    fields = row["manifest"]["$dict"]
    fields[:] = [pair for pair in fields if pair[0] != "target_depth"]
    h.db.kv_set(ROUTING_KEY, json.dumps(queue))
    stats = run(restart(h))
    assert stats["routing"]["reconsidered"] == 1
    assert not stats["generalization_dirty"]
    same_normal, retry = reviews(h, b)
    assert same_normal == normal
    assert retry["lane"] == "reconsiderations" and retry["outcome"] == "no_claim"
    assert retry["premises"] == normal["premises"]


def test_stacking_prompt_shows_shared_origin_through_two_different_memories():
    prompts = []

    def respond(system, user):
        if "already-named" in system.lower():
            prompts.append(user)
        return {"concepts": []}

    h = harness(respond)
    a, b, c, d = bases(h, ["draft on a copy", "use undo", "keep a save", "try a mod"])
    mid = h.db.add_message("u:session", "user", "I kept a copy before I tried it.")
    for source, cid in [(101, a), (102, c)]:
        h.db.execute_commit(
            "INSERT INTO memories (id,content,kind,embedding,created_at,source_message_id) "
            "VALUES (?, 'kept a copy', 'fact', ?, '2026-11-01', ?)", (source, b"", mid),
        )
        memory = MemStub(source, "kept a copy", "fact", 0.8)
        memory.source_message_id = mid
        h.mem._by_id[source] = memory
        h.store.dependencies.add(cid, [[("memory", str(source))]])
    for label, children in [("preserve drafts", [a, b]), ("protect saves", [c, d])]:
        gid = _add_l1_gen(h.store, subject="user", label=label, child_ids=children)
        h.store.dependencies.add(gid, [[("concept", str(cid)) for cid in children]])
    run(h.worker)
    prompt, = prompts
    origin = revision_hash(f"message:{mid}")[:12]
    assert prompt.count(origin) == 2
    assert "draft on a copy" in prompt and "keep a save" in prompt
