import json
from types import SimpleNamespace as NS

import numpy as np

from app.core.memory.shared_shorthand import admit_shorthand, shorthand_hits, sources_current
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_extractor import MemoryExtractor
from app.core.infra.chat_database import ChatDatabase
from app.core.rag.rag_retriever import RagRetriever


def message(ident, role, content):
    return NS(id=ident, role=role, content=content, session_id="s",
              created_at="2026-10-01T12:00:00+00:00")


def candidate(definition, adopter, *, meaning="small experiments", scope="session", scope_quote=""):
    return {"content": "an adopted term", "kind": "shared_term", "salience": .6,
            "shorthand": {"term": "lab mode", "meaning": meaning,
                          "definition": {"message_id": definition.id, "quote": definition.content},
                          "adoption": {"message_id": adopter.id, "quote": adopter.content},
                          "scope": scope, "scope_quote": scope_quote}}


def test_suggestion_and_question_are_not_adoption_but_direct_agreement_is():
    definition = message(1, "assistant", "Call it lab mode: small experiments.")
    adopter = message(2, "user", "Yes, agreed.")
    good = admit_shorthand(candidate(definition, adopter), [definition, adopter],
                           session_key="s", owner="u")
    assert good is not None
    for content in ("No, not that.", "Could we?", "haha", "Let's pretend yes"):
        adopter.content = content
        assert admit_shorthand(candidate(definition, adopter), [definition, adopter],
                               session_key="s", owner="u") is None


def test_direct_naming_and_scope_require_literal_evidence():
    row = message(1, "user", "Let's always call small experiments lab mode.")
    cand = candidate(row, row, scope="relationship", scope_quote=row.content)
    assert admit_shorthand(cand, [row], session_key="s", owner="u")
    cand["shorthand"]["meaning"] = "a completely different thing"
    assert admit_shorthand(cand, [row], session_key="s", owner="u") is None


def test_user_can_name_the_immediately_preceding_shared_approach():
    definition = message(1, "assistant", "We could try small experiments.")
    adopter = message(2, "user", "Let's call that lab mode.")
    assert admit_shorthand(candidate(definition, adopter), [definition, adopter],
                           session_key="s", owner="u")


def test_explicit_retirement_inherits_the_existing_relationship_scope(tmp_path):
    db = ChatDatabase(tmp_path / "memory.db")
    store = MemoryStore(tmp_path / "memory.db")
    source = message(1, "user", "Let's always call small experiments lab mode.")
    retire = message(2, "user", "Stop calling it lab mode.")
    reader = NS(get_message_row={1: source, 2: retire}.get)
    first = admit_shorthand(
        candidate(source, source, scope="relationship", scope_quote=source.content),
        [source], session_key="s", owner="u",
    )
    try:
        row = store.add(first["content"], kind="shared_term", embedding=np.array([1., 0.]),
                        metadata=first["metadata"], source_session="s", source_message_id=1)
        cand = candidate(retire, retire, meaning="")
        cand["shorthand"]["revises_memory_id"] = row.id
        value = admit_shorthand(cand, [retire], session_key="s", owner="u",
                                memories=store, db=reader)
        assert value["metadata"]["shared_shorthand"]["scope"] == "relationship"
        store.add(value["content"], kind="shared_term", embedding=np.array([1., 0.]),
                  metadata=value["metadata"], source_session="s", source_message_id=2,
                  skip_dedupe=True)
        assert not shorthand_hits(store, reader, RagRetriever,
                                  user_text="lab mode", session_key="other", owner="u")
    finally:
        store.close()
        db._get_conn().close()


def test_reuse_correction_source_edit_deletion_and_session_scope(tmp_path):
    db = ChatDatabase(tmp_path / "memory.db")
    store = MemoryStore(tmp_path / "memory.db")
    source = message(1, "user", "Let's call small experiments lab mode.")
    current = {1: source}
    reader = NS(get_message_row=current.get)

    def add(row, meaning):
        value = admit_shorthand(candidate(row, row, meaning=meaning), [row],
                                session_key="s", owner="u")
        assert value
        return store.add(value["content"], kind="shared_term", embedding=np.array([1., 0.]),
                         source_session="s", source_message_id=row.id,
                         metadata=value["metadata"], skip_dedupe=True)

    try:
        first = add(source, "small experiments")
        args = dict(user_text="Try lab mode", session_key="s", owner="u")
        assert len(shorthand_hits(store, reader, RagRetriever, **args)) == 1
        assert not shorthand_hits(store, reader, RagRetriever, **{**args, "session_key": "other"})
        assert not shorthand_hits(store, reader, RagRetriever, **{**args, "owner": "other"})
        changed = message(2, "user", "Let's call careful trials lab mode instead.")
        current[2] = changed
        second = add(changed, "careful trials")
        hits = shorthand_hits(store, reader, RagRetriever, **args)
        assert len(hits) == 1 and "careful trials" in hits[0].text
        assert hits[0].record.id == str(second.id)
        assert first.id != second.id
        current.pop(2)
        assert not shorthand_hits(store, reader, RagRetriever, **args)
        assert not sources_current(second.metadata["shared_shorthand"], reader)
    finally:
        store.close()
        db._get_conn().close()


def test_extractor_preserves_specialized_metadata_in_pending_batch():
    source = message(1, "user", "Let's call small experiments lab mode.")
    cand = candidate(source, source)
    extractor = object.__new__(MemoryExtractor)
    extractor._user_display_name_provider = lambda: "Jacob"
    validated = extractor._validate_entries([cand])
    assert validated[0]["shorthand"] == cand["shorthand"]
    assert json.loads(json.dumps(validated))[0]["kind"] == "shared_term"


def test_existing_extraction_call_writes_an_adopted_meaning(tmp_path):
    db = ChatDatabase(tmp_path / "memory.db")
    store = MemoryStore(tmp_path / "memory.db")
    source = message(1, "user", "Let's call small experiments lab mode.")
    reply = message(2, "assistant", "Lab mode it is.")
    kv = {}
    reader = NS(kv_get=kv.get, kv_set=lambda k, v: kv.update({k: v}),
                get_messages=lambda *a, **k: [source, reply],
                get_message_row=lambda ident: {1: source, 2: reply}.get(ident))
    calls = []
    identity = {"owner": "u"}

    def chat(messages, **kwargs):
        calls.append(messages)
        identity["owner"] = "other"
        return json.dumps({"memories": [candidate(source, source)]}), NS(
            prompt_tokens=30, completion_tokens=50, done_reason="stop")

    extractor = MemoryExtractor(
        reader, store, NS(embed=lambda _: np.array([1., 0.])), NS(chat_json=chat),
        model="test", think=False, min_window_messages=2, context_window=lambda: 8192,
        user_id_provider=lambda: identity["owner"], user_display_name_provider=lambda: "Jacob",
    )
    try:
        assert extractor.extract_for_session("s") == 1
        assert len(calls) == 1
        row = store.list_recent(kind="shared_term")[0]
        assert row.metadata["shared_shorthand"]["owner"] == "u"
        assert row.tier == "long_term"
        assert "small experiments" in row.content
        assert json.loads(kv["concept.pattern_intake:u"])["shorthand_admitted"] == 1
    finally:
        store.close()
        db._get_conn().close()
