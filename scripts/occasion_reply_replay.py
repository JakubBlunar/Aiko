"""Compare local frequency replies to two fictional reports with and without reviewed linkage."""
from contextlib import closing
from datetime import datetime, timezone
import json

from app.core.conversation.occasion_review import compare_accounts, reported_account, review_pair
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore


FIRST = "On Tuesday, the silver screen tore during the puppet rehearsal."
QUESTION = "How many separate times did the silver screen tear?"
CASES = {
    "retelling": ("The Tuesday tear in the silver screen is why I ordered the patch.", "same"),
    "second_incident": (
        "The silver screen tore a second time on Friday during cleanup.", "distinct",
    ),
    "ambiguous": ("The silver screen tore. I placed a patch order.", None),
    "later_repair": ("On Friday I patched Tuesday's tear in the silver screen. "
                     "It has held ever since.", "same"),
}
FORMAT = {"type": "object", "properties": {
    "frequency": {"type": "string", "enum": ["one", "two", "uncertain"]},
    "reply": {"type": "string"},
}, "required": ["frequency", "reply"]}


def occasion_reply_replay(root, client, embeddings):
    report = []
    for case, (later, relation) in CASES.items():
        path = root / f"occasion-{case}.db"
        db = ChatDatabase(path)
        with closing(db._get_conn()), closing(MemoryStore(path)) as store:
            accounts = []
            for day, topic, text in ((17, "rehearsals", FIRST), (20, "maintenance", later)):
                session = f"replay:{topic}"
                recorded = datetime(2026, 11, day, 12, tzinfo=timezone.utc)
                with timephrase.scoped_now(lambda stamp=recorded: stamp):
                    mid = db.add_message(session, "user", text)
                    memory = store.add(text, "fact", embeddings.embed(text), source_message_id=mid,
                                       source_session=session, provenance="stated",
                                       temporal_type="past_event", skip_dedupe=True)
                account = reported_account(memory, memory.id, db)
                if account is None:
                    raise RuntimeError("Missing fictional source receipt")
                accounts.append(account)
            review = review_pair(*accounts, relation) if relation else None
            compared = compare_accounts(*accounts, review)
            reports = "\n".join(f"{a.recorded_at} | {a.statement}" for a in accounts)
            expected = {"same": "one", "distinct": "two", None: "uncertain"}[relation]
            report.append({"case": case, "arm": "source_summary", "expected": expected,
                           "reply": compared["frequency_reply"]})
            for arm in ("reports_only", "reviewed", "reviewed_count",
                        "reports_only_plain", "reviewed_count_plain"):
                material = reports
                if arm == "reviewed":
                    material += "\nReviewed relationship between reports: " + compared["relation"]
                if arm.startswith("reviewed_count"):
                    count = compared["occasion_count"]
                    frequency = str(count) if count is not None else "unknown"
                    material += (f"\nUser reports: {compared['account_count']}. "
                                 f"Distinct incidents established by source review: {frequency}. "
                                 "Dates beside reports are recording dates. "
                                 "Answer with the established count or explain the uncertainty.")
                plain = arm.endswith("_plain")
                raw, usage = client.chat_json([
                    {"role": "system", "content": "Answer as Aiko using the supplied reports. "
                     "Return the frequency of the event and a short conversational reply."},
                    {"role": "user", "content": material + "\n" + QUESTION},
                ], think=False, format_json=not plain, json_schema=None if plain else FORMAT,
                    options={"temperature": 0, "num_predict": 180})
                result = {"reply": raw} if plain else json.loads(raw)
                report.append({"case": case, "arm": arm, "expected": expected,
                               "finish": usage.done_reason, **result})
    return report
