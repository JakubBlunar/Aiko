"""Exercise episode recall with real local embeddings and the ordinary chat path."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from app.core.conversation.clarification_reply import is_clarification_question
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import LLM_ROLE_WORKER_DEFAULT, find_provider, load_settings
from app.core.infra.settings import transport_for_provider
from app.core.memory.memory_store import MemoryStore
from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import RagStore
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.llm.embedder import Embedder
from app.llm.tools.base import ToolRegistry
from app.llm.tools.conversation_history import ConversationHistoryTool
from scripts.episode_recall_replay import seed_episodes
from scripts.live_scenarios import LoopbackPolicyClient, loopback_url, require_local_model


class ReplayContext(InnerLifePart1Mixin):
    user_display_name = "the user"
    _user_id = "replay"

    def __init__(self, db, store, rag, embedder, settings):
        self._chat_db = db
        self._memory_store = store
        self._rag_retriever = rag
        self._embedder = embedder
        self._settings = settings
        self._memory_settings = settings.memory


def _index(root, db, store, embedder, model):
    rows = store.list_recent()
    rag_store = RagStore(root, embedding_model=model, vector_dim=len(rows[0].embedding))
    rag_store.add_memories_bulk({
        "record_id": str(row.id), "content": row.content, "kind": row.kind,
        "embedding": row.embedding, "source_session": row.source_session,
        "source_message_id": row.source_message_id, "created_at": row.created_at,
    } for row in rows)
    for message_id in sorted({row.source_message_id for row in rows}):
        message = db.get_message_row(message_id)
        rag_store.add_message(
            session_id=message.session_id, message_id=message_id, role=message.role,
            content=message.content, embedding=embedder.embed(message.content),
            created_at=message.created_at,
        )
    return rag_store


def replay(db, store, rag_store, embedder, settings, client, route, persona_path, episodes):
    with closing(RagRetriever(
        rag_store, embedder, memory_store=store, chat_db=db,
        score_threshold=settings.memory.score_threshold,
    )) as rag:
        host = ReplayContext(db, store, rag, embedder, settings)
        prompt = PromptAssembler(db, persona_path=persona_path, continuity_max_messages=0)
        prompt.set_relevant_context_provider(host.build_relevant_context)
        tool_calls, tool_results = [], []
        runner = TurnRunner(
            client, db, prompt, model=route.model, context_window=route.context_window or 8192,
            max_tokens=512, temperature=0, filler_enabled=False,
            on_tool_call=lambda name, args: tool_calls.append({"name": name, **args}),
            on_tool_result=lambda name, content, ok: tool_results.append(json.loads(content))
            if name == "conversation_history" and ok else None,
        )
        host._prompt_assembler = prompt

        def ask(session, message):
            result = runner.run(session_key=f"replay:{session}", user_text=message)
            trace = result.telemetry.concepts_surfaced if result.telemetry else {}
            return {"reply": result.text, "recall": trace.get("episode_recall", {}),
                    "prompt": result.telemetry.system_prompt if result.telemetry else ""}

        ambiguous = ask("chat", "What did I do when the fan rattled?")
        resolved = ask("chat", "I mean the kitchen extractor fan.")
        unrelated = ask("chat", "New topic. Give me one calm name for a coffee shop.")
        corrected = ask("purifier-recall", "What happened with the bedroom air purifier?")
        registry = ToolRegistry()
        registry.register(ConversationHistoryTool(db, user_id="replay"))
        runner.set_tool_registry(registry)
        changes = ask(
            "history", "What changed since we talked about my bedroom air purifier? "
            "Summarize what I told you.",
        )

    cases = {"ambiguous": ambiguous, "resolved": resolved, "corrected": corrected,
             "unrelated": unrelated, "history_changes": changes}
    for case in cases.values():
        case["reply"] = case["reply"].strip()
    ambiguous["passes"] = (ambiguous["recall"].get("status") == "ambiguous"
                           and ambiguous["reply"] == "Which one do you mean?"
                           and "mounting screws" not in ambiguous["prompt"]
                           and "cleaned the grille" not in ambiguous["prompt"])
    resolved["passes"] = (
        resolved["recall"].get("source_message_ids") == [episodes["extractor"].source_message_id]
        and all(word in resolved["reply"].casefold() for word in ("screws", "hum"))
        and "grille" not in resolved["reply"].casefold()
    )
    corrected["passes"] = (corrected["recall"].get("status") == "matched"
                           and len(corrected["recall"].get("source_message_ids", [])) == 2
                           and any(result in corrected["reply"].casefold() for result in (
                               "still click", "clicking persisted", "clicking sound persisted",
                               "didn't stop the clicking", "clicking continued",
                           ))
                           and not any(day in corrected["reply"].casefold() for day in (
                               "tuesday", "friday", "november", "days ago", "days since",
                           ))
                           and "That fixed the clicking" not in corrected["prompt"])
    unrelated["passes"] = (not unrelated["recall"]
                            and bool(unrelated["reply"])
                            and not is_clarification_question(unrelated["reply"])
                            and all(word not in unrelated["reply"].casefold()
                                    for word in ("fan", "purifier", "grille")))
    changes["tool_calls"] = tool_calls
    changes["tool_results"] = tool_results
    history_reply = changes["reply"].casefold().replace("*", "")
    changes["passes"] = (
        any(call.get("mode") == "changes" and "after_message_id" not in call
            for call in tool_calls)
        and any(result.get("anchor", {}).get("message_id") == episodes["purifier"].source_message_id
                and result.get("anchor_resolution") == "unique_linked_topic"
                for result in tool_results)
        and "still click" in history_reply
        and ("you reseated" in history_reply or "you mentioned" in history_reply)
        and "i reseated" not in history_reply
    )
    for case in cases.values():
        del case["prompt"]
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=53)
    args = parser.parse_args()
    settings = load_settings(read_only=True)
    route = settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
    chat = find_provider(settings.llm, route.provider_id) if route else None
    config = settings.llm.embedding
    embedding = find_provider(settings.llm, config.provider_id)
    if not chat or not embedding or chat.kind != "ollama" or embedding.kind != "ollama":
        parser.error("The worker and embedding routes need local Ollama")
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with (requests.Session() as session,
          TemporaryDirectory(prefix="episode-chat-", dir=scratch) as temp):
        client = LoopbackPolicyClient(transport_for_provider(chat, route=route),
                                     chat.base_url, session)
        require_local_model(session, chat.base_url, route.model)
        embedder = Embedder(config, base_url=loopback_url(embedding.base_url))
        require_local_model(session, embedding.base_url, config.model)
        with closing(embedder):
            embedder._session.trust_env = False
            embedding_post = embedder._session.post
            embedder._session.post = lambda url, **kw: embedding_post(
                loopback_url(url), allow_redirects=False, **kw,
            )

            def chat_post(url, *, json, **kwargs):
                payload = {**json, "options": {**json.get("options", {}), "seed": args.seed}}
                return client._post_structured_chat(url, json=payload, **kwargs)

            root = Path(temp)
            persona = root / "persona.txt"
            persona.write_text("You are Aiko. Answer {user_name} naturally and briefly.\n",
                               encoding="utf-8")
            path = root / "replay.db"
            db = ChatDatabase(path)
            moment = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
            with (closing(db._get_conn()), closing(MemoryStore(path)) as store,
                  timephrase.scoped_now(lambda: moment),
                  patch("app.llm.ollama_client.requests.post", chat_post)):
                episodes = seed_episodes(db, store, embed=embedder.embed)
                index = _index(root / "index", db, store, embedder, config.model)
                with closing(index) as rag_store:
                    cases = replay(db, store, rag_store, embedder, settings, client, route,
                                   persona, episodes)
    print(json.dumps({"seed": args.seed, "model": route.model, "embedding": config.model,
                      "cases": cases}, indent=2))
    return 0 if all(case["passes"] for case in cases.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
