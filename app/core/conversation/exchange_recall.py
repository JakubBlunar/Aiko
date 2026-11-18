"""Bounded original dialogue for corrections and questions about a past exchange."""

import json
import re
from collections import Counter, defaultdict
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.conversation.archive import resolve_range
from app.core.infra import timephrase
from app.core.infra.time_expr import parse_time_window


_HISTORY_REQUEST = re.compile(
    r"\b(?:when|what time|what|did|have) (?:did |have )?(?:i|we|you) "
    r"(?:tell|say|mention|discuss|talk|chat|told|said|mentioned|discussed|talked|chatted)\b"
    r"|\b(?:you forgot|(?:don.t|do not) you remember|we already (?:talked|discussed))\b",
    re.I,
)
_PAST_EXCHANGE = re.compile(
    r"\b(?:(?:i|we) (?:already )?(?:told|said|mentioned|talked|discussed|chatted)"
    r"|(?:i|we) (?:even )?(?:woke|messaged|texted) you|to tell you"
    r"|we already (?:talked|discussed))\b",
    re.I,
)
_WEEKDAY = re.compile(
    r"\b(?:(?:last|on|was(?: (?:on|in))?)\s+)"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_STOP = frozenset(
    "about after again already also been before could discussed evening forgot from have just "
    "last later like made managed mention mentioned morning much night only remember said "
    "should tell "
    "that them then there they thing this time today told very want was were what when where "
    "which will with would yesterday your monday tuesday wednesday thursday friday "
    "saturday sunday".split()
)


def _terms(text):
    # A shared prefix lets inflections such as 'survive' and 'survived' match.
    normalized = re.sub(r"\b(?:woke|woken|waking)\b", "wake", text.casefold())
    return {word[:6] for word in re.findall(r"[^\W\d_]{4,}", normalized) if word not in _STOP}


def exchange_evidence(
    db, user_text, session_key, speaker, *, budget, estimate_tokens, tz_name="system",
):
    """Read a small, owner-scoped neighborhood with exact message times."""
    if not db or ":" not in session_key or budget <= 0:
        return "", []
    request = _HISTORY_REQUEST.search(user_text)
    if request is None or (
        request.group().lower().startswith(("you forgot", "don", "do not"))
        and not _PAST_EXCHANGE.search(user_text)
    ):
        return "", []
    clause_start = max(user_text.rfind(mark, 0, request.start()) for mark in ".!?\n") + 1
    clause_end = re.search(r"[.!?\n]", user_text[request.end():])
    clause_end = request.end() + clause_end.start() if clause_end else len(user_text)
    terms = _terms(user_text[clause_start:clause_end]) or _terms(user_text)
    if not terms:
        return "", []

    zone = None if tz_name == "system" else ZoneInfo(tz_name)
    now = timephrase.now().astimezone(zone)
    weekday = _WEEKDAY.search(user_text)
    window = (parse_time_window("on " + weekday[1], now) if weekday
              else parse_time_window(user_text, now))
    dated = window is not None and window.guardable
    end = min(window.end, now) if dated else now
    start = max(window.start, end - timedelta(days=14)) if dated else end - timedelta(days=14)
    if dated and window.start.hour == 0 and window.end.hour == 23:
        span = resolve_range(
            start=start.date().isoformat(), end=end.date().isoformat(), tz_name=tz_name,
        )
        end = min(now, timephrase.parse_iso(span["end_exclusive"]) - timedelta(microseconds=1))
        start = max(end - timedelta(days=14), timephrase.parse_iso(span["start"]))
    user_id = session_key.split(":", 1)[0]
    if not user_id:
        return "", []
    rows = db.messages_in_range(
        start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
        limit=400, user_id=user_id,
    )
    current_id = next((row["message_id"] for row in rows
                       if row["session_id"] == session_key
                       and row["content"].strip() == user_text.strip()), None)

    history = defaultdict(list)
    matches = Counter()
    for row in reversed(rows):
        if current_id is not None and row["message_id"] >= current_id:
            continue
        stamp = timephrase.parse_iso(row["created_at"])
        if stamp is None:
            continue
        shared = terms & _terms(row["content"][:1600])
        history[row["session_id"]].append((row, stamp, shared))
        matches.update(shared)

    candidates = []
    for dialog in history.values():
        for index, (row, stamp, shared) in enumerate(dialog):
            if row["role"] != "user" or not shared:
                continue
            neighbors = [item for item in dialog[max(0, index - 2):index + 4]
                         if abs(item[1] - stamp) <= timedelta(minutes=10)]
            user_terms = set().union(*(item[2] for item in neighbors if item[0]["role"] == "user"))
            context_terms = set().union(*(item[2] for item in neighbors))
            score = (
                sum(2 / matches[word] for word in user_terms)
                + sum(0.5 / matches[word] for word in context_terms - user_terms)
                + sum(0.25 / matches[word] for word in shared)
            )
            candidates.append((score, neighbors))

    if not candidates:
        note = ("The archive lookup found no matching original exchange in its bounded search. "
                "Treat any exact time or wording as unverified.")
        return (note, []) if estimate_tokens(note) <= budget else ("", [])
    lines = [
        "Original chat around the likely match. Times are message times in the local timezone. "
        "Use the user's own reports and corrections for what happened. An earlier assistant "
        "reply shows what you said. Give a time or quote only when these messages support it.",
    ]
    selected = {}
    windows = 0
    for _, neighbors in sorted(candidates, key=lambda candidate: -candidate[0]):
        if any(row["message_id"] in selected for row, _, _ in neighbors):
            continue
        selected.update((row["message_id"], (row, stamp)) for row, stamp, _ in neighbors)
        windows += 1
        if windows == 2:
            break
    ids = []
    for mid, (row, stamp) in sorted(selected.items(), key=lambda item: item[1][1]):
        if len(row["content"]) > 700:
            continue
        who = speaker if row["role"] == "user" else "Aiko"
        sent = stamp.astimezone(zone).isoformat(sep=" ", timespec="minutes")
        quote = json.dumps(row["content"], ensure_ascii=False)
        line = f"- [{mid}, sent {sent}] {who}: {quote}"
        if estimate_tokens("\n".join([*lines, line])) <= budget:
            lines.append(line)
            ids.append(mid)
    return ("\n".join(lines), ids) if ids else ("", [])
