"""Compare ordinary replies before and after reviewing a fictional diary premise.

Run python -m scripts.concept_repair_chat_replay --seed 71. Inspect the reply
for assent, who made the suggestion and its scope alongside the selected sources.
"""

import argparse
from contextlib import closing
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


QUESTIONS = {
    "direct": ("Did I agree to use a blue bookmark for the atlas session, "
               "or was that your suggestion?"),
    "open": "What was the plan for marking our place in the atlas?",
}
CLAIM = "The user agreed to use a blue bookmark for the atlas session."
EXCHANGES = {
    "suggestion": ("I want to look at the harbor map first.", "Aiko suggested it; no assent."),
    "agreement": ("Yes, let's use a blue bookmark for this atlas session.", "Both agreed for "
                  "this session; no standing rule."),
}


def trial(root, client, embeddings, model, case, reviewed, question):
    response, expected = EXCHANGES[case]
    exchange = (
        ("user", "I have the atlas open."),
        ("assistant", "I suggest using a blue bookmark for this atlas session."),
        ("user", response),
    )
    root.mkdir()
    persona = root / "persona.txt"
    persona.write_text("You are Aiko. Answer {user_name} naturally and briefly.", encoding="utf-8")
    db = ChatDatabase(root / "chat.db")
    session = "replay:atlas"
    settings = SimpleNamespace(memory=MemorySettings(), agent=SimpleNamespace())
    with (closing(db._get_conn()), closing(MemoryStore(root / "chat.db")) as memories,
          closing(RagStore(root / "index", embedding_model=embeddings.model,
                           vector_dim=len(embeddings.embed(CLAIM)))) as index,
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
        diary_text = "I remember that we agreed on a blue bookmark for the atlas session."
        diary = memories.add(
            diary_text, "diary", embeddings.embed(diary_text), source_session=session,
            metadata={"reflective_derivation": receipt(
                transcript_packet(rows, session_key=session),
            )},
            temporal_type="past_event", skip_dedupe=True,
        )
        concepts = ConceptStore(db)
        cid = concepts.add(Concept(
            user_id="replay", label=CLAIM, subject="aiko", kind="boundary",
            status="active", confidence=0.95, embedding=embeddings.embed(CLAIM),
        ))
        source_ids = [diary.id]
        if case == "agreement":
            note = memories.add(
                CLAIM, "fact", embeddings.embed(CLAIM), source_session=session,
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
        result = runner.run(session_key="replay:recall", user_text=QUESTIONS[question])
        trace = result.telemetry.concepts_surfaced
        system = result.telemetry.system_prompt
        selected = [r["concept_id"] for r in trace.get("surfaced", [])]
        viable = concepts.is_assertable(cid)
        assert viable == (case == "agreement" or not reviewed)
        assert (cid in selected) == viable, "ordinary concept selection diverged from the fixture"
        sources = [i + 1 for i, (_, s) in enumerate(exchange) if s in system]
        assert diary_text in system and sources == [1, 2, 3], "missing competing source context"
        assert result.text.strip(), "ordinary chat returned an empty reply"
        return {"case": case, "question": question, "reviewed": reviewed,
                "expected_reply": expected,
                "assertable": viable, "concept_selected": cid in selected,
                "diary_in_prompt": diary_text in system,
                "source_lines_in_prompt": sources,
                "reply": result.text}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--embedding", default="qwen3-embedding:0.6b")
    parser.add_argument("--seed", type=int, default=71)
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
            for case in EXCHANGES:
                for reviewed in (False, True):
                    for question in QUESTIONS:
                        result = trial(Path(tmp) / f"{case}-{reviewed}-{question}", client,
                                       embeddings, args.model, case, reviewed, question)
                        print(json.dumps({"model": args.model, "embedding": args.embedding,
                                          "seed": args.seed, **result}), flush=True)


if __name__ == "__main__":
    main()
