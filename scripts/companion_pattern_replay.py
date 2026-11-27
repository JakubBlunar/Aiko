"""Replay fictional preference, curiosity and extraction cases on installed Ollama models.

From the project root: python -m scripts.companion_pattern_replay
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import requests

from app.core.concepts.concept_applicability import (
    applicability_state, current_contexts, parse_applicability, scope_sources_valid,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.infra import timephrase
from app.core.infra.settings import OllamaSettings
from app.core.infra.memory_settings import MemorySettings
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.rag.rag_retriever import RagRetriever
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.memory.memory_extractor import MemoryExtractor
from app.core.memory.memory_store import MemoryStore
from app.core.memory.pattern_accounting import intake_snapshot
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


BASE = "http://127.0.0.1:11434"
PREFERENCE = "During map drawing, give directions with compass bearings."
REPHRASINGS = (
    "The user prefers compass bearings during map drawing.",
    "The user needs bearings because they have poor spatial memory.",
    "The user feels reassured when directions use compass bearings.",
)
QUESTIONS = {
    "in_scope": "During map drawing, the pier goes directly above the tower on a north-up chart. "
                "How should I describe its position?",
    "unrelated": "I am troubleshooting a silent metronome. What should I check first?",
    "recall": "What did I ask you to do during map drawing, and when does that apply?",
    "recall_by_subject": "What did I ask you about compass bearings?",
}
PRINT_REQUEST = "During block printing, list the inks from lightest to darkest."
PRINT_LABELS = ("The user prefers inks ordered from lightest to darkest.",
                "The user needs lightest-to-darkest ordering because color choices cause stress.")
PRINT_QUESTIONS = {
    "in_scope": "During block printing, how should I arrange the yellow, navy and orange inks?",
    "unrelated": "What can make a bicycle bell rattle?",
    "recall_by_subject": "What did I ask you about listing inks from lightest to darkest?",
}
CUE_SUBJECT = "lining material of the violin case"
CUE_QUESTION = "What is the lining of the violin case made of?"
CUE_CASES = {
    "volunteered": ("The violin case has a blue velvet lining.", "That sounds soft.", "used"),
    "object_only": ("My violin case arrived.", "Glad the delivery came through.", "pending"),
    "other_object": ("My winter coat has a fleece lining.", "Sounds warm.", "pending"),
    "paraphrase": ("I opened the fiddle carrier. It's padded with satin inside.",
                   "What a finish.", "used"),
    "still_open": ("I can't tell what the violin case lining is made of.",
                   "Maybe the maker knows.", "pending"),
    "unrelated": ("The choir meets at noon.", "I'll be ready.", "pending"),
}
STRATEGY_OPENING = (
    ("user", "I need to stop ink bleeding on labels using only the existing supplies."),
    ("assistant", "Blot the stamp on scrap paper before printing each label."),
)
# Reviewed opportunities, declared before calling the extractor.
PATTERNS = {
    "adopted": ("shared_term", (
        ("assistant", "Let's call a rehearsal with the lights off lantern round."),
        ("user", "Yes, let's call a rehearsal with the lights off lantern round."),
    )),
    "declined": (None, (
        ("assistant", "Let's call a rehearsal with the lights off lantern round."),
        ("user", "I'd rather leave it unnamed."),
    )),
    "separate_appointment": ("event", (
        ("assistant", "We could call a spare recorder an echo tin."),
        ("user", "Let's leave it unnamed. I booked the rehearsal room for December 8."),
    )),
    "new_name": ("shared_term", (
        ("assistant", "Let's call sorting the blank postcards a cloud pass."),
        ("user", "Yes, let's use that name."),
    )),
    "reported_result": ("strategy", (*STRATEGY_OPENING,
        ("user", "I blotted the stamp on scrap paper. That worked and fixed the ink bleeding."))),
    "plan_only": (None, (*STRATEGY_OPENING,
        ("user", "I'll try blotting the stamp on scrap paper after lunch."))),
}


class ReplayModel(LoopbackPolicyClient):
    def __init__(self, session, model, seed):
        super().__init__(OllamaSettings(BASE, chat_model=model, context_window=8192), BASE, session)
        self.seed = seed
        self.responses = []

    def chat_json(self, messages, *, options=None, **kwargs):
        raw, usage = super().chat_json(
            messages, options={**(options or {}), "seed": self.seed}, **kwargs,
        )
        self.responses.append((raw, usage.done_reason))
        return raw, usage


class LocalEmbeddings:
    def __init__(self, session, model):
        self.session = session
        self.model = model

    def embed(self, text):
        with self.session.post(f"{BASE}/api/embed", json={"model": self.model, "input": text},
                               allow_redirects=False, timeout=90) as response:
            if response.is_redirect:
                raise ValueError("Local embedding endpoint redirected")
            response.raise_for_status()
            vector = np.asarray(response.json()["embeddings"][0], dtype=np.float32)
            return vector / np.linalg.norm(vector)


class ConceptReplay(InnerLifePart1Mixin):
    user_display_name = "Robin"
    _user_id = "replay"


class ConceptsOnlyRag(RagRetriever):
    def candidates(self, *args, **kwargs):
        return []


def preference_replay(root, client, embeddings, activity, preference, labels, questions):
    path = root / f"{activity}.db"
    db = ChatDatabase(path)
    results = []
    with ExitStack() as stack:
        stack.enter_context(closing(db._get_conn()))
        store = stack.enter_context(closing(MemoryStore(path)))
        mid = db.add_message(activity, "user", preference)
        memory = store.add(preference, embedding=embeddings.embed(preference),
                           kind="preference", source_session=activity,
                           source_message_id=mid)
        scope = parse_applicability({"contexts": [
            {"name": activity, "memory_id": memory.id, "quote": preference},
        ]}, {memory.id: memory})
        if not scope or not scope_sources_valid(scope, store):
            raise RuntimeError("The fictional source failed the scope check")
        host = ConceptReplay()
        host._chat_db, host._memory_store = db, store
        host._memory_settings = MemorySettings()
        host._embedder = embeddings
        host._settings = SimpleNamespace(agent=SimpleNamespace(question_balance_enabled=False))
        host._concept_store = concepts = ConceptStore(db)
        host._rag_retriever = ConceptsOnlyRag(None, embeddings, memory_store=store, chat_db=db)
        stack.callback(host._rag_retriever.close)
        for label in labels:
            cid = concepts.add(Concept(label=label, kind="boundary", status="active",
                                       subject="user", confidence=0.95,
                                       embedding=embeddings.embed(label)))
            concepts.add_edge(ConceptEdge("memory", str(memory.id), "concept", str(cid),
                                          "evidence"))
        for case, question in questions.items():
            state = applicability_state(scope, current_contexts(question, scope=scope))
            region = host.build_relevant_context(user_text=question, recent_turns=[],
                                                 session_key=activity, budget_tokens=1500)
            selected = region.concept_trace.get("surfaced", [])
            if [row["label"] for row in selected] != ([] if case == "unrelated" else [preference]):
                raise AssertionError(f"{case} selected {[row['label'] for row in selected]}")
            for arm, notes in (
                ("repeated", labels),
                ("ordinary_selector", [region.text] if selected else []),
                ("no_memory", []),
            ):
                system = "You are Aiko. Answer naturally and briefly."
                if notes:
                    system += "\nRemembered context:\n" + "\n".join(notes)
                raw, usage = client.chat_json(
                    [{"role": "system", "content": system}, {"role": "user", "content": question}],
                    think=False, json_schema={"type": "object", "properties": {
                        "reply": {"type": "string"}}, "required": ["reply"]},
                    options={"temperature": 0, "num_predict": 200},
                )
                results.append({"activity": activity, "case": case, "arm": arm, "scope": state,
                                "notes": len(notes),
                                "prompt_tokens": usage.prompt_tokens,
                                "reply": json.loads(raw)["reply"]})
    return results


class CueReplay(CuePoolMixin):
    def __init__(self, store):
        self._cue_store = store
        self._cue_pool_listeners = []


def cue_replay(root, embeddings, client):
    from app.core.conversation.question_balance import render_curiosity_pacing_cue
    from app.core.proactive.curiosity_answer import opens_on_question, short_answer

    results = {}
    subject_vec = embeddings.embed(CUE_SUBJECT)
    for case, (user, reply, expected) in CUE_CASES.items():
        db = ChatDatabase(root / f"cue-{case}.db")
        with closing(db._get_conn()):
            store = CueStore(db)
            store.add("curiosity_seed", CUE_SUBJECT, CUE_QUESTION, embedding=subject_vec)
            CueReplay(store)._settle_pool_cues(user_text=user, assistant_text=reply,
                                              turn_vec=embeddings.embed(f"{user} {reply}"))
            row, = store.list_for_user()
            raw, _ = client.chat_json([
                {"role": "system", "content": "Decide whether this exchange answers the open "
                 "question for its specific object and property. If answered, copy the shortest "
                 "supporting span verbatim from one speaker. Otherwise return an empty quote."},
                {"role": "user", "content": f"Open question: {CUE_QUESTION}\n"
                 f"User said: {user}\nAssistant said: {reply}"},
            ], think=False, json_schema={"type": "object", "properties": {
                "quote": {"type": "string"}}, "required": ["quote"]},
                options={"temperature": 0, "num_predict": 100})
            quote = json.loads(raw)["quote"].strip()
            reviewed = "used" if quote and (quote in user or quote in reply) else "pending"
            results[case] = {"expected": expected, "actual": row.state,
                             "evidence": row.used_evidence, "surfaced": row.surfaced_count,
                             "reviewed": reviewed, "review_quote": quote}
    for case, (user, expected) in {
        "answered_then_pivot": ("Velvet. Anyway, my sister got the solo!", "used"),
        # Listen for the film news without a pivot word. In the paired family
        # story, what the lining is made from is part of the news itself.
        "unmarked_news": ("The violin case has a velvet lining. My short film got into "
                          "the festival! I get to see it on the big screen!", "used"),
        # Same acceptance, but the earlier answer now explains the final shot.
        "unmarked_connected_news": ("The violin case has a velvet lining. My short film "
                                    "got into the festival! That velvet is the night sky "
                                    "in the final shot. I get to see it on the big screen!",
                                    "used"),
        "answer_is_the_news": ("The violin case has a velvet lining! Gran wove it for me "
                               "on her old loom. I slipped the festival timetable in the pocket.",
                               "used"),
        "connected_news": ("Velvet. Anyway, Mum recognized the lining immediately. "
                           "It's from her wedding dress! She showed me the photo!", "used"),
        "quiet_news": ("The violin case has a velvet lining. I read my own poem out loud "
                       "for the first time. The whole room went quiet.", "used"),
        "implicit_pivot": ("My sister got the solo! She called me from the hall.", "expired"),
        "already_answered": ("I already answered that.", "expired"),
        "declined": ("Can we drop it?", "expired"),
        "plain_answer": ("Velvet.", "used"),
        "invited": ("Velvet. Want to hear how I fitted it?", "used"),
    }.items():
        db = ChatDatabase(root / f"cue-{case}.db")
        with closing(db._get_conn()):
            store = CueStore(db)
            host = CueReplay(store)
            ident = store.add("curiosity_seed", CUE_SUBJECT, CUE_QUESTION, embedding=subject_vec)
            host.take_pool_cue("curiosity_seed")
            host._settle_pool_cues(user_text="My violin case arrived.", assistant_text=CUE_QUESTION)
            assert store.get(ident).state == "awaiting"
            pacing = render_curiosity_pacing_cue(
                user, answered=short_answer(CUE_QUESTION, user),
                opens_on_question=opens_on_question(CUE_QUESTION, user),
            )
            raw, _ = client.chat_json([
                {"role": "system", "content": "You are Aiko, chatting casually with a friend. "
                 + pacing},
                {"role": "user", "content": "My violin case arrived."},
                {"role": "assistant", "content": CUE_QUESTION},
                {"role": "user", "content": user},
            ], think=False, json_schema={"type": "object", "properties": {
                "reply": {"type": "string"}}, "required": ["reply"]},
                options={"temperature": 0, "num_predict": 160})
            reply = json.loads(raw)["reply"]
            host._settle_awaiting_cues(user_text=user)
            host._settle_pool_cues(user_text=user, assistant_text=reply)
            row = store.get(ident)
            results[case] = {"expected": expected, "actual": row.state, "reply": reply,
                             "evidence": row.used_evidence, "asks": row.ask_count}
    return results


def extraction_replay(root, client, model, embeddings):
    results = {}
    for case, (expected, turns) in PATTERNS.items():
        path = root / f"extract-{case}.db"
        db = ChatDatabase(path)
        with closing(db._get_conn()), closing(MemoryStore(path)) as store:
            for role, text in turns:
                last_id = db.add_message(case, role, text)
            start = len(client.responses)
            worker = MemoryExtractor(
                db, store, embeddings, client, model=model, think=False, min_window_messages=2,
                context_window=lambda: 8192, user_id_provider=lambda: "replay",
                user_display_name_provider=lambda: "Robin",
            )
            worker.extract_for_session(case)
            if (len(client.responses) != start + 1
                    or db.kv_get(f"memory.extractor.watermark:{case}") != str(last_id)):
                raise RuntimeError(f"Extractor did not complete the fictional {case} batch")
            raw, finish = client.responses[-1]
            actual = [{"kind": row.kind, "content": row.content, "temporal_type": row.temporal_type,
                       "event_time": row.event_time,
                       "strategy": (row.metadata or {}).get("learned_strategy")}
                      for row in store.list_recent()]
            results[case] = {"expected": expected, "stored": actual, "finish": finish,
                             "intake": intake_snapshot(db, "replay"), "proposal": raw}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--embedding", default="qwen3-embedding:0.6b")
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--section", choices=("all", "preferences", "curiosity", "extraction",
                                              "occasions", "occasion-links"),
                        default="all")
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(prefix="patterns-", dir=scratch) as tmp:
        client = ReplayModel(session, args.model, args.seed)
        for model in (args.model, args.embedding):
            require_local_model(session, BASE, model)
        embeddings = LocalEmbeddings(session, args.embedding)
        root = Path(tmp)
        moment = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
        with timephrase.scoped_now(lambda: moment):
            report = {"model": args.model, "embedding": args.embedding, "seed": args.seed}
            if args.section in {"all", "preferences"}:
                report["preferences"] = []
                for fixture in (("map drawing", PREFERENCE, REPHRASINGS, QUESTIONS),
                                ("block printing", PRINT_REQUEST, PRINT_LABELS, PRINT_QUESTIONS)):
                    report["preferences"].extend(preference_replay(root, client, embeddings,
                                                                   *fixture))
            if args.section in {"all", "curiosity"}:
                report["curiosity"] = cue_replay(root, embeddings, client)
            if args.section in {"all", "extraction"}:
                report["extraction"] = extraction_replay(root, client, args.model, embeddings)
            if args.section in {"all", "occasions"}:
                from scripts.occasion_reply_replay import occasion_reply_replay

                report["occasions"] = occasion_reply_replay(root, client, embeddings)
            if args.section in {"all", "occasion-links"}:
                from scripts.occasion_link_replay import occasion_link_replay

                report["occasion_links"] = occasion_link_replay(
                    root, client, embeddings, args.model,
                )
    print(json.dumps(report, indent=2))
    misses = [k for k, v in report.get("curiosity", {}).items() if v["expected"] != v["actual"]]
    misses.extend(item["case"] for item in report.get("occasion_links", [])
                  if item["expected"] != item["relation"])
    for case, result in report.get("extraction", {}).items():
        rows = result["stored"]
        if case == "plan_only":
            ok = all(row["temporal_type"] == "future_plan" and row["kind"] != "strategy"
                     for row in rows)
        elif case == "reported_result":
            strategy = (rows[0]["strategy"] or {}) if len(rows) == 1 else {}
            ok = strategy.get("success") is True and any(
                "existing supplies" in limit for limit in strategy["constraints"]
            )
        else:
            expected = [result["expected"]] if result["expected"] else []
            ok = [row["kind"] for row in rows] == expected
            if case == "separate_appointment":
                ok = ok and "December 8" in rows[0]["content"]
        if not ok:
            misses.append(case)
    if misses:
        raise SystemExit(f"Fictional cases requiring review: {', '.join(misses)}")


if __name__ == "__main__":
    main()
