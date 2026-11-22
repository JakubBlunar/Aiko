"""Compare ordinary replies before and after reviewing a fictional diary premise.

Run python -m scripts.concept_repair_chat_replay --seed 71. Repeat with
--occasion soundcheck for an independent exchange. Check speaker, assent,
and any claimed outcome against the source lines in the original order.
Use --without-archive to exercise concept withdrawal alongside ordinary memory recall.
"""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
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
}


def trial(root, client, embeddings, model, occasion, case, reviewed, question, *, archive):
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
        for role, line in exchange:
            mid = db.add_message(session, role, line)
            row = db.get_message_row(mid)
            rows.append(row)
            index.add_message(session_id=session, message_id=mid, role=role, content=line,
                              embedding=embeddings.embed(line), created_at=row.created_at)
        diary_text = fixture.diary
        diary = memories.add(
            diary_text, "diary", embeddings.embed(diary_text), source_session=session,
            metadata={"reflective_derivation": receipt(
                transcript_packet(rows, session_key=session),
            )},
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
        runner = TurnRunner(client, db, prompt, model=model, context_window=8192,
                            temperature=0, max_tokens=512, filler_enabled=False)
        user_text = fixture.direct_question if question == "direct" else fixture.open_question
        result = runner.run(session_key="replay:recall", user_text=user_text)
        trace = result.telemetry.concepts_surfaced
        system = result.telemetry.system_prompt
        selected = [r["concept_id"] for r in trace.get("surfaced", [])]
        archive_ids = trace.get("original_exchange_message_ids", [])
        viable = concepts.is_assertable(cid)
        assert viable == (case == "agreement" or not reviewed)
        sources = [i + 1 for i, (_, s) in enumerate(exchange) if s in system]
        snippet_order = sorted(sources, key=lambda i: system.index(exchange[i - 1][1]))
        if archive:
            assert archive_ids == [row.id for row in rows[1:]] and snippet_order == [2, 3]
            assert cid not in selected and diary_text not in system
        else:
            assert not archive_ids and diary_text in system and sources == [1, 2, 3]
            assert (cid in selected) == viable
        assert result.text.strip(), "ordinary chat returned an empty reply"
        return {"occasion": occasion, "case": case, "question": question, "reviewed": reviewed,
                "expected_reply": expected,
                "assertable": viable, "concept_selected": cid in selected,
                "diary_in_prompt": diary_text in system,
                "source_lines_in_prompt": sources,
                "archive_ids": archive_ids,
                "snippet_order": snippet_order,
                "reply": result.text}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--embedding", default="qwen3-embedding:0.6b")
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--occasion", choices=OCCASIONS, default="atlas")
    parser.add_argument("--without-archive", action="store_true")
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
            for case in ("suggestion", "agreement"):
                for reviewed in (False, True):
                    for question in ("direct", "open"):
                        result = trial(Path(tmp) / f"{case}-{reviewed}-{question}", client,
                                       embeddings, args.model, args.occasion, case, reviewed,
                                       question, archive=not args.without_archive)
                        print(json.dumps({"model": args.model, "embedding": args.embedding,
                                          "seed": args.seed, **result}), flush=True)


if __name__ == "__main__":
    main()
