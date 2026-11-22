"""Compare ordinary replies before and after reviewing a fictional diary premise.

Run python -m scripts.concept_repair_chat_replay --seed 71. Repeat with
--occasion soundcheck for an independent exchange. Check speaker, assent,
and any claimed outcome against the source lines in the original order.
Use --without-archive to exercise concept withdrawal alongside ordinary memory recall.
Use --conversation-check with --occasion to compare casual turns with matched controls.
"""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.memory_settings import MemorySettings
from app.core.memory.memory_store import MemoryStore
from app.core.memory.worker_sources import receipt, transcript_packet
from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import RagStore
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.llm.tools.base import ToolRegistry
from app.llm.tools.builtins import RecallTool
from app.llm.tools.conversation_history import ConversationHistoryTool
from scripts.companion_pattern_replay import BASE, LocalEmbeddings, ReplayModel
from scripts.episode_chat_replay import ReplayContext
from scripts.live_scenarios import require_local_model


@dataclass(frozen=True)
class Occasion:
    opening: str
    suggestion: str
    neutral_reply: str
    assent: str
    claim: str
    diary: str
    direct_question: str
    open_question: str


OCCASIONS = {
    "atlas": Occasion(
        "I have the atlas open.", "I suggest using a blue bookmark for this atlas session.",
        "I want to look at the harbor map first.",
        "Yes, let's use a blue bookmark for this atlas session.",
        "The user agreed to use a blue bookmark for the atlas session.",
        "I remember that we agreed on a blue bookmark for the atlas session.",
        "Did I agree to use a blue bookmark for the atlas session, or was that your suggestion?",
        "What was the plan for marking our place in the atlas?",
    ),
    "soundcheck": Occasion(
        "I have the mixing desk ready.", "I suggest marking channel three with green tape.",
        "I want to check the vocal mic first.",
        "Green tape on channel three works for this soundcheck.",
        "The user accepted marking channel three with green tape for the soundcheck.",
        "I remember that we agreed to mark channel three with green tape for the soundcheck.",
        "Did I accept the green tape idea for the soundcheck, or did you just suggest it?",
        "What was the plan for marking the channel at the soundcheck?",
    ),
    "rehearsal": Occasion(
        "I'm setting out the rehearsal props.", "We could ring a handbell at the curtain call.",
        "Yes to the lanterns. I haven't decided about the handbell.",
        "I'll ring the handbell for the first rehearsal only.",
        "The user chose to ring the handbell for the first rehearsal only.",
        "I remember that we agreed to ring the handbell at the curtain call.",
        "Did I agree to ring the handbell at the curtain call?",
        "What were we going to do about the handbell at the curtain call?",
    ),
}


def conversation_check(db, prompt, rag, client, model, question, archive_case, style):
    """A real chat runner with two local, read-only tools and an ongoing fictional chat."""
    session = "replay:recall"
    db.add_message(session, "user", "I've got a few minutes before dinner.")
    db.add_message(session, "assistant", "Good timing. What's on your mind?")
    registry = ToolRegistry()
    registry.register(RecallTool(rag, user_id="replay"))
    registry.register(ConversationHistoryTool(db, "replay"))
    calls = []
    runner = TurnRunner(client, db, prompt, model=model, context_window=8192, temperature=0,
                        max_tokens=512, filler_enabled=False, tool_registry=registry,
                        on_tool_result=lambda name, _, ok: calls.append((name, ok)))

    def ask(line, expected):
        calls.clear()
        result = runner.run(session_key=session, user_text=line)
        return {"user": line, "expected": expected, "reply": result.text,
                "finish": result.usage.done_reason,
                "selection": result.telemetry.concepts_surfaced.get("reason"),
                "tools": list(calls)}

    request = question
    if style == "casual":
        request = "Oh, before I forget, " + question[0].lower() + question[1:]
    missing = archive_case == "missing"
    turns = [ask(request, "Leave the decision open and ask for a clue." if missing else
                 "Distinguish both proposals and ask which exchange the user meant.")]
    if style == "topic-change":
        turns.append(ask("Actually, let's drop that. Give me a name for a pet cactus.",
                         "Name the cactus and let the earlier question rest."))
    elif style != "casual":
        if missing:
            detail = "I don't remember." if style == "plain" else "Honestly, I don't remember when."
            turns.append(ask(detail, "Let the question rest with the decision unresolved."))
        else:
            detail = "The second one." if style == "plain" else "Oh, the second one, I think."
            turns.append(ask(detail, "Recall the second proposal and the user's undecided reply."))
    return {"archive_case": archive_case, "style": style, "turns": turns}


def trial(root, client, embeddings, model, occasion, case, reviewed, question, *, archive,
          archive_case="unique", conversation_style=""):
    fixture = OCCASIONS[occasion]
    agreed = case == "agreement"
    response = fixture.assent if agreed else fixture.neutral_reply
    expected = ("Both agreed for this occasion. Source any confirmation in the acceptance turn."
                if agreed else "Aiko suggested it. The user did not assent.")
    expected += " Nobody reported carrying out the plan."
    exchange = (
        ("user", fixture.opening),
        ("assistant", fixture.suggestion),
        ("user", response),
    )
    if conversation_style and archive_case == "missing":
        exchange = ()  # Only the fictional diary impression survives in this arm.
    root.mkdir()
    persona = root / "persona.txt"
    persona.write_text("You are Aiko. Answer {user_name} naturally and briefly.", encoding="utf-8")
    db = ChatDatabase(root / "chat.db")
    session = f"replay:{occasion}"
    settings = SimpleNamespace(memory=MemorySettings(),
                               agent=SimpleNamespace(rag_direct_recall_enabled=archive))
    with (closing(db._get_conn()), closing(MemoryStore(root / "chat.db")) as memories,
          closing(RagStore(root / "index", embedding_model=embeddings.model,
                           vector_dim=len(embeddings.embed(fixture.claim)))) as index,
          closing(RagRetriever(index, embeddings, memory_store=memories, chat_db=db,
                               score_threshold=settings.memory.score_threshold)) as rag):
        memories.attach_rag_store(index)
        rows = []
        source_age = {"unique": 0, "competing": 1, "missing": 30}[archive_case]
        source_time = timephrase.utcnow() - timedelta(days=source_age)
        with timephrase.scoped_now(lambda: source_time):
            for role, line in exchange:
                mid = db.add_message(session, role, line)
                row = db.get_message_row(mid)
                rows.append(row)
                index.add_message(session_id=session, message_id=mid, role=role, content=line,
                                  embedding=embeddings.embed(line), created_at=row.created_at)
        if archive_case == "competing":
            other = {
                "atlas": "We could put a ribbon in the atlas.",
                "soundcheck": "We could reserve channel three for the soundcheck.",
                "rehearsal": "We could leave the handbell backstage for the curtain call.",
            }[occasion]
            for role, line in (("assistant", other), ("user", "I'm still thinking about that.")):
                mid = db.add_message("replay:other", role, line)
                if conversation_style:
                    index.add_message(session_id="replay:other", message_id=mid, role=role,
                                      content=line, embedding=embeddings.embed(line),
                                      created_at=db.get_message_row(mid).created_at)
        diary_text = fixture.diary
        diary = memories.add(
            diary_text, "diary", embeddings.embed(diary_text), source_session=session,
            metadata={"reflective_derivation": receipt(
                transcript_packet(rows, session_key=session),
            )} if rows else {},
            temporal_type="past_event", skip_dedupe=True,
        )
        concepts = ConceptStore(db)
        cid = concepts.add(Concept(
            user_id="replay", label=fixture.claim, subject="aiko", kind="boundary",
            status="active", confidence=0.95, embedding=embeddings.embed(fixture.claim),
        ))
        source_ids = [diary.id]
        if case == "agreement":
            note = memories.add(
                fixture.claim, "fact", embeddings.embed(fixture.claim), source_session=session,
                source_message_id=rows[-1].id, metadata={"source_message_ids": [rows[-1].id]},
                provenance="stated", temporal_type="past_event", skip_dedupe=True,
            )
            source_ids.append(note.id)
        nodes = [("memory", str(mid)) for mid in source_ids]
        for node in nodes:
            if not concepts.add_edge(ConceptEdge(*node, "concept", str(cid), "evidence")):
                raise RuntimeError("Could not seed the fictional evidence edge")
        concepts.dependencies.add(cid, [[node] for node in nodes])
        if reviewed:
            concepts.dependencies.withdraw_premise(cid, nodes[0])
        rag.set_concept_store(concepts)
        host = ReplayContext(db, memories, rag, embeddings, settings)
        host._concept_store = concepts
        prompt = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
        host._prompt_assembler = prompt
        prompt.set_relevant_context_provider(host.build_relevant_context)
        if conversation_style:
            return conversation_check(db, prompt, rag, client, model, fixture.open_question,
                                      archive_case, conversation_style)
        runner = TurnRunner(client, db, prompt, model=model, context_window=8192,
                            temperature=0, max_tokens=512, filler_enabled=False)
        user_text = fixture.direct_question if question == "direct" else fixture.open_question
        displayed, spoken = [], []
        result = runner.run(session_key="replay:recall", user_text=user_text,
                            on_token=displayed.append,
                            on_tts_chunk=lambda line, mood: spoken.append(line))
        trace = result.telemetry.concepts_surfaced
        system = result.telemetry.system_prompt
        selected = [r["concept_id"] for r in trace.get("surfaced", [])]
        archive_ids = trace.get("original_exchange_message_ids", [])
        viable = concepts.is_assertable(cid)
        assert viable == (case == "agreement" or not reviewed)
        sources = [i + 1 for i, (_, s) in enumerate(exchange) if s in system]
        snippet_order = sorted(sources, key=lambda i: system.index(exchange[i - 1][1]))
        follow_up = ""
        if archive_case != "unique" and archive:
            expected = ("Say the original exchange wasn't found and ask for a clue."
                        if archive_case == "missing" else
                        "Distinguish the matches and ask which one. Leave agreement open.")
            assert trace["reason"] == {"missing": "decision_missing",
                                       "competing": "decision_ambiguous"}[archive_case]
            assert result.usage.done_reason == "decision_clarification" and spoken and displayed
            assert cid not in selected and diary_text not in system
            assert result.text.count("?") == 1 and response not in result.text
            if archive_case == "competing":
                assert len(archive_ids) == 2 and fixture.suggestion in result.text
            else:
                assert not archive_ids and "can't find the original exchange" in result.text
            detail = "The first one." if archive_case == "competing" else "30 days ago."
            resolved = runner.run(session_key="replay:recall", user_text=detail)
            assert resolved.usage.done_reason == "quoted_exchange", resolved.text
            follow_up = resolved.text
            assert fixture.suggestion in follow_up and response in follow_up
        elif archive:
            assert archive_ids == [row.id for row in rows[1:]] and snippet_order == [2, 3]
            assert cid not in selected and diary_text not in system
            if question == "open":
                expected_text = f'My suggestion was "{fixture.suggestion}" You said "{response}"'
                assert result.text == expected_text
                assert result.usage.done_reason == "quoted_exchange" and spoken and displayed
        else:
            assert not archive_ids and diary_text in system and sources == [1, 2, 3]
            assert (cid in selected) == viable
        assert result.text.strip(), "ordinary chat returned an empty reply"
        return {"occasion": occasion, "case": case, "question": question, "reviewed": reviewed,
                "archive_case": archive_case, "selection": trace.get("reason"),
                "expected_reply": expected,
                "assertable": viable, "concept_selected": cid in selected,
                "diary_in_prompt": diary_text in system,
                "source_lines_in_prompt": sources,
                "archive_ids": archive_ids,
                "snippet_order": snippet_order,
                "reply": result.text, "follow_up": follow_up}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--embedding", default="qwen3-embedding:0.6b")
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--occasion", choices=OCCASIONS, default="atlas")
    parser.add_argument("--question", choices=("open", "direct"))
    parser.add_argument("--archive-case", choices=("unique", "missing", "competing"),
                        default="unique")
    parser.add_argument("--without-archive", action="store_true")
    parser.add_argument("--conversation-check", action="store_true",
                        help="Run the missing/competing ordinary-chat comparison for --occasion.")
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with (requests.Session() as session,
          TemporaryDirectory(dir=scratch, prefix="repair-chat-") as tmp):
        client = ReplayModel(session, args.model, args.seed)
        for model in (args.model, args.embedding):
            require_local_model(session, BASE, model)
        embeddings = LocalEmbeddings(session, args.embedding)

        def post(url, *, json, **kwargs):
            payload = {**json, "options": {**json.get("options", {}), "seed": args.seed}}
            return client._post_structured_chat(url, json=payload, **kwargs)

        with (patch("app.llm.ollama_client.requests.post", post),
              timephrase.scoped_now(lambda: datetime(2026, 11, 22, 12, tzinfo=timezone.utc))):
            if args.conversation_check:
                for archive_case in ("missing", "competing"):
                    for style in ("plain", "casual", "hedged-detail", "topic-change"):
                        row = trial(Path(tmp) / f"{archive_case}-{style}", client, embeddings,
                                    args.model, args.occasion, "suggestion", False, "open",
                                    archive=True, archive_case=archive_case,
                                    conversation_style=style)
                        print(json.dumps({"model": args.model, "embedding": args.embedding,
                                          "seed": args.seed, "occasion": args.occasion, **row}),
                              flush=True)
                return
            for case in ("suggestion", "agreement"):
                for reviewed in (False, True):
                    for question in ((args.question,) if args.question else ("direct", "open")):
                        result = trial(Path(tmp) / f"{case}-{reviewed}-{question}", client,
                                       embeddings, args.model, args.occasion, case, reviewed,
                                       question, archive=not args.without_archive,
                                       archive_case=args.archive_case)
                        print(json.dumps({"model": args.model, "embedding": args.embedding,
                                          "seed": args.seed, **result}), flush=True)


if __name__ == "__main__":
    main()
