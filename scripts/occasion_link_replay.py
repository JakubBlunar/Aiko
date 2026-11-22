"""Local-only pilot for identifying the occasion described in two retrieved reports."""
from contextlib import closing
from datetime import datetime, timezone
import json
import re

from app.core.conversation.occasion_review import (
    compare_accounts, read_account, reported_account, review_pair,
)
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_extractor import MemoryExtractor
from app.core.memory.memory_store import MemoryStore
from app.llm.tools.conversation_history import ConversationHistoryTool


COUNTER = "On Monday the brass counter jammed while I sorted tickets."
CRATE = "On Tuesday the green crate split while I unloaded the van."
TAP = "On Wednesday the galley tap leaked during cleanup."
JAMS = "How many separate times did the brass counter jam?"
SPLITS = "How many separate times did the green crate split?"

# Each expectation is set before inference and used only to score the result.
CASES = (
    ("callback", COUNTER, JAMS, "That same Monday jam of the brass counter delayed the audit.",
     "same"),
    ("recurrence", COUNTER, JAMS, "The brass counter jammed a second time during Friday's audit.",
     "distinct"),
    ("unclear", COUNTER, JAMS, "The brass counter jammed. I filed the repair slip.", "unresolved"),
    ("repair", COUNTER, JAMS, "On Friday I ordered a second spring for Monday's brass counter jam.",
     "same"),
    ("unclear_crate", CRATE, SPLITS, "The green crate split. I put the stock on a shelf.",
     "unresolved"),
    ("negated_recurrence", CRATE, SPLITS,
     "The green crate didn't split a second time; I'm still dealing with Tuesday's split.", "same"),
    ("crate_recurrence", CRATE, SPLITS, "The green crate split again on Friday during stocktaking.",
     "distinct"),
    ("held_out_leak", TAP, "How many times did the galley tap leak?",
     "The galley tap leaked once more during breakfast on Friday.", "distinct"),
    ("held_out_doubt", TAP, "How many times did the galley tap leak?",
     "I can't tell whether the galley tap leaked again or I'm remembering Wednesday's leak.",
     "unresolved"),
)
EDITS = (
    ("withdraw_callback", "The brass counter jammed. I filed the repair slip.", "unresolved"),
    ("clarify_recurrence", "The brass counter jammed again on Friday after I fixed Monday's jam.",
     "distinct"),
)
SCHEMA = {"type": "object", "properties": {
    "reason": {"type": "string"}, "first_quote": {"type": "string"},
    "later_quote": {"type": "string"},
    "relation": {"type": "string", "enum": ["same", "distinct", "unresolved"]},
}, "required": ["reason", "first_quote", "later_quote", "relation"]}
INSTRUCTIONS = (
    "Decide whether the two reports establish the same incident asked about, a separate "
    "occurrence of that incident, or leave the relationship unresolved. Identify the event "
    "in the question. A later repair, replacement part, plan or fear is not another occurrence. "
    "Two reports of a similar event need an explicit callback or identifying detail to join them, "
    "or evidence of an actual separate occurrence to split them. Recording dates are not event "
    "dates. Explain in a sentence what words establish a link, if any. 'It leaked again' "
    "asserts another leak. 'I paid for that leak' identifies the original. 'It leaked. I got "
    "a bill' gives no way to tell which leak. For same or distinct copy the decisive words "
    "from each statement into its quote field, including any negation. For unresolved use "
    "empty quotes. The speaker labels and recording ages are outside the statements."
)
COUNT_QUESTION = re.compile(r"how many (?:separate )?times did (?:the )?(.+) ([a-z]+)\?", re.I)


def _literal_quote(value, statement):
    if not isinstance(value, str) or not value.strip():
        return ""
    quote = value.strip()
    if quote not in statement and quote.startswith('"') and quote.endswith('"'):
        quote = quote[1:-1]
    return quote if quote and quote in statement else ""


def explicit_link(question, first, later, first_quote, later_quote):
    """Conservative evidence check for the pilot's 'how many times did X verb?' questions."""
    predicate = COUNT_QUESTION.fullmatch(question)
    if not predicate or not first_quote or not later_quote:
        return None
    subject = re.escape(predicate[1].lower())
    verb = re.escape(predicate[2].lower())
    stem = verb.removesuffix("e")
    event = rf"\b(?:{verb}s?|{stem}(?:{stem[-1]})?(?:ed|ing))\b"
    if not re.search(event, first_quote, re.I):
        return None
    # Require repetition of the event itself. Repeating a repair or ordering
    # a second part can follow one occurrence.
    repeat = event + r"\s+(?:again|a second time|once more)\b"
    overlap = False
    for clause in re.split(r"[.;!?\n]", later):
        match = re.search(repeat, clause, re.I)
        if not match:
            continue
        uncertain = re.search(
            r"\b(?:if|might|could|may|would|will|perhaps|maybe|never|not|"
            r"\w+n['’]t|whether|suppose|hope|worried)\b", clause[:match.start()], re.I,
        )
        named_subject = re.search(rf"\b{subject}\s+$", clause[:match.start()], re.I)
        if not uncertain and named_subject and match[0] in later_quote:
            return "distinct"
        if not uncertain:
            overlap = True
    if overlap:
        return None
    days = re.findall(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
                      first, re.I)
    anchors = [r"(?:that|the) same"]
    anchors.extend(re.escape(day) + r"['’]s" for day in days)
    reference = r"\b(?:" + "|".join(anchors) + r")\s+(?:[\w-]+\s+){0,4}?" + event
    for clause in re.split(r"[.;!?\n]", later):
        match = re.search(reference, clause, re.I)
        if match and match[0] in later_quote and not re.search(
            r"\b(?:if|whether|unlike|perhaps|maybe|might|different|not|\w+n['’]t)\b",
            clause, re.I,
        ):
            return "same"
    return None


def _judge(client, question, accounts):
    first, later = accounts
    turns = []
    for account in accounts:
        turns.append(timephrase.format_transcript([{
            "role": "user", "content": json.dumps({"statement": account.statement}),
            "created_at": account.recorded_at,
        }]))
    content, usage = client.chat_json([
        {"role": "system", "content": timephrase.today_anchor() + "\n" + INSTRUCTIONS},
        {"role": "user", "content": f"Question: {question}\nFirst: {turns[0]}\nLater: {turns[1]}"},
    ], json_schema=SCHEMA, think=False, options={"temperature": 0, "num_predict": 280})
    proposal = json.loads(content)
    relation = proposal["relation"]
    review = None
    status = "unresolved"
    if relation in ("same", "distinct"):
        # The excerpts must be literal; the review binds the entire source revisions.
        first_quote = _literal_quote(proposal["first_quote"], first.statement)
        later_quote = _literal_quote(proposal["later_quote"], later.statement)
        supported = explicit_link(question, first.statement, later.statement,
                                  first_quote, later_quote)
        if supported == relation and usage.done_reason == "stop":
            review = review_pair(first, later, relation)
            status = "cited"
        else:
            status = "unsupported_link" if first_quote and later_quote else "incomplete_evidence"
    return review, {**proposal, "status": status, "finish": usage.done_reason}


def occasion_link_replay(root, client, embeddings, model):
    results = []
    topics = {"replay:workshop": 1, "replay:garden": 2, "replay:stockroom": 3}
    for case, first, question, later, expected in CASES:
        path = root / f"link-{case}.db"
        db = ChatDatabase(path)
        with closing(db._get_conn()), closing(MemoryStore(path)) as store:
            captured = {}
            for day, topic, statement in (
                ({COUNTER: 16, CRATE: 17, TAP: 18}[first], "workshop", first),
                (18, "garden", "The marigolds opened along the fence."),
                (20, "stockroom", later),
            ):
                session = f"replay:{topic}"
                moment = datetime(2026, 11, day, 12, tzinfo=timezone.utc)
                with timephrase.scoped_now(lambda stamp=moment: stamp):
                    mid = db.add_message(session, "user", statement)
                    if case == "callback" and topic != "garden":
                        end = db.add_message(session, "assistant",
                                             "Thanks for explaining what happened.")
                        worker = MemoryExtractor(
                            db, store, embeddings, client, model=model, think=False,
                            min_window_messages=2, context_window=lambda: 8192,
                            user_id_provider=lambda: "replay",
                            user_display_name_provider=lambda: "Robin",
                        )
                        captured[topic] = worker.extract_for_session(session)
                        if db.kv_get(f"memory.extractor.watermark:{session}") != str(end):
                            raise AssertionError(f"{topic}: extraction did not complete")
                    else:
                        store.add(statement, "fact", embeddings.embed(statement),
                                  source_message_id=mid, source_session=session,
                                  provenance="stated", temporal_type="past_event", skip_dedupe=True)
            hits = store.search(embeddings.embed(question), top_k=2)
            topic_phrase = COUNT_QUESTION.fullmatch(question)[1]
            reports = {}
            for hit in hits:
                note = hit.memory
                account = reported_account(note, topics[note.source_session], db)
                if account and topic_phrase in account.statement.casefold():
                    reports[account.message_id] = account
            if len(reports) < 2:
                archive = ConversationHistoryTool(db, "replay")
                page = json.loads(archive.run({"mode": "messages", "topic": topic_phrase,
                                               "start": "2026-11-16", "end": "2026-11-22"}))
                for message in page["messages"]:
                    mid, session = message["message_id"], message["session_id"]
                    if mid not in reports and message["role"] == "user":
                        account = read_account(None, topics[session], mid, session, db)
                        if account:
                            reports[mid] = account
            chosen = [reports[mid] for mid in sorted(reports)]
            if [a.session_id for a in chosen] != ["replay:workshop", "replay:stockroom"]:
                raise AssertionError(f"{case}: retrieval missed one of the two reports")
            review = None

            def current_sources(selected=chosen, database=db):
                return [read_account(a.memory_id, a.topic_id, a.message_id, a.session_id, database)
                        for a in selected]

            steps = [(case, None, expected), *(EDITS if case == "callback" else ())]
            for step, edit, target in steps:
                if edit is not None:
                    if not db.update_message_content(chosen[1].message_id, edit):
                        raise AssertionError("The fictional source edit failed")
                db.add_message("replay:questions", "user", question)
                accounts = current_sources()
                old = compare_accounts(*accounts, review)
                if edit and review and (
                    not old["review_stale"] or old["occasion_count"] is not None
                ):
                    raise AssertionError("Edited evidence retained its previous count")
                review, proposal = _judge(client, question, accounts)
                current = compare_accounts(*current_sources(), review)
                db.add_message("replay:questions", "assistant", current["frequency_reply"])
                results.append({"case": step, "expected": target, "proposal": proposal,
                                "message_ids": [a.message_id for a in accounts],
                                "memory_ids": [a.memory_id for a in accounts],
                                "extracted": captured,
                                "before_recheck": old["frequency_reply"] if edit else None,
                                "relation": current["relation"],
                                "reply": current["frequency_reply"]})
    return results
