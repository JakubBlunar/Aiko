"""K63 — long-arc callbacks ("weeks ago you said...").

K22 catches inside-jokes and short-horizon callbacks; K63 is the *long*
reach — the rare beat where Aiko connects the live turn to something the
user told her **weeks or months** ago ("wait, didn't you once mention your
dad back in May?"). That long reach is one of the strongest "she actually
knows me" signals a companion can produce, so the whole design is built
around **rarity**: a high topical bar, a hard age floor, a wall-clock
cooldown, a per-session cap, and a don't-repeat ring. Over-firing turns
"she remembers" into "she's combing a database".

This module is the pure, dependency-free core:

  * :class:`AgedCandidate` — one old, topically-linked memory the retriever's
    aged lane (:meth:`RagRetriever.aged_callback_candidate`) surfaced.
  * :func:`select` — pick the single best callback candidate (strongest
    topical match, oldest on a tie), skipping anything recently used.
  * :func:`render_block` — the tentative "reach back" cue, leaning on K25's
    hedging posture ("float it as a question, the details may have faded").
  * kv helpers — wall-clock cooldown + the recently-surfaced-id ring, both
    persisted on ``kv_meta`` so the rarity survives a restart / session
    switch.

The consumer is
:meth:`InnerLifeProvidersMixin._render_long_arc_callback_block`, which never
speaks or fires a proactive nudge — the cue is a private prompt hint the
chat model phrases itself, in Aiko's own words.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from app.core.infra import timephrase as _tp

log = logging.getLogger("app.long_arc_callback")


# ── tuning defaults (mirrored by AgentSettings / MemorySettings) ────────
# An eligible callback memory must be at least this old. Three weeks keeps
# this firmly in "long arc" territory — K22's recency-aware callback bonus
# already covers anything fresher.
DEFAULT_MIN_AGE_DAYS = 21
# Topical bar: cosine of the live turn against the old memory. Higher than
# the normal RAG ``score_threshold`` (0.4) so a callback is a genuine link,
# not a loose association.
DEFAULT_MIN_COSINE = 0.55
# Wall-clock cooldown between callbacks (hours). Long on purpose.
DEFAULT_COOLDOWN_HOURS = 6.0
# At most this many callbacks per session, regardless of the cooldown.
DEFAULT_PER_SESSION_CAP = 1
# Skip short turns — a one-word reply rarely carries a callback-worthy topic
# and embedding it just burns a search for nothing.
DEFAULT_MIN_USER_WORDS = 5
# How many recently-surfaced callback ids to remember (don't-repeat ring).
RECENT_IDS_MAX = 30
# How many candidates the aged lane fetches before we pick one.
CANDIDATE_TOP_K = 24
# Snippet length for the rendered cue.
SNIPPET_MAXLEN = 180
# Only memory kinds that represent things the *user* told Aiko qualify;
# her own self-stances / distilled knowledge / housekeeping rows do not.
ALLOWED_KINDS: frozenset[str] = frozenset(
    {"fact", "preference", "event", "relationship", "shared_moment"}
)

# kv_meta keys owned by K63.
KV_LAST_FIRED_AT = "aiko.long_arc_callback.last_fired_at"
KV_RECENT_IDS = "aiko.long_arc_callback.recent_ids"


@dataclass(slots=True, frozen=True)
class AgedCandidate:
    """One old, topically-linked memory eligible for a long-arc callback."""

    memory_id: int
    content: str
    kind: str
    created_at: str
    cosine: float
    age_days: float


def select(
    candidates: Iterable[AgedCandidate],
    *,
    exclude_ids: Iterable[int] | None = None,
) -> AgedCandidate | None:
    """Pick the single best callback candidate, or ``None``.

    Strongest topical match wins (highest cosine); ties break toward the
    *oldest* memory (largest ``age_days``) because the longer reach is the
    more impressive "she remembers" beat. Anything whose id is in
    ``exclude_ids`` (the recently-surfaced ring) is skipped so the same
    callback can't land twice in a row.
    """
    skip = {int(i) for i in (exclude_ids or [])}
    best: AgedCandidate | None = None
    for cand in candidates:
        try:
            mid = int(cand.memory_id)
        except (TypeError, ValueError):
            continue
        if mid in skip:
            continue
        if not (cand.content or "").strip():
            continue
        if best is None:
            best = cand
            continue
        if cand.cosine > best.cosine or (
            cand.cosine == best.cosine and cand.age_days > best.age_days
        ):
            best = cand
    return best


def _snippet(text: str) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > SNIPPET_MAXLEN:
        return flat[: SNIPPET_MAXLEN - 1].rstrip() + "\u2026"
    return flat


def _month_anchor(created_at: str, now: datetime, *, age_days: float) -> str:
    """A ", back in May" / ", back in May 2025" anchor for older memories.

    Only added once a memory is at least ~6 weeks old (below that the
    relative phrase like "3 weeks ago" already reads naturally). Returns
    an empty string when the date can't be parsed.
    """
    if age_days < 42:
        return ""
    when = _tp.parse_iso(created_at)
    if when is None:
        return ""
    when_local = when.astimezone()
    now_local = _tp.to_aware(now).astimezone()
    month = when_local.strftime("%B")
    if when_local.year != now_local.year:
        return f", back in {month} {when_local.year}"
    return f", back in {month}"


def render_block(
    candidate: AgedCandidate,
    *,
    user_display_name: str = "the user",
    now: datetime | None = None,
) -> str:
    """Render the tentative "reach back" cue for ``candidate``.

    Phrased as private guidance, never a script — the chat model decides
    whether and how to use it. The tentativeness is the point: an old
    memory's details may have faded (K25), so Aiko floats it as a question
    rather than asserting it as fact.
    """
    when = now or datetime.now(timezone.utc)
    name = (user_display_name or "the user").strip() or "the user"
    snippet = _snippet(candidate.content)
    if not snippet:
        return ""
    rel = _tp.humanize_past(candidate.created_at, when)
    anchor = _month_anchor(candidate.created_at, when, age_days=candidate.age_days)
    return (
        f"Heads-up: what {name} just said connects to something he told you a "
        f"long while back \u2014 \"{snippet}\" (about {rel}{anchor}). If it "
        "fits naturally, you could reach back to it *tentatively* \u2014 "
        "\"wait, didn't you once mention\u2026?\" \u2014 rather than stating "
        "it as fact. It's an old memory and the details may have faded, so "
        "float it as a question and let him fill in the gaps; drop it gently "
        "if it doesn't land. Don't force the callback \u2014 this only works "
        "when it's genuinely the same thread."
    )


# ── kv helpers (cooldown + don't-repeat ring) ───────────────────────────


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


def cooldown_elapsed(
    kv_get: Callable[[str], str | None],
    *,
    now: datetime,
    cooldown_hours: float,
) -> bool:
    """True when the wall-clock cooldown since the last fire has elapsed."""
    if cooldown_hours <= 0:
        return True
    try:
        raw = kv_get(KV_LAST_FIRED_AT)
    except Exception:
        return True
    last = _parse_iso(raw)
    if last is None:
        return True
    return (now - last).total_seconds() / 3600.0 >= float(cooldown_hours)


def mark_fired(
    kv_set: Callable[[str, str], None],
    *,
    now: datetime,
) -> None:
    """Stamp the wall-clock cooldown."""
    try:
        kv_set(KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))
    except Exception:
        log.debug("long_arc_callback mark_fired write failed", exc_info=True)


def load_recent_ids(kv_get: Callable[[str], str | None]) -> list[int]:
    """Return the don't-repeat ring of recently-surfaced callback ids."""
    try:
        raw = kv_get(KV_RECENT_IDS)
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
    out: list[int] = []
    for item in blob:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def append_recent_id(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    memory_id: int,
    *,
    max_entries: int = RECENT_IDS_MAX,
) -> None:
    """Append ``memory_id`` to the don't-repeat ring, trimming to cap."""
    ring = [i for i in load_recent_ids(kv_get) if i != int(memory_id)]
    ring.append(int(memory_id))
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(KV_RECENT_IDS, json.dumps(ring))
    except Exception:
        log.debug("long_arc_callback recent-ids write failed", exc_info=True)


def candidates_from_hits(
    hits: Sequence[Any],
    *,
    now: datetime,
    min_age_days: int,
    allowed_kinds: Iterable[str] | None = None,
) -> list[AgedCandidate]:
    """Build :class:`AgedCandidate` rows from raw memory RAG hits.

    Pure projection used by :meth:`RagRetriever.aged_callback_candidate`:
    filters each hit by age floor + allowed kind, computes ``age_days``,
    and carries the cosine through from the hit score. Defensive — a hit
    with an unparseable id / date / score is skipped, never raised.
    """
    kinds = (
        {str(k).strip().lower() for k in allowed_kinds}
        if allowed_kinds is not None
        else None
    )
    out: list[AgedCandidate] = []
    for hit in hits:
        record = getattr(hit, "record", None)
        if record is None:
            continue
        kind = str(getattr(record, "kind", "") or "").strip().lower()
        if kinds is not None and kind not in kinds:
            continue
        raw_id = getattr(record, "id", None)
        try:
            mem_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        created_at = str(getattr(record, "created_at", "") or "")
        created = _parse_iso(created_at)
        if created is None:
            continue
        age_days = (now - created).total_seconds() / 86400.0
        if age_days < float(min_age_days):
            continue
        content = str(getattr(record, "content", "") or "").strip()
        if not content:
            continue
        try:
            cosine = float(getattr(hit, "score", 0.0))
        except (TypeError, ValueError):
            cosine = 0.0
        out.append(
            AgedCandidate(
                memory_id=mem_id,
                content=content,
                kind=kind,
                created_at=created_at,
                cosine=cosine,
                age_days=age_days,
            )
        )
    return out


__all__ = [
    "AgedCandidate",
    "ALLOWED_KINDS",
    "CANDIDATE_TOP_K",
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_MIN_AGE_DAYS",
    "DEFAULT_MIN_COSINE",
    "DEFAULT_MIN_USER_WORDS",
    "DEFAULT_PER_SESSION_CAP",
    "KV_LAST_FIRED_AT",
    "KV_RECENT_IDS",
    "RECENT_IDS_MAX",
    "SNIPPET_MAXLEN",
    "append_recent_id",
    "candidates_from_hits",
    "cooldown_elapsed",
    "load_recent_ids",
    "mark_fired",
    "render_block",
    "select",
]
