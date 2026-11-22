"""Resolve a bounded retrospective question against retrieved report receipts."""

from dataclasses import dataclass
import re

from app.core.infra import timephrase
from app.core.memory.learned_strategies import words
from app.core.memory.memory_origin import memory_origin
from app.core.memory.memory_relations import incoming_relations
from app.core.memory.observation_receipts import longitudinal_sources_current
from app.core.memory.reflective_evidence import eligible


_RECALL = re.compile(
    r"^(?:what happened (?:when|with|after)|what did (?:i|we) (?:do|try|change)\b"
    r"|did (?:that|it|this|my\b[^?]{0,55}|the\b[^?]{0,55}) (?:fix|help|solve|work)\b)",
    re.I,
)
_OTHER_REQUEST = re.compile(r"\b(?:can you|could you|please|would you|tell me)\b", re.I)
_FOLLOW_UP = re.compile(r"^(?:i mean(?:t)?(?:\s|,)|(?:the|my|our) )", re.I)
_STOP = frozenset(
    "what happened when after where did do fix help solve work worked try change changed "
    "when where there think thinking remember also again just mean meant happen still time".split()
)
_CONTEXT_LIMIT = 480
MATCHED_REPLY_GUIDANCE = (
    "The current question has a verified matching episode in the original user reports. "
    "Answer now in one or two sentences. State what the user did and the latest result, "
    "including what still happens. End after the reported facts."
)


@dataclass(frozen=True)
class Episode:
    memory_id: int
    message_ids: tuple[int, ...]
    text: str


def _terms(text):
    return words(text) - _STOP


def requested_episode(user_text, recent_turns):
    """Recognize a past-action question or the immediate answer to its clarification."""
    text = user_text.strip()
    recent = recent_turns or ()
    if _OTHER_REQUEST.search(text):
        return ""
    if _RECALL.match(text):
        if len(_terms(text)) >= 2:
            return text
        return next((turn for turn in reversed(recent) if len(_terms(turn)) >= 2), "")
    if (len(recent) >= 2 and recent[-1].strip() == "Which one do you mean?"
            and _RECALL.match(recent[-2].strip()) and len(text) <= 100
            and _FOLLOW_UP.match(text) and "?" not in text):
        return text
    return ""


def _owned(session, owner):
    return bool(owner and session and session.startswith(f"{owner}:")
                and not session.endswith(":noremember"))


def _packet(memory, observation, store, now, owner, *, with_age):
    sources = observation["sources"]
    action, outcome = observation.get("action"), observation.get("outcome")
    exceptions = observation.get("exceptions", [])
    receipts = [observation["context"], action, outcome, *exceptions]
    if any(not isinstance(row, dict) or row not in sources or row.get("role") != "user"
           or row.get("session_id") != memory.source_session for row in receipts):
        return None
    relations, truncated = incoming_relations(memory, store)
    if truncated:
        return None
    for row in relations:
        relation = row["receipt"]
        if relation["relation"] != "correction":
            continue
        report = relation["report"]
        if not row["current"] or report.get("role") != "user" or not _owned(
            report.get("session_id"), owner,
        ):
            return None
        outcome = report
        break
    receipts = [observation["context"], action, outcome, *exceptions]
    labels = ["Problem", "Action taken", "Latest reported result"]
    labels.extend(["Exception"] * len(exceptions))
    transcript = timephrase.format_transcript([
        {"role": "user", "content": f"{label}: {row['quote']}", "created_at": row["source_at"]}
        for label, row in zip(labels, receipts, strict=True)
    ], now, with_age=with_age)
    if len(transcript) > _CONTEXT_LIMIT:
        return None
    ids = tuple(sorted({row["message_id"] for row in [*sources, outcome]}))
    return Episode(memory.id, ids, transcript)


def select_episode(question, candidates, store, now, *, owner, with_age=True):
    """Prefer a unique literal match, with no assertion for a tie or stale source."""
    query_words = _terms(question)
    matches = []
    for memory in candidates:
        observation = (memory.metadata or {}).get("observation")
        origin = memory_origin(memory)
        if (not isinstance(observation, dict) or observation.get("subject") != "user"
                or origin.subject != "user" or origin.mode != "observation"
                or origin.domain != "conversation" or not _owned(memory.source_session, owner)):
            continue
        context = observation.get("context") or {}
        if context.get("context_only") or context.get("role") != "user":
            continue
        overlap = len(query_words.intersection(_terms(context.get("quote", ""))))
        if overlap >= 2:
            matches.append((overlap, memory, observation))
    if not matches:
        return None, "no_match"
    best = max(item[0] for item in matches)
    episodes = {}
    for overlap, memory, observation in matches:
        if overlap != best:
            continue
        if not eligible(memory) or not longitudinal_sources_current(memory, store):
            return None, "source_unavailable"
        packet = _packet(memory, observation, store, now, owner, with_age=with_age)
        if packet is None:
            return None, "source_unavailable"
        episodes.setdefault(packet.message_ids, packet)
    if len(episodes) > 1:
        return None, "ambiguous"
    return next(iter(episodes.values())), "matched"


@dataclass(frozen=True)
class EpisodeContext:
    text: str = ""
    status: str = "no_match"
    episode: Episode | None = None
    reply_guidance: str = ""


def context_from_hits(query, hits, store, *, owner, session_key):
    if not query or store is None or not _owned(session_key, owner):
        return EpisodeContext()
    ids = [int(hit.record.id) for hit in hits
           if hit.source == "memory" and str(hit.record.id).isdecimal()]
    # These questions ask about the reported action and outcome. The message age
    # can be mistaken for the date of the event, which may not have been stated.
    episode, status = select_episode(
        query, store.get_many(ids).values(), store,
        timephrase.utcnow(), owner=owner, with_age=False,
    )
    if episode is not None:
        return EpisodeContext("Original user reports for this episode:\n" + episode.text,
                              status, episode, MATCHED_REPLY_GUIDANCE)
    elif status == "ambiguous":
        text = ("Several recorded episodes fit the current reference. Which one the user "
                "means is unresolved. Reply only with [[clarify:reference]].")
    elif status == "source_unavailable":
        text = ("The matching episode lacks current source evidence. Leave the earlier action "
                "and result unknown.")
    else:
        text = ""
    return EpisodeContext(text, status)
