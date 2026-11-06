import json
from types import SimpleNamespace

from app.core.infra import timephrase
from app.core.memory.research_evidence import evidence_metadata
from app.core.concepts.concept_evidence_lineage import _memory_lineage
from app.core.proactive.idle_knowledge_worker import IdleKnowledgeWorker


def test_only_selected_excerpt_is_a_support_root_in_new_and_legacy_receipts():
    rows = [{"url": "https://example.org/a", "snippet": "A"},
            {"url": "https://example.org/b", "snippet": "B"}]
    meta = evidence_metadata(rows, (1,), timephrase.utcnow())
    assert meta["source_urls"] == ["https://example.org/b"]
    assert len(meta["available_source_urls"]) == 2
    # Historical metadata may still have all offered URLs in this field.
    meta["source_urls"] = [r["url"] for r in rows]
    lineage = _memory_lineage(SimpleNamespace(kind="knowledge", metadata=meta))
    assert lineage.roots == frozenset({"url:https://example.org/b"})
    meta["evidence_support"] = "unattributed"
    assert not _memory_lineage(SimpleNamespace(kind="knowledge", metadata=meta)).complete


def test_overlong_fact_cannot_become_an_unqualified_prefix():
    text = "A possibly useful method. " * 15 + "Except when explicitly declined."
    assert IdleKnowledgeWorker._parse_facts(json.dumps({"facts": [{
        "text": text, "confidence": .9, "source_indices": [0],
    }]})) == []


def test_changed_research_question_is_not_resolved(tmp_path):
    import numpy as np
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.memory_store import MemoryStore
    from app.core.proactive.idle_curiosity_worker import IdleCuriosityWorker, CuriosityAnswer

    path = tmp_path / "research.db"
    ChatDatabase(path)
    store = MemoryStore(path)
    try:
        old = store.add("When did the violin originate?", "open_question", np.array([1., 0.]))
        def embed(text):
            store.update(old.id, content="A different question", embedding=np.array([1., 0.]))
            return np.array([0., 1.])
        host = SimpleNamespace(_memory_store=store, _embedder=SimpleNamespace(embed=embed))
        assert IdleCuriosityWorker._write_finding(host, question=old,
            answer=CuriosityAnswer("The excerpts suggested an origin in Italy.", .8, (0,)),
            safe_query="violin origin", now=timephrase.utcnow(),
            snippets=[{"url": "https://example.org/a", "snippet": "Italy"}]) is None
        assert not store.get(old.id).metadata.get("curiosity_resolved_at")
        assert store.count() == 1
    finally:
        store.close()
