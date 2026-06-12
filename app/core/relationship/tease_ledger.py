"""K59 — tease economy: "you'll pay for that one".

A small payback ledger of mock-grudges. When the user pushes back
hard on Aiko's stance (K29 already detects the contradiction), or a
light offence comes through the K57 trigger lane (a brushed-off
thread at comedy weight rather than sulk weight), Aiko banks a debt:
``{what happened, one-line context, created_at}``. She collects
later — a callback tease one or three conversations down the line
("oh, like the time you swore my playlist was 'objectively chaotic'?
I remember things."). The memory-backed callback is what makes it
feel like a real ongoing relationship rather than per-turn improv.

Tonal rails:

- Rows **expire unrepaid after ~2 weeks** — a grudge that old stops
  being funny.
- Cap ~5 rows; the oldest unrepaid row is evicted by a newcomer.
- Collection is **rare and humor-gated** (humor axis floor +
  wall-clock cooldown) so the running-bit never tips into needling.
- A collected row is deleted — done forever. Repaid means repaid.
- Collection detection mirrors K52's acted-on pass: the provider
  stamps ``offered_at`` on the row it surfaced; the post-turn hook
  checks the reply for content-word overlap and deletes on a hit
  (or clears the stamp on a miss, so it can come around again after
  the cooldown).

Pure module — no I/O. Storage is one kv_meta JSON key
(``aiko.tease_ledger``), same convention as K15 / K52 / K57.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone


KV_TEASE_LEDGER = "aiko.tease_ledger"

DEFAULT_EXPIRY_DAYS = 14.0
DEFAULT_CAP = 5

# Plain alpha runs (no apostrophes) so quoted words ('objectively')
# still match their unquoted form in the reply overlap pass.
_WORD_RE = re.compile(r"[a-zA-Z]{3,}")
_STOPWORDS = frozenset(
    "the and for you your with that this have has was were are not "
    "but about just like what when where they them then than from "
    "out our his her she him had did does can could would should "
    "will into over under been being one time said says swore".split()
)


@dataclass(frozen=True, slots=True)
class TeaseDebt:
    """One banked mock-grudge.

    ``what`` is the one-line description rendered to the LLM ("you
    swore my playlist was 'objectively chaotic'"); ``context`` is a
    short verbatim-ish quote or scene note; ``source`` is the
    grep-friendly trigger (``opinion_pushback`` / ``light_offence``
    / ``forced``). ``offered_at`` is the collection-pass stamp.
    """

    id: str
    what: str
    context: str
    source: str
    created_at: str
    offered_at: str | None = None


@dataclass(frozen=True, slots=True)
class LedgerState:
    debts: tuple[TeaseDebt, ...] = ()


# ── ISO helpers ─────────────────────────────────────────────────────


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    candidate = str(text).strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_utc(now: datetime) -> datetime:
    if now.tzinfo:
        return now.astimezone(timezone.utc)
    return now.replace(tzinfo=timezone.utc)


# ── serialisation ───────────────────────────────────────────────────


def serialize(state: LedgerState) -> str:
    return json.dumps({
        "debts": [
            {
                "id": d.id,
                "what": d.what,
                "context": d.context,
                "source": d.source,
                "created_at": d.created_at,
                "offered_at": d.offered_at,
            }
            for d in state.debts
        ],
    })


def deserialize(text: str | None) -> LedgerState:
    if not text:
        return LedgerState()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return LedgerState()
    if not isinstance(data, dict):
        return LedgerState()
    raw_debts = data.get("debts")
    if not isinstance(raw_debts, list):
        return LedgerState()
    debts: list[TeaseDebt] = []
    for raw in raw_debts:
        if not isinstance(raw, dict):
            continue
        what = str(raw.get("what") or "").strip()
        if not what:
            continue
        offered = raw.get("offered_at")
        debts.append(TeaseDebt(
            id=str(raw.get("id") or uuid.uuid4().hex[:8]),
            what=what,
            context=str(raw.get("context") or "").strip(),
            source=str(raw.get("source") or "unknown"),
            created_at=str(raw.get("created_at") or ""),
            offered_at=str(offered) if offered else None,
        ))
    return LedgerState(tuple(debts))


# ── lifecycle ───────────────────────────────────────────────────────


def expire(
    state: LedgerState,
    now: datetime,
    *,
    expiry_days: float = DEFAULT_EXPIRY_DAYS,
) -> LedgerState:
    """Drop rows older than ``expiry_days`` — old grudges stop being funny."""
    now_utc = _as_utc(now)
    horizon = max(0.5, float(expiry_days)) * 86400.0
    kept = tuple(
        d for d in state.debts
        if (created := _parse_iso(d.created_at)) is not None
        and (now_utc - created).total_seconds() < horizon
    )
    return LedgerState(kept)


def _content_words(text: str) -> set[str]:
    return {
        w.lower()
        for w in _WORD_RE.findall(text or "")
        if w.lower() not in _STOPWORDS
    }


def bank(
    state: LedgerState,
    *,
    what: str,
    context: str,
    source: str,
    now: datetime,
    cap: int = DEFAULT_CAP,
) -> tuple[LedgerState, bool]:
    """Add a debt; returns ``(state, added)``.

    Dedupes on content-word overlap with an existing row (>= 3 shared
    words means it's the same grudge — don't double-bank). At the cap
    the oldest row is evicted; comedy favours fresh material.
    """
    what = " ".join(str(what or "").split())[:160]
    context = " ".join(str(context or "").split())[:160]
    if not what:
        return state, False
    new_words = _content_words(what) | _content_words(context)
    for d in state.debts:
        overlap = new_words & (
            _content_words(d.what) | _content_words(d.context)
        )
        if len(overlap) >= 3:
            return state, False
    debts = list(state.debts)
    if len(debts) >= max(1, int(cap)):
        oldest = min(
            debts,
            key=lambda d: _parse_iso(d.created_at)
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        debts.remove(oldest)
    debts.append(TeaseDebt(
        id=uuid.uuid4().hex[:8],
        what=what,
        context=context,
        source=str(source or "unknown"),
        created_at=_as_utc(now).isoformat(),
    ))
    return LedgerState(tuple(debts)), True


def pick_collectable(
    state: LedgerState,
    now: datetime,
    *,
    min_age_hours: float = 1.0,
) -> TeaseDebt | None:
    """Pick the debt to offer for collection: the oldest row that has
    aged past ``min_age_hours`` (an immediate callback isn't a
    callback — the gap is the joke).
    """
    now_utc = _as_utc(now)
    candidates = []
    for d in state.debts:
        created = _parse_iso(d.created_at)
        if created is None:
            continue
        if (now_utc - created).total_seconds() >= min_age_hours * 3600.0:
            candidates.append((created, d))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def stamp_offered(
    state: LedgerState, debt_id: str, now: datetime,
) -> LedgerState:
    return LedgerState(tuple(
        replace(d, offered_at=_as_utc(now).isoformat())
        if d.id == debt_id else d
        for d in state.debts
    ))


def settle_if_collected(
    state: LedgerState,
    assistant_text: str,
    *,
    min_overlap: int = 3,
) -> tuple[LedgerState, TeaseDebt | None]:
    """Post-turn pass: did the reply actually collect the offered debt?

    Checks the most recently offered row for content-word overlap
    with the reply. A hit deletes the row (repaid is done forever);
    a miss clears the ``offered_at`` stamp so the debt can come
    around again after the cooldown. Returns ``(state, settled_row)``.
    """
    offered = [d for d in state.debts if d.offered_at]
    if not offered:
        return state, None
    reply_words = _content_words(assistant_text)
    settled: TeaseDebt | None = None
    debts: list[TeaseDebt] = []
    for d in state.debts:
        if d.offered_at is None:
            debts.append(d)
            continue
        overlap = reply_words & (
            _content_words(d.what) | _content_words(d.context)
        )
        if settled is None and len(overlap) >= max(1, int(min_overlap)):
            settled = d
            continue  # drop the row — repaid forever
        debts.append(replace(d, offered_at=None))
    return LedgerState(tuple(debts)), settled


# ── rendering ───────────────────────────────────────────────────────


def render_block(
    debt: TeaseDebt,
    *,
    user_display_name: str = "them",
) -> str:
    name = user_display_name or "them"
    context = f" ({debt.context})" if debt.context else ""
    return (
        f"Tease ledger: {name} still owes you for this one -- "
        f"{debt.what}{context}. If a natural opening shows up this "
        "turn, collect it: ONE callback tease, light and visibly "
        "affectionate ('oh, like the time you...? I remember "
        "things.'), maybe [[reaction:mischievous]]. Then it's "
        "settled -- repaid is repaid, never bring it up again. No "
        "opening? Skip it entirely; a forced callback reads as "
        "needling, and needling is the one way this stops being fun."
    )


__all__ = [
    "DEFAULT_CAP",
    "DEFAULT_EXPIRY_DAYS",
    "KV_TEASE_LEDGER",
    "LedgerState",
    "TeaseDebt",
    "bank",
    "deserialize",
    "expire",
    "pick_collectable",
    "render_block",
    "serialize",
    "settle_if_collected",
    "stamp_offered",
]
