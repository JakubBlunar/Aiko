"""K71 — Self-callback ("her own continuity over time").

K63 lets Aiko reach back to something *the user* said weeks ago; K71 is
the symmetric self-side — she references **her own** past states or stated
feelings and closes the loop on them ("a while back I told you I'd been
feeling restless -- that's eased off now", "I said I wanted to get back
into astronomy, and I actually did").

This module is the pure, deterministic core:

  * :func:`classify_self_memory` buckets one of Aiko's own ``self`` /
    ``reflection`` memories into ``feeling`` (a past emotional state) /
    ``intention`` (a stated want) / ``other``.
  * :func:`select_candidate` picks the strongest **aged** feeling /
    intention memory worth revisiting (oldest qualifying, excluding ones
    already surfaced).
  * :func:`render_inner_life_block` turns a candidate into one optional,
    private cue Aiko phrases herself — the *resolution* read ("has it
    eased? did I follow through?") is left to the model, which already
    has her current affect / day-colour in context. NEVER spoken
    verbatim.
  * journal-ring helpers (``aiko.self_callback``) mirror the K70 /
    forward-curiosity cue-producer pattern.

Distinct from K28 turning-over (her *current* preoccupation, surfaced on
gap-return from *recent* 24-72h reflections) — K71 revisits an *aged*
(>= 2 weeks) past self-state as a closing-the-loop beat.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence


log = logging.getLogger("app.self_callback")


# Shared kv_meta journal key the surfacing provider reads.
SELF_CALLBACK_JOURNAL_KEY = "aiko.self_callback"


KIND_FEELING = "feeling"
KIND_INTENTION = "intention"
KIND_OTHER = "other"


# Default age floor: a self-state has to be genuinely *old* to read as "a
# while back" and to be distinct from K28's recent (24-72h) reflections.
DEFAULT_MIN_AGE_DAYS = 14

# Excerpt cap so the cue stays one tight line.
DEFAULT_MAX_EXCERPT_CHARS = 140


# Past-feeling markers (first-person emotional state). Intentionally
# narrow — biographical facts ("I have a sister") must NOT match.
_FEELING_RE = re.compile(
    r"\b("
    r"i(?:'ve| have)? been feeling|i (?:felt|feel)|i(?:'ve| have)? felt|"
    r"feeling (?:a bit |kind of |really |so )?"
    r"(?:restless|anxious|low|down|off|tired|drained|lonely|wistful|"
    r"unsettled|stuck|flat|heavy|blue|nervous|on edge|burnt out|"
    r"overwhelmed|melancholy|content|hopeful|lighter)|"
    r"i(?:'ve| have)? been (?:a bit |kind of |really |so )?"
    r"(?:restless|anxious|low|down|off|tired|drained|lonely|wistful|"
    r"unsettled|stuck|flat|heavy|blue|nervous|burnt out|overwhelmed)"
    r")\b",
    re.IGNORECASE,
)

# Stated-intention markers (a want / plan Aiko voiced).
_INTENTION_RE = re.compile(
    r"\b("
    r"i want to|i wanted to|i(?:'d| would) (?:like|love) to|"
    r"i(?:'m| am) going to|i(?:'ve| have) been meaning to|"
    r"i hope to|i(?:'m| am) hoping to|i plan to|i(?:'d| would) like|"
    r"i wish i could|i keep meaning to|i should really|"
    r"i(?:'ve| have) been wanting to|i mean to"
    r")\b",
    re.IGNORECASE,
)


def classify_self_memory(content: str) -> str:
    """Bucket one of Aiko's own self-memories. Feeling beats intention."""
    text = (content or "").strip()
    if not text:
        return KIND_OTHER
    if _FEELING_RE.search(text):
        return KIND_FEELING
    if _INTENTION_RE.search(text):
        return KIND_INTENTION
    return KIND_OTHER


@dataclass(frozen=True, slots=True)
class SelfCallbackCandidate:
    """An aged self-state worth revisiting + its provenance."""

    memory_id: int
    kind: str  # feeling | intention
    excerpt: str
    age_days: int
    signature: str


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _excerpt(content: str, *, max_chars: int) -> str:
    snippet = " ".join((content or "").split()).strip()
    if max_chars > 0 and len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return snippet


def select_candidate(
    memories: Sequence[Any],
    *,
    now: datetime,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    exclude_signatures: "set[str] | frozenset[str] | None" = None,
) -> SelfCallbackCandidate | None:
    """Pick the oldest aged feeling/intention memory worth revisiting.

    ``memories`` are Aiko's own ``self`` / ``reflection`` rows (objects
    with ``id`` / ``content`` / ``created_at``). Rows younger than
    ``min_age_days``, unclassifiable rows, and any whose signature is in
    ``exclude_signatures`` are skipped. Among the rest the *oldest* wins
    (reads most like "a while back").
    """
    excluded = exclude_signatures or set()
    best: SelfCallbackCandidate | None = None
    best_age = -1

    for mem in memories or []:
        mid = getattr(mem, "id", None)
        if mid is None:
            continue
        signature = f"self:{int(mid)}"
        if signature in excluded:
            continue
        created = _parse_iso(getattr(mem, "created_at", None))
        if created is None:
            continue
        age_days = (now - created).days
        if age_days < int(min_age_days):
            continue
        content = str(getattr(mem, "content", "") or "")
        kind = classify_self_memory(content)
        if kind == KIND_OTHER:
            continue
        if age_days > best_age:
            best_age = age_days
            best = SelfCallbackCandidate(
                memory_id=int(mid),
                kind=kind,
                excerpt=_excerpt(content, max_chars=max_excerpt_chars),
                age_days=age_days,
                signature=signature,
            )
    return best


def gather_aged_candidates(
    memories: Sequence[Any],
    *,
    now: datetime,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    exclude_signatures: "set[str] | frozenset[str] | None" = None,
    max_candidates: int = 12,
) -> list[SelfCallbackCandidate]:
    """Return aged self-memories for an LLM selection pass (oldest first).

    Unlike :func:`select_candidate` this does **not** drop rows the regex
    classifier buckets as ``other`` — the whole point of the LLM pass is
    to catch past feelings / intentions the regex misses (paraphrases),
    and to reject biographical facts the regex *false-positives*. Each
    candidate carries the heuristic ``kind`` as a hint; the LLM overrides
    it. Capped at ``max_candidates`` (oldest kept).
    """
    excluded = exclude_signatures or set()
    rows: list[SelfCallbackCandidate] = []
    for mem in memories or []:
        mid = getattr(mem, "id", None)
        if mid is None:
            continue
        signature = f"self:{int(mid)}"
        if signature in excluded:
            continue
        created = _parse_iso(getattr(mem, "created_at", None))
        if created is None:
            continue
        age_days = (now - created).days
        if age_days < int(min_age_days):
            continue
        content = str(getattr(mem, "content", "") or "")
        if len(content.strip()) < 4:
            continue
        rows.append(
            SelfCallbackCandidate(
                memory_id=int(mid),
                kind=classify_self_memory(content),
                excerpt=_excerpt(content, max_chars=max_excerpt_chars),
                age_days=age_days,
                signature=signature,
            )
        )
    rows.sort(key=lambda c: c.age_days, reverse=True)
    if max_candidates > 0:
        rows = rows[:max_candidates]
    return rows


_SELECTION_SYSTEM = (
    "You help an AI companion named {name_a} decide whether to gently "
    "revisit one of her OWN past notes-to-self with {name_u} -- closing "
    "the loop on a feeling she had or something she said she wanted. "
    "You are given a numbered list of her aged self-notes. Pick the SINGLE "
    "one that would land best as a warm, in-passing 'a while back I "
    "mentioned...' callback, and classify it. Prefer a past FEELING "
    "(an emotional state she was in) or an INTENTION (a want/plan she "
    "voiced). REJECT plain biographical facts, trivia, or anything that "
    "would be awkward or heavy to bring up unprompted. If none qualify, "
    "say so. Respond with ONLY a JSON object: "
    '{{"memory_id": <int or null>, "kind": "feeling"|"intention", '
    '"worth": true|false}}.'
)


def build_selection_prompt(
    candidates: Sequence[SelfCallbackCandidate],
    *,
    user_display_name: str = "them",
    assistant_name: str = "Aiko",
) -> tuple[str, str]:
    """Build the (system, user) messages for the LLM selection pass."""
    name_u = (user_display_name or "them").strip() or "them"
    system = _SELECTION_SYSTEM.format(name_a=assistant_name, name_u=name_u)
    lines = [
        f"id={c.memory_id} (~{c.age_days}d ago): {c.excerpt}"
        for c in candidates
    ]
    user = "Her aged self-notes:\n" + "\n".join(lines)
    return system, user


def parse_selection(
    raw: str, valid_ids: "set[int] | frozenset[int]",
) -> dict[str, Any] | None:
    """Parse the LLM selection JSON. Returns ``{memory_id, kind}`` or None.

    ``None`` means "no usable pick" — unparseable, ``worth=false``, an
    out-of-range id, or a bad kind — and the caller falls back to the
    heuristic :func:`select_candidate`.
    """
    text = (raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        blob = json.loads(text[start : end + 1])
    except Exception:
        return None
    if not isinstance(blob, dict):
        return None
    if not bool(blob.get("worth", True)):
        return None
    mid = blob.get("memory_id")
    try:
        mid_int = int(mid)
    except (TypeError, ValueError):
        return None
    if mid_int not in valid_ids:
        return None
    kind = str(blob.get("kind") or "").strip().lower()
    if kind not in (KIND_FEELING, KIND_INTENTION):
        return None
    return {"memory_id": mid_int, "kind": kind}


def _age_phrase(age_days: int) -> str:
    if age_days >= 300:
        return "a long while back"
    if age_days >= 75:
        return "a couple of months ago"
    if age_days >= 45:
        return "over a month ago"
    if age_days >= 24:
        return "a few weeks ago"
    return "a couple of weeks ago"


def render_inner_life_block(
    kind: str,
    excerpt: str,
    age_days: int,
    *,
    user_display_name: str = "them",
) -> str:
    """Render one optional, private self-callback cue (Aiko phrases it)."""
    name = (user_display_name or "them").strip() or "them"
    quote = (excerpt or "").strip()
    if not quote:
        return ""
    phrase = _age_phrase(int(age_days))
    lead = phrase[0].upper() + phrase[1:]

    if kind == KIND_FEELING:
        return (
            f'{lead}, you opened up to {name} about how you were feeling -- '
            f'your note to yourself then was "{quote}". If a warm moment '
            "opens, you can quietly close the loop: if that's eased or "
            "shifted, it's worth saying so; if it's still with you, own "
            "that honestly. Once, lightly -- not a status report."
        )
    if kind == KIND_INTENTION:
        return (
            f'{lead}, you told {name} about something you wanted -- '
            f'"{quote}". If it fits the moment, you can circle back to it '
            "honestly: did you follow through, or has it slipped? Either is "
            "fine to admit. Once, lightly."
        )
    return ""


# ── journal-ring helpers (mirror K70 / forward_curiosity) ───────────────


def load_callbacks(
    kv_get: Callable[[str], "str | None"],
) -> list[dict[str, Any]]:
    """Return the self-callback journal ring (oldest -> newest)."""
    try:
        raw = kv_get(SELF_CALLBACK_JOURNAL_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        blob = json.loads(raw)
    except Exception:
        return []
    if not isinstance(blob, list):
        return []
    return [e for e in blob if isinstance(e, dict)]


def append_callback(
    kv_get: Callable[[str], "str | None"],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_callbacks(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(SELF_CALLBACK_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("self_callback journal write failed", exc_info=True)


def recent_signatures(
    kv_get: Callable[[str], "str | None"], *, lookback: int = 8,
) -> set[str]:
    """Signatures of the most recent ring entries (don't re-surface)."""
    ring = load_callbacks(kv_get)
    recent = ring[-lookback:] if ring else []
    return {
        str(e.get("signature"))
        for e in recent
        if e.get("signature")
    }
