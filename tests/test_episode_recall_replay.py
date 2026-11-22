"""The offline replay preserves a qualified and revision-bound episode."""

from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.conversation.clarification_reply import REFERENCE_QUESTION
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.episode_recall import pending_episode_question
from app.core.memory.memory_store import MemoryStore
from app.core.rag.rag_retriever import RagRetriever
from app.core.session.prompt_assembler import PromptAssembler
from scripts.episode_chat_replay import ReplayContext, _index
from scripts.episode_recall_replay import replay, seed_episodes, select_episode


def test_replay_preserves_partial_results_and_current_correction(tmp_path):
    path = tmp_path / "episode-recall.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        records = seed_episodes(db, store)
        calls = []

        def infer(system, prompt, schema):
            calls.append(prompt)
            assert "Today is " in prompt
            return {"reply": "Synthetic reply."}

        changes = db._get_conn().total_changes
        cases = replay(records, store, infer)
        for case in cases.values():
            assert case["arms"]["episode"]["memory_id"] == case["expected_id"]
        assert cases["partial_repair"]["arms"]["ranked"]["memory_id"] == records["desk"].id
        assert "tightened the mounting screws" in calls[1]
        assert "still hums on high" in calls[1]
        assert "still clicks" in calls[5]
        assert "That fixed the clicking" not in calls[5]
        assert len(cases["corrected_outcome"]["source_message_ids"]) == 2
        assert cases["ambiguous_object"]["selection_status"] == "ambiguous"
        assert "Several recorded episodes match" in calls[8]
        assert len(calls[6]) == len(calls[7]) == len(calls[8])
        for turn in (0, 2, 4, 9):
            assert len(calls[turn]) == len(calls[turn + 1])
        assert db._get_conn().total_changes == changes

        corrected = cases["corrected_outcome"]["source_message_ids"][-1]
        assert db.update_message_content(corrected, "I deleted my report about the purifier.")
        assert select_episode("Did the bedroom air purifier stop?", records.values(), store,
                              timephrase.utcnow()) == (None, "source_unavailable")


def test_episode_replay_rejects_foreign_virtual_ambiguous_and_changed_sources(tmp_path):
    path = tmp_path / "episode-scope.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        records = seed_episodes(db, store)
        now = timephrase.utcnow()
        extractor = records["extractor"]
        query = "What happened with the kitchen extractor fan?"
        virtual = replace(extractor, metadata={**extractor.metadata, "memory_origin": {
            **extractor.metadata["memory_origin"], "domain": "virtual",
        }})
        foreign = replace(extractor, source_session="someone-else:extractor")
        assert select_episode(query, [virtual, foreign], store, now) == (None, "no_match")
        assert select_episode("What did I do when the fan rattled?", records.values(),
                              store, now) == (None, "ambiguous")
        copies = [extractor, replace(extractor, id=999)]
        selected, status = select_episode(query, copies, store, now)
        assert status == "matched" and selected.memory_id == extractor.id
        assert db.update_message_content(extractor.source_message_id, "I removed that report.")
        assert select_episode(query, records.values(), store, now) == (None, "source_unavailable")


def test_episode_selection_in_ordinary_retrieval(tmp_path):
    path = tmp_path / "chat.db"
    db = ChatDatabase(path)
    embedder = SimpleNamespace(embed=lambda _text: np.array([1.0, 0.0], dtype=np.float32))
    settings = SimpleNamespace(memory=SimpleNamespace(), agent=SimpleNamespace())
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        episodes = seed_episodes(db, store)
        with closing(_index(tmp_path / "index", db, store, embedder, "test")) as index:
            with closing(RagRetriever(index, embedder, memory_store=store, chat_db=db)) as rag:
                host = ReplayContext(db, store, rag, embedder, settings)

                def build(text, history=(), session="replay:chat", budget=800, question=""):
                    return host.build_relevant_context(
                        user_text=text, recent_turns=list(history), session_key=session,
                        budget_tokens=budget, episode_question=question,
                    )

                question = "Did that fix it?"
                result = build(question, ["I was thinking about a fan that rattled.", "I see."])
                assert result.concept_trace["episode_recall"] == {
                    "status": "ambiguous", "included": True,
                }
                assert "[[clarify:reference]]" in result.text
                assert "screws" not in result.text and "grille" not in result.text

                prompt = PromptAssembler(db, persona_path=tmp_path / "persona.txt")
                prompt.set_relevant_context_provider(host.build_relevant_context)
                session = "replay:chat"
                for role, content in (
                    ("user", question), ("assistant", REFERENCE_QUESTION),
                    ("user", "I mean the fan that rattled."), ("assistant", REFERENCE_QUESTION),
                ):
                    db.add_message(session, role, content)
                reply = "I mean the kitchen extractor fan."
                reply_id = db.add_message(session, "user", reply)
                messages, telemetry = prompt.assemble_with_budget(
                    session, reply, context_window=8192, response_budget=512,
                    current_user_message_id=reply_id,
                )
                assert telemetry.concepts_surfaced["episode_recall"]["source_message_ids"] == [
                    episodes["extractor"].source_message_id,
                ]
                system = messages[0]["content"]
                assert "screws" in system and "hums" in system and "grille" not in system
                assert messages[-1] == {"role": "user", "content": f"{reply}\n\n{question}"}
                assert all(REFERENCE_QUESTION not in msg["content"] for msg in messages)
                assert db.get_message_row(reply_id).content == reply

                result = build("I mean the fan that rattled.", question=question)
                assert result.concept_trace["episode_recall"]["status"] == "ambiguous"
                assert not result.reply_guidance
                assert not result.episode_question_resolved

                result = build(reply, question=question, budget=24)
                assert not result.episode_question_resolved

                result = build("Did my change fix the bedroom air purifier?")
                assert result.concept_trace["episode_recall"]["status"] == "matched"
                assert "still clicks" in result.text
                assert "That fixed the clicking" not in result.text

                assert "episode_recall" not in build("Give me a title for a novel.").concept_trace
                transient = build(
                    "Did my change fix the bedroom air purifier?", session="replay:chat:noremember",
                )
                assert not transient.concept_trace["episode_recall"]["included"]
                assert not transient.reply_guidance


@pytest.mark.parametrize(("latest", "history"), [
    ("Give me a name for a novel.", [("user", "Did that fix it?"),
                                  ("assistant", REFERENCE_QUESTION)]),
    ("I mean the kitchen extractor fan.", [("user", "Did that fix it?"),
                                          ("user", REFERENCE_QUESTION)]),
    ("I mean the kitchen extractor fan.", [("user", "Did that fix it?"),
                                          ("assistant", "What time should I use?")]),
    ("I mean the kitchen extractor fan.", [("user", "Did that fix it?"),
                                          ("assistant", REFERENCE_QUESTION),
                                          ("user", "New topic."),
                                          ("assistant", REFERENCE_QUESTION)]),
])
def test_pending_episode_question_requires_same_unresolved_exchange(latest, history):
    turns = [SimpleNamespace(role=role, content=content) for role, content in history]
    assert not pending_episode_question(latest, turns)
