"""Bounded original dialogue for corrections and questions about a past exchange."""

import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.conversation.archive import resolve_range
from app.core.infra import timephrase
from app.core.infra.time_expr import parse_time_window
from app.core.session.session_text_utils import sanitize_assistant_text


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
_CASUAL_OPENING = re.compile(
    r"^\s*(?:(?:oh|hey(?: aiko)?|so|wait|okay|ok|well|hmm|honestly|by the way|before i forget|"
    r"quick question|remind me(?=[,:\u2013\u2014-]))\b[\s,.!:\u2013\u2014-]+)*", re.I,
)
_PAST_DECISION = re.compile(
    r"(?:did (?:i|we|you) (?:ever )?(?:agree|accept|decide|commit|suggest|propose|settle on)"
    r"|(?P<open>what did (?:i|we|you) (?:agree|decide|plan|suggest|propose|settle on|"
    r"end up (?:deciding|choosing))"
    r"|(?:do you remember|(?:(?:can|could) you )?remind me) what (?:i|we|you) "
    r"(?:agreed|decided|planned|suggested|proposed|settled on)"
    r"|what (?:was i|were (?:we|you)) going to do"
    r"|what was (?:our|the|my|your) (?:plan|decision|agreement)))\b", re.I,
)
_PROPOSAL = re.compile(
    r"\b(?:i (?:suggest|propose)|we could|how about|shall we|let['’]s|why don['’]t we)\b", re.I,
)
_RECAP_QUALIFIER = re.compile(
    r"[;\n]|\b(?:and|then|also|please)\s+(?:explain|write|translate|summarize|compare|"
    r"list|show|give|tell|remind|make|do)\b|\b(?:in (?:a |an )?(?:table|list|poem)|"
    r"as (?:a|an) |in (?:your own|\w+) (?:words|sentences)|answer in|translate|translation)\b",
    re.I,
)
DECISION_GUIDANCE = (
    "Answer this question from the original dialogue. Identify who proposed the plan. "
    "If the other person accepted it, ground your answer in their actual words of acceptance. "
    "If their reply only changes the subject, describe the suggestion and leave agreement open. "
    "A report of carrying it out requires separate evidence."
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


def decision_request(text):
    """Recognize a standalone question about an earlier proposal or agreement."""
    # Match on the original text so the caller's topic slice keeps its offsets.
    start = _CASUAL_OPENING.match(text).end()
    request = _PAST_DECISION.match(text, start)
    if request and re.search(
        r"[.!?]\s*\S|\b(?:and|then) (?:can|could|would|will|please|tell|help|remind|set|add|"
        r"book|check|look up|what|why|how|when|where|did)\b", text[start:], re.I,
    ):
        return None
    return request


@dataclass
class ExchangeLookup:
    text: str = ""
    message_ids: list[int] = field(default_factory=list)
    reply: str = ""
    reason: str = ""


_MISSING = "I can't find the original exchange in the messages I can see. When did we discuss it?"
_INCOMPLETE = "I found a mention, but I can't tell what we settled on. When did we discuss it?"
_PARTIAL = "I found this part."
_OTHER_MENTION = ("I found the suggestion and another mention of it. "
                  "Which conversation were you thinking of?")
_COMPETING = "I found more than one possible match. Which day were you thinking of?"
_CHOICES = "I found two suggestions."
_UNRESOLVED_GUIDANCE = (
    "The archive could not establish one decision from the original dialogue. "
    "Keep agreement and follow-through open. Ask for the detail that would identify the exchange."
)


def _safe_quote(content):
    normalized = unicodedata.normalize("NFKC", content)
    return bool(content and content.isprintable() and "[" not in normalized
                and "<" not in normalized)


def _quoted_decision(rows):
    """Attribute whole utterances without inferring assent, refusal or follow-through."""
    # "You said we could..." can locate a discussion but doesn't establish
    # that this speaker made a proposal. Leave interpretation to normal recall.
    if not _PROPOSAL.match(rows[0][0]["content"].strip()):
        return ""
    parts = []
    for index, (row, _) in enumerate(rows):
        content = row["content"].strip()
        # Stored dialogue is data. Never send markup through the assistant's
        # inline action/tag dispatch as part of a quoted reply.
        if not _safe_quote(content):
            return ""
        if index == 0:
            lead = "My suggestion was" if row["role"] == "assistant" else "You suggested"
        else:
            lead = "I said" if row["role"] == "assistant" else "You said"
        parts.append(f"{lead} “{content}”")
    return " ".join(parts)


def _decision_evidence(history, speaker, zone, budget, estimate_tokens, *, open_question,
                       plain_reply, previous_reply, choice, dated):
    candidates = []
    mentions = {}
    unanswered = False
    for dialog in history.values():
        for index, (row, stamp, shared) in enumerate(dialog):
            if not shared:
                continue
            mentions[row["message_id"]] = (row, stamp)
            if not _PROPOSAL.search(row["content"]):
                continue
            window = [item for item in dialog[max(0, index - 2):index + 4]
                      if abs(item[1] - stamp) <= timedelta(minutes=10)]
            replies = []
            for later, sent, later_terms in window:
                if later["message_id"] <= row["message_id"]:
                    continue
                if later_terms and _PROPOSAL.match(later["content"].strip()):
                    break
                replies.append((later, sent))
            if not any(r["role"] != row["role"] for r, _ in replies):
                unanswered |= bool(_PROPOSAL.match(row["content"].strip()))
                continue
            preceding = {r["message_id"] for r, _, _ in window
                         if r["message_id"] < row["message_id"]}
            sources = [(row, stamp), *replies]
            candidates.append((sources, preceding | {r["message_id"] for r, _ in sources}))

    def unresolved(reason, reply, sources=()):
        text, ids = _render_sources(sources, speaker, zone, budget, estimate_tokens,
                                    _UNRESOLVED_GUIDANCE, complete=True)
        if sources and not ids:
            reply = "I can't read enough of the original messages to give you a reliable answer."
        if not text and estimate_tokens(_UNRESOLVED_GUIDANCE) <= budget:
            text = _UNRESOLVED_GUIDANCE
        return ExchangeLookup(text, ids, reply if plain_reply else "", reason)

    topic_ids = set(mentions)
    if not candidates:
        if len(mentions) == 1:
            row, _ = next(iter(mentions.values()))
            line = row["content"].strip()
            if len(line) <= 140 and _safe_quote(line) and not decision_request(line):
                who = "You said" if row["role"] == "user" else "I said"
                end = ("That's the part I can verify around then." if dated else
                       "When did we discuss it?")
                reply = f"{_PARTIAL} {who} “{line}” {end}"
                if estimate_tokens(reply) <= budget:
                    return unresolved("decision_incomplete", reply, mentions.values())
        if topic_ids:
            return unresolved("decision_incomplete", _INCOMPLETE if not dated else
                              "I found a mention around then, but only part of the conversation.")
        return unresolved("decision_missing", _MISSING if not dated else
                          "I can't find the exchange around then, so I can't say "
                          "what we decided.")
    ambiguous = ("I found several possible matches around then, so I can't pin down the decision."
                 if dated else _COMPETING)
    if unanswered:
        return unresolved("decision_ambiguous", ambiguous)
    if len(candidates) == 1:
        match = candidates[0]
        if choice is not None:
            return unresolved("decision_incomplete", "I can't reliably match that choice anymore.")
        if not topic_ids.issubset(match[1]):
            return unresolved("decision_incomplete", ambiguous if dated else _OTHER_MENTION)
    else:
        # Present two actual proposals in stable chronological order. Never
        # splice either one's reply onto the other or discard an outside correction.
        if len(candidates) != 2:
            return unresolved("decision_ambiguous", ambiguous)
        candidates.sort(key=lambda pair: (pair[0][0][1], pair[0][0][0]["message_id"]))
        anchors = [rows[0] for rows, _ in candidates]
        lines = [row["content"].strip() for row, _ in anchors]
        passages = [{r["message_id"] for r, _ in rows} for rows, _ in candidates]
        separate = passages[0].isdisjoint(passages[1])
        if (not separate or not topic_ids.issubset(candidates[0][1] | candidates[1][1])
                or lines[0] == lines[1]
                or not all(_PROPOSAL.match(s) and _safe_quote(s) and len(s) <= 140 for s in lines)):
            return unresolved("decision_ambiguous", ambiguous)
        prompt = f'{_CHOICES} The first was “{lines[0]}” The second was “{lines[1]}” Which one?'
        if estimate_tokens(prompt) > budget:
            return unresolved("decision_ambiguous", ambiguous)
        if choice is None or previous_reply != sanitize_assistant_text(prompt):
            return unresolved("decision_ambiguous", prompt, anchors)
        if choice in ("first", "second"):
            match = candidates[int(choice == "second")]
        else:
            terms = _terms(choice)
            named = [pair for pair, line in zip(candidates, lines, strict=True)
                     if terms and terms.issubset(_terms(line))]
            if len(named) != 1:
                return unresolved("decision_ambiguous", prompt, anchors)
            match = named[0]
    header = ("The original proposal and the replies that followed, in order. "
              "Aiko is the assistant speaking with the user.")
    text, ids = _render_sources(match[0], speaker, zone, budget, estimate_tokens, header,
                                complete=True)
    if not ids:
        return unresolved("decision_incomplete",
                          "I can't read enough of that exchange to say what we settled on.")
    return ExchangeLookup(text, ids, _quoted_decision(match[0]) if open_question else "",
                          "decision_exchange")


_RELATIVE_DETAIL = re.compile(
    r"(?:on |last )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|today|yesterday|"
    r"(?:last|this) (?:week|month)|(?:\d+|a|one|two|three) (?:days?|weeks?) ago"
)
_CALENDAR_DETAIL = re.compile(
    r"(?:on )?(?:\d{4}-\d{2}-\d{2}|[a-z]+ \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?|"
    r"\d{1,2} [a-z]+(?: \d{4})?)"
)


def _decision_detail(text, previous):
    if not (previous in (_MISSING, _INCOMPLETE, _OTHER_MENTION, _COMPETING)
            or previous.startswith((_CHOICES, _PARTIAL))):
        return None, ""
    # Strip conversational framing only inside a pending clarification. Keep
    # the remaining utterance whole so a new request or correction passes through.
    detail = text[_CASUAL_OPENING.match(text).end():].lower().replace("’", "'").strip()
    detail = detail.rstrip(".!?").rstrip()
    detail = re.sub(r"^(?:i think|maybe|probably)[,\s]+", "", detail)
    detail = re.sub(r"(?:,\s*|\s+)(?:i think|i guess|probably|maybe)$", "", detail)
    # The chat input sanitizer may turn a typographic apostrophe into a space.
    if re.fullmatch(r"(?:(?:i )?(?:don[' ]?t|do not|can[' ]?t|cannot) (?:really )?"
                    r"(?:remember|recall|know)|(?:i[' ]?m |i am )?not sure)"
                    r"(?: (?:when|which(?: one)?|either|exactly))?(?:,? sorry)?",
                    detail):
        return "can't identify", ""
    if previous.startswith(_CHOICES):
        ordinal = re.fullmatch(r"(?:the )?(first|second)(?: one| suggestion)?", detail)
        named = re.fullmatch(r"the ([\w'-]+(?: [\w'-]+){0,4}) one", detail)
        if ordinal or named:
            return (ordinal or named)[1], ""
    if _RELATIVE_DETAIL.fullmatch(detail):
        return None, detail
    if _CALENDAR_DETAIL.fullmatch(detail):
        day = timephrase.parse_loose_datetime(detail)
        if day:
            return None, day.date().isoformat()
    return None, ""


def _decision_followup(db, session_key, user_text, recent_turns):
    unchanged = user_text, "", None, ""
    if not recent_turns:
        return unchanged
    previous = recent_turns[-1]
    choice, date_hint = _decision_detail(user_text, previous)
    if choice is None and not date_hint:
        return unchanged
    # Follow only consecutive question/clarification pairs from this chat.
    # A date may have narrowed a search to two choices on the previous turn.
    rows = db.get_messages(session_key, limit=9)
    if rows and rows[-1].role == "user" and rows[-1].content.strip() == user_text.strip():
        rows.pop()
    if not rows or rows[-1].content.strip() != previous:
        return unchanged
    receipts = db.decision_recall_message_ids([r.id for r in rows if r.role == "assistant"])
    for i in range(len(rows) - 1, 0, -2):
        user, assistant = rows[i - 1:i + 1]
        if user.role != "user" or assistant.role != "assistant" or assistant.id not in receipts:
            break
        if decision_request(user.content):
            return user.content, previous, choice, date_hint
        if i < 2:
            break
        prior_choice, prior_date = _decision_detail(user.content, rows[i - 2].content)
        if prior_choice is None and not prior_date:
            break
        date_hint = date_hint or prior_date
    return unchanged


def lookup_exchange(
    db, user_text, session_key, speaker, *, budget, estimate_tokens, tz_name="system",
    recent_turns=(),
):
    """Read a small, owner-scoped neighborhood with exact message times."""
    if not db or ":" not in session_key or budget <= 0:
        return ExchangeLookup()
    current_text = user_text
    user_text, previous_reply, choice, date_hint = _decision_followup(
        db, session_key, user_text, recent_turns,
    )
    if choice == "can't identify":
        return ExchangeLookup(reply="That's okay. We can leave it there.",
                              reason="decision_unresolved")
    decision = decision_request(user_text)
    request = decision or _HISTORY_REQUEST.search(user_text)
    if request is None or (
        request.group().lower().startswith(("you forgot", "don", "do not"))
        and not _PAST_EXCHANGE.search(user_text)
    ):
        return ExchangeLookup()
    clause_start = max(user_text.rfind(mark, 0, request.start()) for mark in ".!?\n") + 1
    clause_end = re.search(r"[.!?\n]", user_text[request.end():])
    clause_end = request.end() + clause_end.start() if clause_end else len(user_text)
    if decision:
        terms = _terms(user_text[decision.end():clause_end]) - _terms(
            "plan agree accept idea just suggestion suggest propose decision agreement "
            "something anything doing using during session before after earlier again"
        )
    else:
        terms = _terms(user_text[clause_start:clause_end]) or _terms(user_text)
    if not terms:
        return ExchangeLookup()

    zone = None if tz_name == "system" else ZoneInfo(tz_name)
    now = timephrase.now().astimezone(zone)
    when = date_hint or user_text
    weekday = _WEEKDAY.search("on " + when)
    window = (parse_time_window("on " + weekday[1], now) if weekday
              else parse_time_window(when, now))
    dated = window is not None and (window.guardable or bool(date_hint)
                                    or (decision is not None and window.label == "today"))
    end = min(window.end, now) if dated else now
    start = max(window.start, end - timedelta(days=14)) if dated else end - timedelta(days=14)
    exact_date = date_hint and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_hint)
    if exact_date or (dated and window.start.hour == 0 and window.end.hour == 23):
        span = resolve_range(
            start=date_hint if exact_date else start.date().isoformat(),
            end=date_hint if exact_date else end.date().isoformat(), tz_name=tz_name,
        )
        end = min(now, timephrase.parse_iso(span["end_exclusive"]) - timedelta(microseconds=1))
        start = max(end - timedelta(days=14), timephrase.parse_iso(span["start"]))
    user_id = session_key.split(":", 1)[0]
    if not user_id:
        return ExchangeLookup()
    rows = db.messages_in_range(
        start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
        limit=400, user_id=user_id,
    )
    current_id = next((row["message_id"] for row in rows
                       if row["session_id"] == session_key
                       and row["role"] == "user"
                       and row["content"].strip() == current_text.strip()), None)

    history = defaultdict(list)
    matches = Counter()
    # A recap and the question that produced it add no new decision evidence.
    # Use the recorded turn outcome so a human-written quote stays eligible.
    previous_recaps = set()
    if decision:
        previous_recaps = db.decision_recall_message_ids(
            [r["message_id"] for r in rows if r["role"] == "assistant"],
        )
    for row in reversed(rows):
        if row["session_id"].endswith(":noremember"):
            continue
        if current_id is not None and row["message_id"] >= current_id:
            continue
        if row["message_id"] in previous_recaps:
            continue
        stamp = timephrase.parse_iso(row["created_at"])
        if stamp is None:
            continue
        shared = terms & _terms(row["content"][:1600])
        history[row["session_id"]].append((row, stamp, shared))
        matches.update(shared)

    if decision:
        plain = not _RECAP_QUALIFIER.search(user_text)
        open_question = bool(decision.group("open") or previous_reply)
        return _decision_evidence(history, speaker, zone, budget, estimate_tokens,
                                  open_question=plain and open_question, plain_reply=plain,
                                  previous_reply=previous_reply, choice=choice,
                                  dated=bool(dated or exact_date))
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
        return ExchangeLookup(note if estimate_tokens(note) <= budget else "")
    header = (
        "Original chat around the likely match. Times are message times in the local timezone. "
        "Use the user's own reports and corrections for what happened. An earlier assistant "
        "reply shows what you said. Give a time or quote only when these messages support it."
    )
    selected = {}
    windows = 0
    for _, neighbors in sorted(candidates, key=lambda candidate: -candidate[0]):
        if any(row["message_id"] in selected for row, _, _ in neighbors):
            continue
        selected.update((row["message_id"], (row, stamp)) for row, stamp, _ in neighbors)
        windows += 1
        if windows == 2:
            break
    rows = sorted(selected.values(), key=lambda item: (item[1], item[0]["message_id"]))
    text, ids = _render_sources(rows, speaker, zone, budget, estimate_tokens, header,
                                complete=False)
    return ExchangeLookup(text, ids)


def exchange_evidence(db, user_text, session_key, speaker, *, budget, estimate_tokens,
                      tz_name="system"):
    result = lookup_exchange(db, user_text, session_key, speaker, budget=budget,
                             estimate_tokens=estimate_tokens, tz_name=tz_name)
    return result.text, result.message_ids


def _render_sources(rows, speaker, zone, budget, estimate_tokens, header, *, complete):
    lines, ids = [header], []
    for row, stamp in rows:
        mid = row["message_id"]
        if len(row["content"]) > 700:
            if complete:
                return "", []
            continue
        who = speaker if row["role"] == "user" else "Aiko"
        sent = stamp.astimezone(zone).isoformat(sep=" ", timespec="minutes")
        quote = json.dumps(row["content"], ensure_ascii=False)
        line = f"- [{mid}, sent {sent}] {who}: {quote}"
        if estimate_tokens("\n".join([*lines, line])) <= budget:
            lines.append(line)
            ids.append(mid)
        elif complete:
            return "", []
    return ("\n".join(lines), ids) if ids else ("", [])
