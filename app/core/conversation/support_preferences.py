"""J14 pilot for explicitly stated or agreed responses to venting."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from app.core.conversation.implicit_need import (
    MODE_NEUTRAL,
    MODE_PROBLEM_SOLVE,
    MODE_WITNESS,
    explicit_reply_mode,
    unquoted_text,
)
from app.core.infra import timephrase


_WHEN_VENT = r"(?:when i vent|(?:when|while) i(?:'m| am) venting)(?P<work> about work)?"
_LISTEN = r"(?:please )?(?:just )?(?:listen(?: first)?|hear me out)"
_ASK_FOR_IDEAS = r"i (?:actually |do )?(?:want|prefer) (?:ideas|advice|suggestions)"
_PREFERENCE = re.compile(
    rf"(?:actually, )?{_WHEN_VENT},? (?P<response>{_LISTEN}|{_ASK_FOR_IDEAS})",
)
_WORK_PREFERENCE = re.compile(
    rf"(?:actually, )?for work(?: (?:things|stuff|problems|issues))?,? {_ASK_FOR_IDEAS}",
)
_WORK_QUESTION = re.compile(
    r"when you vent(?: about work)?,? do you want me to listen first\?",
)
_CORRECTION = re.compile(
    r"(?:i (?:just |only )?(?:wanted|needed) (?:company|someone to listen|you to listen)"
    r"(?:,? not (?:advice|solutions))?|"
    r"i was (?:just |only )?(?P<vent>venting)(?P<work> about work)?,? not asking for advice)",
)
_OFFER = re.compile(
    r"(?:(?:i'm sorry(?: i jumped in with advice)?|sorry|i jumped in with advice)\. )?"
    r"(?:next time(?P<vent> you(?: need to)? vent(?P<work> about work)?)?,? "
    r"i(?:'ll| will) (?:just )?listen first|i(?:'ll| will) listen first next time)",
)
_ENDORSEMENT = re.compile(
    r"(?:yes,? please|yes,? let's do that|yes,? that's what i want(?: next time)?)",
)
_OUTCOME = re.compile(
    r"it helped when you listened first (?:while|when) i was venting(?P<work> about work)?",
)
_HISTORY_QUESTION = re.compile(
    r"\b(?:what did we agree|did we agree|(?P<outcome>did (?:it|that|listening first) help))\b",
    re.I,
)
_SUPPORT_TOPIC = re.compile(r"\b(?:vent(?:ing|ed)?|listen(?:ing|ed)?|advice|ideas)\b", re.I)
_VENT_OPENING = re.compile(
    r"^(?:ugh[,!.] *|hey[, ]+)?(?:i (?:really )?(?:need|want) to vent|"
    r"i(?:'m| am) (?:just )?venting|just venting|can i vent)\b", re.I,
)
_WORK_TOPIC = re.compile(
    r"\b(?:(?:at|about|from|for) work|my (?:job|boss|manager))\b", re.I,
)
_WORK_VENT = re.compile(r"^\s+about (?:work|my (?:job|boss|manager))\b", re.I)
_PAST_FRAME = re.compile(
    r"\b(?:for (?:a|the) (?:story|scene|script|role.?play)|as an example|"
    r"hypothetically|pretend|imagine)\b", re.I,
)
_LOOKBACK = timedelta(days=30)
_MAX_ROWS = 400


@dataclass(frozen=True, slots=True)
class SupportContext:
    mode: str
    prompt: str


@dataclass(frozen=True, slots=True)
class _Preference:
    mode: str
    scope: str
    sources: tuple[dict, ...]

    @property
    def message_id(self) -> int:
        return self.sources[-1]["message_id"]


def _statement(row: dict) -> str:
    text = row["content"]
    if len(text) > 220 or "\n" in text:
        return ""
    return " ".join(text.lower().replace("’", "'").split()).rstrip(".!")


def _in_one_exchange(rows: tuple[dict, ...]) -> bool:
    first, last = (timephrase.parse_iso(row["created_at"]) for row in (rows[0], rows[-1]))
    return (
        first is not None and last is not None
        and timedelta() <= last - first <= timedelta(minutes=10)
    )


def _read_candidates(rows: list[dict]):
    sessions = defaultdict(list)
    for row in reversed(rows):
        sessions[row["session_id"]].append(row)
    preferences, outcomes = [], []
    for dialog in sessions.values():
        for index, row in enumerate(dialog):
            if row["role"] != "user":
                continue
            statement = _statement(row)
            stated = _PREFERENCE.fullmatch(statement)
            if stated:
                mode = MODE_WITNESS if re.fullmatch(_LISTEN, stated["response"]) else (
                    MODE_PROBLEM_SOLVE
                )
                preferences.append(_Preference(mode, "work" if stated["work"] else "vent", (row,)))
            elif (_WORK_PREFERENCE.fullmatch(statement) and index > 0
                  and dialog[index - 1]["role"] == "assistant"
                  and _WORK_QUESTION.fullmatch(_statement(dialog[index - 1]))
                  and _in_one_exchange((dialog[index - 1], row))):
                preferences.append(_Preference(
                    MODE_PROBLEM_SOLVE, "work", (dialog[index - 1], row),
                ))
            elif outcome := _OUTCOME.fullmatch(statement):
                outcomes.append(("work" if outcome["work"] else "vent", row))
            elif _ENDORSEMENT.fullmatch(statement) and index >= 2:
                correction, offer = dialog[index - 2:index]
                clarified = _CORRECTION.fullmatch(_statement(correction))
                offered = _OFFER.fullmatch(_statement(offer))
                sources = (correction, offer, row)
                if (correction["role"] == "user" and offer["role"] == "assistant"
                        and clarified and offered and (clarified["vent"] or offered["vent"])
                        and _in_one_exchange(sources)):
                    scope = "work" if clarified["work"] or offered["work"] else "vent"
                    preferences.append(_Preference(
                        MODE_WITNESS, scope, sources,
                    ))
    return sorted(preferences, key=lambda item: item.message_id), outcomes


def _source_line(row: dict, now) -> str:
    return f"[Message {row['message_id']}] " + timephrase.format_transcript([row], now)


def _history_prompt(preferences, outcomes, scope, now, *, asks_outcome) -> str:
    relevant = [item for item in preferences if item.scope == scope or scope == "all"]
    if scope == "work" and not relevant:
        relevant = [item for item in preferences if item.scope == "vent"]
    reported = max(
        (row for name, row in outcomes if scope == "all" or name == scope),
        key=lambda row: row["message_id"], default=None,
    )
    if asks_outcome:
        if reported is None:
            return (
                "The outcome of listening first is unconfirmed. Answer in one sentence "
                "saying you don't know whether it helped. Leave the experience and reason open."
            )
        return (
            "Answer the outcome question from the user's report: "
            + _source_line(reported, now)
            + "\nIn one sentence, say what the user reported. Leave the reason open "
            "unless they stated it."
        )
    if not relevant and reported is None:
        return (
            "The bounded support-history lookup found no explicit preference, agreement or "
            "reported outcome for this question. Briefly say you can't verify an agreement. "
            "Leave past behavior and usual habits unspecified."
        )
    lines = [
        "Use these original, dated messages as quoted evidence for the question.",
        "Separate a stated preference, an accepted offer, and a user-reported outcome.",
    ]
    for name in dict.fromkeys(item.scope for item in relevant):
        matching = [item for item in relevant if item.scope == name]
        latest = matching[-1]
        agreement = next((item for item in reversed(matching) if len(item.sources) == 3), None)
        selected = (agreement, latest) if agreement is not None and agreement != latest else (
            latest,
        )
        for item in selected:
            label = "Accepted offer" if len(item.sources) == 3 else "User's stated preference"
            lines.append(f"{label} for {'work vents' if name == 'work' else 'venting'}:")
            lines.extend(_source_line(row, now) for row in item.sources)
    if reported is not None:
        lines.append("User-reported outcome about listening: " + _source_line(reported, now))
    lines.append(
        "If an agreement or outcome lacks explicit evidence here, say what is unknown. "
        "Keep reported effects at the level the user gave. If they only said it helped, "
        "say it helped and leave the reason open."
    )
    return "\n".join(lines)


def support_context(db, session_key: str, user_text: str) -> SupportContext | None:
    """Resolve guidance from the owner's retained dialogue on relevant turns."""
    user_id, separator, session = session_key.partition(":")
    if db is None or not user_id or not separator or not session:
        return None
    text = unquoted_text(user_text)
    if _PAST_FRAME.search(text):
        return None
    history = _HISTORY_QUESTION.search(text) if _SUPPORT_TOPIC.search(text) else None
    opening = _VENT_OPENING.match(text)
    if not history and (opening is None or explicit_reply_mode(text)):
        return None
    now = timephrase.utcnow()
    rows = db.messages_in_range(
        (now - _LOOKBACK).isoformat(), now.isoformat(), limit=_MAX_ROWS, user_id=user_id,
    )
    preferences, outcomes = _read_candidates(rows)
    if history:
        scope = "work" if _WORK_TOPIC.search(text) else "all"
        return SupportContext(
            MODE_NEUTRAL, _history_prompt(preferences, outcomes, scope, now,
                                          asks_outcome=bool(history["outcome"])),
        )
    scope = "work" if _WORK_VENT.match(text[opening.end():]) else "vent"
    by_scope = {item.scope: item for item in preferences}
    current = by_scope.get(scope) or by_scope.get("vent")
    if current is None:
        return None
    reply = (
        "Listen first and acknowledge what the user is dealing with. Stay with listening "
        "on this turn. Wait for a direct request before offering solutions or brainstorming."
        if current.mode == MODE_WITNESS else
        "Acknowledge what the user is dealing with and offer one practical idea if it fits. "
        "Build on their actual details. Leave the timing open unless the user gave a date."
    )
    return SupportContext(
        current.mode,
        f"Response preference for this kind of vent (message {current.message_id}). {reply} "
        "Bring up the earlier conversation only if the user asks. Follow any direct request in "
        "the current message.",
    )
