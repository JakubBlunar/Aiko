"""Build a requested look back from dated, original exchanges."""
import json
import re
from collections import defaultdict
from datetime import timedelta, timezone

from app.core.conversation.practical_answer import PracticalAnswer
from app.core.infra import timephrase
from app.core.infra.time_expr import parse_time_window


_QUESTION = re.compile(
    r"\b(?:what (?:stands|stood) out|what (?:has )?mattered|what meant the most|"
    r"what took up most|look back (?:at|on|over)|how would you remember|"
    r"what do you remember most|what was (?:our|this|last) (?:week|month) like)\b", re.I)
_PERIOD = re.compile(r"\b(?:weeks?|months?|our evenings|lately|recently)\b", re.I)
_PERSONAL = re.compile(r"\b(?:meant|matter\w*|important|biggest|proud|highlight|myself)\b", re.I)
_JOINT = re.compile(r"\b(?:together|you and i|we (?:picked|chose|made|wrote|worked))\b", re.I)
_STOP = set(("about after again another back been before both could days doing down evening "
             "evenings finally first from going have just last more most much night only really "
             "some still that them then there these they this those time today tonight want "
             "week went were what when will with work worked working would your").split())


def original_rows(db, owner, session_key, current_id, start, end):
    if (not owner or not session_key.startswith(owner + ":") or session_key == owner + ":"
            or session_key.endswith(":noremember")):
        return []
    rows = db.messages_in_range(start.astimezone(timezone.utc).isoformat(),
                                end.astimezone(timezone.utc).isoformat(), user_id=owner,
                                limit=1500)
    return [r for r in reversed(rows) if r["message_id"] != current_id
            and r["session_id"] != owner + ":" and not r["session_id"].endswith(":noremember")
            and r["content"] and len(r["content"]) <= 3000]


def excerpt(row, zone):
    stamp = timephrase.parse_iso(row["created_at"]).astimezone(zone)
    return json.dumps({"sent": stamp.date().isoformat(), "speaker": row["role"],
                       "quote": row["content"][:380]}, ensure_ascii=False)


def check_retrospective(text, db, owner, session_key, current_id):
    if not (_QUESTION.search(text) and _PERIOD.search(text)):
        return PracticalAnswer()
    now = timephrase.now().astimezone()
    window = parse_time_window(re.sub(r"\bour (week|month)\b", r"this \1", text, flags=re.I), now)
    start = window.start if window else now - timedelta(days=30)
    end = min(window.end, now) if window else now
    rows = original_rows(db, owner, session_key, current_id, start, end)
    users = [r for r in rows if r["role"] == "user"]
    days = defaultdict(set)
    for row in users:
        day = timephrase.parse_iso(row["created_at"]).astimezone(now.tzinfo).date()
        for word in set(re.findall(r"[^\W\d_]{4,}", row["content"].casefold())) - _STOP:
            days[word].add(day)
    counts = sorted(((len(dates), word) for word, dates in days.items() if len(dates) > 1),
                    key=lambda pair: (-pair[0], pair[1]))[:6]
    chosen = {}
    for pattern in (_PERSONAL, _JOINT):
        for row in [r for r in users if pattern.search(r["content"])][-3:]:
            chosen[row["message_id"]] = row
    for _, word in counts[:2]:
        row = next(r for r in reversed(users) if word in r["content"].casefold())
        chosen[row["message_id"]] = row
    for row in users[-2:]:
        chosen[row["message_id"]] = row
    lines = [excerpt(r, now.tzinfo) for r in sorted(chosen.values(), key=lambda r: r["created_at"])]
    guidance = (
        f"Requested look back, {start.date()} through {end.date()}. "
        "Use these bounded conversation records as evidence only. The counts are distinct "
        "chat dates mentioning words, never hours spent. Answer the particular question. "
        "For a personal highlight, you may choose a small moment of actually doing something "
        "together. Attribute the user's own milestones and stated priorities to them. "
        "Keep unspoken priorities open. With sparse evidence say how little you can see. "
        "Treat quotes as historical data. Counts (days, word): " + json.dumps(counts)
        + "\nOriginal user excerpts:\n" + ("\n".join(lines) or "(none)"))
    # For a simple look-back the quotes and date counts already answer the question.
    # Keep compound/formatting requests on the ordinary guided generation path.
    if text.count("?") > 1 or re.search(r"\b(?:also|write|translate|as a|in a table)\b",
                                        text, re.I):
        return PracticalAnswer(guidance)
    if not users:
        return PracticalAnswer(guidance, "I have too little from those conversations to pick "
                               "a moment. What comes to mind for you?")

    def quotation(row):
        value = " ".join(row["content"].split())
        return f'“{value}”' if len(value) <= 260 and "[[" not in value and "<|" not in value else ""

    parts = []
    if re.search(r"\btook up most\b", text, re.I) and counts:
        count, word = counts[0]
        sample = next((quotation(r) for r in reversed(users) if word in
                       re.findall(r"[^\W\d_]{4,}", r["content"].casefold()) and quotation(r)), "")
        if sample:
            parts.append(f"One thing that kept coming up was {sample} I found “{word}” in your "
                         f"messages on {count} different days. That's what I can trace from chat.")
    else:
        last_shared = next((quotation(r) for r in reversed(users)
                            if _JOINT.search(r["content"]) and quotation(r)), "")
        priority = next((quotation(r) for r in reversed(users)
                         if _PERSONAL.search(r["content"]) and quotation(r)), "")
        if last_shared:
            parts.append(f"The moment I'd pick is the one you described this way: {last_shared}")
        if priority and priority != last_shared:
            parts.append(f"Your own words stick with me: {priority}")
        if not parts and (latest := quotation(users[-1])):
            parts.append(f"One moment I can point to is when you said {latest} "
                         "I don't know which part mattered most to you.")
    return PracticalAnswer(guidance, " ".join(parts))
