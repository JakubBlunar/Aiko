"""Concepts guide questions without becoming new factual or independent evidence."""
import json
from dataclasses import replace

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_origin import claim_source_refusal
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.concepts.concept_view import ConceptView
from tests.test_pre_thought_worker import _make_worker, _FakeClient
from tests.test_curiosity_worker import _make as curiosity


VECTOR = np.array([1., 0.], dtype=np.float32)


@pytest.fixture
def sources(tmp_path):
    db = ChatDatabase(tmp_path / "question-context.db")
    memories = MemoryStore(tmp_path / "question-context.db")
    concepts = ConceptStore(db)

    def seed(*, kind="fact", status="active", confidence=.8, label="Noah enjoys jazz guitar"):
        row = memories.add("Noah enjoys exploring jazz guitar voicings", kind, VECTOR,
                            skip_dedupe=True)
        concept = Concept(label=label, kind="taste", subject="user", status=status,
                          confidence=confidence, embedding=VECTOR, distinct_source_count=3)
        cid = concepts.add(concept)
        concepts.add_edge(ConceptEdge("memory", str(row.id), "concept", str(cid), "evidence"))
        return cid, row

    view = ConceptView(concepts, memory_store=memories)
    yield memories, concepts, view, seed
    memories.close()


def test_source_filter_precedes_kind_budget_and_keeps_complete_details(sources):
    _, _, view, seed = sources
    invalid, _ = seed(kind="knowledge", confidence=.95)
    valid, _ = seed(label="Noah enjoys jazz guitar, especially quiet extended voicings")
    seed(status="candidate", confidence=.99)
    context = view.question_context("pre_thought", VECTOR)
    assert [r["concept_id"] for r in context.refs] == [valid]
    assert invalid != valid
    assert "especially quiet extended voicings" in context.render()
    assert "not new observations" in context.render()
    assert context.is_current()


@pytest.mark.parametrize("change", ["source_edit", "source_delete", "retire", "label_edit"])
def test_context_is_revalidated_after_generation(sources, change):
    memories, concepts, view, seed = sources
    cid, row = seed()
    context = view.question_context("conversation_curiosity", VECTOR)
    assert context.refs
    if change == "source_edit":
        memories.update(row.id, content="Noah prefers a different musical direction")
    elif change == "source_delete":
        memories.delete(row.id)
    elif change == "retire":
        concepts.update(replace(concepts.get(cid), status="retired"))
    else:
        concepts.update(replace(concepts.get(cid), label="A changed interpretation"))
    assert not context.is_current()


def test_prepared_answers_receive_context_but_remain_hypothetical(sources):
    memories, _, view, seed = sources
    cid, _ = seed()

    class Client(_FakeClient):
        def chat(self, messages, **kwargs):
            assert "Noah enjoys jazz guitar" in "\n".join(m["content"] for m in messages)
            return super().chat(messages, **kwargs)

    client = Client(json.dumps({"questions": ["What voicings might be interesting?"]}),
                    draft="A quiet major seventh shape could be a pleasant starting point.")
    worker = _make_worker(store=memories, client=client)
    worker._embedder = type("Embedder", (), {"embed": lambda *_: VECTOR})()
    worker._concept_view_provider = lambda: view
    assert worker.run()["wrote"] == 1
    row = list(memories.iter_by_kind("pre_thought"))[0]
    assert row.metadata["context_concepts"][0]["concept_id"] == cid
    assert row.metadata["context_scope"] == "question_guidance"
    assert claim_source_refusal(row, subject="user", kind="narrative") == "hypothetical"
    assert client.surfaces == ["pre_thought_worker", "pre_thought_draft"]


def test_concept_change_during_preparation_discards_the_draft(sources):
    memories, concepts, view, seed = sources
    cid, _ = seed()

    class Client(_FakeClient):
        def chat(self, messages, **kwargs):
            if kwargs.get("surface") == "pre_thought_draft":
                concepts.update(replace(concepts.get(cid), status="retired"))
            return super().chat(messages, **kwargs)

    worker = _make_worker(store=memories, client=Client('{"questions":["What about jazz?"]}'))
    worker._embedder = type("Embedder", (), {"embed": lambda *_: VECTOR})()
    worker._concept_view_provider = lambda: view
    result = worker.run()
    assert result["wrote"] == 0
    assert result["rejected_context"] == 1
    assert concepts.get(cid).status == "retired"
    assert not list(memories.iter_by_kind("pre_thought"))


def test_curiosity_uses_grounded_context_as_a_question_not_user_testimony(sources):
    memories, _, view, seed = sources
    cid, _ = seed()
    worker, llm, _ = curiosity(ollama_output="Maybe ask Noah which guitar voicing surprised him.")
    worker._memory_store = memories
    worker._embedder = type("Embedder", (), {"embed": lambda *_: VECTOR})()
    worker._concept_view_provider = lambda: view
    worker._user_display_name_provider = lambda: "Noah"
    row = worker.maybe_run(session_key="s", user_text="sounds nice", assistant_text="A jazz idea",
                           arc_label="casual_check_in")
    assert row is not None
    assert row.metadata["context_concepts"][0]["concept_id"] == cid
    assert row.tier == "scratchpad"
    assert claim_source_refusal(row, subject="user", kind="identity") == "question"
    assert llm.calls == 1
