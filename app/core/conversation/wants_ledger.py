"""Aiko's wants ledger — desire with pressure (K52 personality backlog).

A small store of things Aiko *wants from the conversation* — "ask
{user} about X", "tell {user} about Y", "steer toward goal Z" — fed
from producers that already exist (curiosity seeds, K34 forward-
curiosity questions, K1 goals). The new ingredient over those source
blocks is **pressure**: each want carries an intensity in ``[0, 1]``
that grows per wall-clock day until acted on. Below the imperative
threshold the cue renders as today's soft "spend one when a lull
lands" framing; above it the cue flips imperative — "this has been on
your mind for days: bring it up THIS conversation" — which is the
sentence no existing block ever says, and the piece that turns a
permission slip into actual will.

Design choices (mirrors K15 ``vulnerability_budget``):

- **Pure module, no I/O.** All lifecycle math is pure functions over
  a frozen :class:`LedgerState`; callers read/write the single
  ``kv_meta`` JSON key ``aiko.wants_ledger`` themselves.
- **Acting on a want visibly relieves it.** Post-turn detection runs
  a content-word overlap between the turn's text and each want (the
  same shape as revival detection in ``post_turn_mixin``); a hit
  removes the want and records its ``source_ref`` in a re-entry
  cooldown map so the hourly feeder doesn't immediately re-add it.
- **Stale wants decay to nothing.** A want never acted on expires
  after ``max_age_days`` — an itch that old has faded, and silently
  dropping it keeps the ledger from becoming a guilt list.
- **Capped ledger.** At the cap (default 8) new wants are refused
  rather than evicting old ones — expiry is the only exit besides
  acting, so pressure ordering stays honest.

The feeder worker lives in
:mod:`app.core.conversation.wants_ledger_worker`; the provider +
post-turn wiring live on the session mixins.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone


# ── kv_meta key ─────────────────────────────────────────────────────

# Namespaced under ``aiko.*`` alongside K15 / K27 state.
KV_WANTS_LEDGER = "aiko.wants_ledger"


# Want kinds — what acting on it looks like. ``ask`` = a question to
# the user, ``share`` = something Aiko wants to tell them, ``steer`` =
# nudging the conversation toward one of her goals.
WANT_KINDS = ("ask", "share", "steer")


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Want:
    """One thing Aiko wants from a conversation.

    ``text`` is the imperative body the prompt renders ("ask Jacob how
    the interview went"). ``source_ref`` is the dedup key tying the
    want back to its producer row (``seed:<id>`` / ``goal:<id>`` /
    ``fc:<at>`` / ``manual:<uuid>``); it also keys the re-entry
    cooldown after the want is acted on.
    """

    id: str
    text: str
    kind: str
    source: str
    source_ref: str
    created_at: str
    pressure: float
    last_growth_at: str


@dataclass(frozen=True, slots=True)
class LedgerState:
    """The persisted ledger: live wants + recently-acted cooldowns.

    ``recently_acted`` maps ``source_ref`` -> acted-at ISO timestamp.
    Treated as immutable by convention — every mutation goes through
    the pure functions below, which build a fresh dict.
    """

    wants: tuple[Want, ...] = ()
    recently_acted: tuple[tuple[str, str], ...] = ()

    def acted_map(self) -> dict[str, str]:
        return dict(self.recently_acted)


# ── ISO helpers ─────────────────────────────────────────────────────


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    candidate = str(text).strip()
    if not candidate:
        return None
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
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def age_days(want: Want, now: datetime) -> float:
    """Wall-clock age of a want in days (0.0 on parse failure)."""
    created = _parse_iso(want.created_at)
    if created is None:
        return 0.0
    return max(0.0, (_as_utc(now) - created).total_seconds() / 86400.0)


# ── Content-word overlap (acted-on detection) ───────────────────────

# Mirror of the revival-detection stopword posture in
# ``post_turn_mixin`` — kept local so the pure module has no session
# import. Tokens shorter than 4 chars are dropped by the tokenizer,
# so only longer common words need listing.
_STOPWORDS: frozenset[str] = frozenset({
    "that", "this", "these", "those", "with", "from", "about", "into",
    "than", "what", "when", "where", "which", "would", "could",
    "should", "will", "have", "been", "being", "does", "did", "your",
    "yours", "their", "them", "they", "really", "very", "much",
    "like", "just", "also", "some", "more", "most", "want", "wanting",
    "tell", "talk", "bring", "thing", "things",
})


def content_words(text: str) -> set[str]:
    """Lowercase content-word set for the overlap check."""
    if not text:
        return set()
    raw = re.findall(r"[A-Za-z][A-Za-z0-9'_-]+", str(text).lower())
    return {t for t in raw if len(t) >= 4 and t not in _STOPWORDS}


# ── Serialise / deserialise ─────────────────────────────────────────


def serialize(state: LedgerState) -> str:
    return json.dumps({
        "wants": [
            {
                "id": w.id,
                "text": w.text,
                "kind": w.kind,
                "source": w.source,
                "source_ref": w.source_ref,
                "created_at": w.created_at,
                "pressure": float(w.pressure),
                "last_growth_at": w.last_growth_at,
            }
            for w in state.wants
        ],
        "recently_acted": dict(state.recently_acted),
    })


def deserialize(text: str | None) -> LedgerState:
    """Parse a stored blob; corrupt input returns an empty ledger."""
    if not text:
        return LedgerState()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return LedgerState()
    if not isinstance(data, dict):
        return LedgerState()
    wants: list[Want] = []
    for raw in data.get("wants") or []:
        if not isinstance(raw, dict):
            continue
        body = str(raw.get("text") or "").strip()
        if not body:
            continue
        try:
            pressure = max(0.0, min(1.0, float(raw.get("pressure", 0.0))))
        except (TypeError, ValueError):
            pressure = 0.0
        wants.append(Want(
            id=str(raw.get("id") or uuid.uuid4().hex[:8]),
            text=body,
            kind=str(raw.get("kind") or "ask"),
            source=str(raw.get("source") or "manual"),
            source_ref=str(raw.get("source_ref") or ""),
            created_at=str(raw.get("created_at") or ""),
            pressure=pressure,
            last_growth_at=str(raw.get("last_growth_at") or ""),
        ))
    acted_raw = data.get("recently_acted")
    acted: list[tuple[str, str]] = []
    if isinstance(acted_raw, dict):
        acted = [(str(k), str(v)) for k, v in acted_raw.items()]
    return LedgerState(wants=tuple(wants), recently_acted=tuple(acted))


# ── Lifecycle math ──────────────────────────────────────────────────


def apply_growth(
    state: LedgerState,
    now: datetime,
    *,
    growth_per_day: float,
    max_age_days: float,
    reentry_cooldown_days: float,
) -> LedgerState:
    """Grow pressure on every live want; expire the stale.

    For each want: ``pressure += growth_per_day * elapsed_days`` since
    ``last_growth_at`` (clamped to 1.0), then drop wants older than
    ``max_age_days``. Re-entry cooldown entries older than
    ``reentry_cooldown_days`` are also swept so the feeder can re-add
    a topic after the cooldown.
    """
    now_utc = _as_utc(now)
    now_iso = now_utc.isoformat()
    kept: list[Want] = []
    for want in state.wants:
        if age_days(want, now_utc) >= max_age_days > 0:
            continue  # the itch faded
        grown = want.pressure
        if growth_per_day > 0:
            anchor = _parse_iso(want.last_growth_at) or _parse_iso(want.created_at)
            if anchor is not None:
                elapsed_days = max(
                    0.0, (now_utc - anchor).total_seconds() / 86400.0,
                )
                grown = min(1.0, want.pressure + growth_per_day * elapsed_days)
        kept.append(replace(want, pressure=grown, last_growth_at=now_iso))

    acted: list[tuple[str, str]] = []
    for ref, at in state.recently_acted:
        acted_at = _parse_iso(at)
        if acted_at is None:
            continue
        if (now_utc - acted_at).total_seconds() / 86400.0 < reentry_cooldown_days:
            acted.append((ref, at))
    return LedgerState(wants=tuple(kept), recently_acted=tuple(acted))


def add_want(
    state: LedgerState,
    *,
    text: str,
    kind: str,
    source: str,
    source_ref: str,
    now: datetime,
    cap: int = 8,
    initial_pressure: float = 0.15,
) -> tuple[LedgerState, bool]:
    """Add a want; returns ``(new_state, added)``.

    Refused (``added=False``) when: the body is empty, the ledger is
    at cap, the ``source_ref`` already exists (live or in re-entry
    cooldown), or the body's content words substantially overlap an
    existing want (>= 3 shared, or all of the shorter side's words).
    """
    body = (text or "").strip()
    if not body:
        return state, False
    if len(state.wants) >= max(1, cap):
        return state, False
    ref = (source_ref or "").strip()
    if ref:
        if any(w.source_ref == ref for w in state.wants):
            return state, False
        if ref in state.acted_map():
            return state, False
    new_words = content_words(body)
    for existing in state.wants:
        shared = new_words & content_words(existing.text)
        if not shared:
            continue
        smaller = min(len(new_words), len(content_words(existing.text)))
        if len(shared) >= 3 or (smaller > 0 and len(shared) >= smaller):
            return state, False
    if kind not in WANT_KINDS:
        kind = "ask"
    now_iso = _as_utc(now).isoformat()
    want = Want(
        id=uuid.uuid4().hex[:8],
        text=body,
        kind=kind,
        source=(source or "manual"),
        source_ref=ref or f"manual:{uuid.uuid4().hex[:8]}",
        created_at=now_iso,
        pressure=max(0.0, min(1.0, float(initial_pressure))),
        last_growth_at=now_iso,
    )
    return LedgerState(
        wants=state.wants + (want,),
        recently_acted=state.recently_acted,
    ), True


def mark_acted(state: LedgerState, want_id: str, now: datetime) -> LedgerState:
    """Remove a want and start its re-entry cooldown."""
    target = next((w for w in state.wants if w.id == want_id), None)
    if target is None:
        return state
    acted = state.acted_map()
    acted[target.source_ref] = _as_utc(now).isoformat()
    return LedgerState(
        wants=tuple(w for w in state.wants if w.id != want_id),
        recently_acted=tuple(acted.items()),
    )


def drop_source_refs(
    state: LedgerState, refs: set[str],
) -> tuple[LedgerState, list[str]]:
    """Remove wants whose ``source_ref`` is in ``refs``.

    Returns ``(new_state, dropped_ids)``. Unlike :func:`mark_acted`
    this does NOT record a re-entry cooldown: the want is pruned
    because its *producer is gone* (e.g. a curiosity seed was
    consumed/archived once its topic came up), not because Aiko acted
    on it — so there's nothing to cool down against, and the feeder
    won't re-offer a dead producer anyway. Without this, a want
    outlived its seed and kept growing pressure, driving Aiko to
    re-ask a question she'd already had answered.
    """
    if not refs or not state.wants:
        return state, []
    dropped = [w.id for w in state.wants if w.source_ref in refs]
    if not dropped:
        return state, []
    kept = tuple(w for w in state.wants if w.source_ref not in refs)
    return LedgerState(wants=kept, recently_acted=state.recently_acted), dropped


def detect_acted(
    state: LedgerState,
    turn_text: str,
    *,
    min_overlap: int = 3,
) -> list[str]:
    """Return ids of wants whose topic surfaced in ``turn_text``.

    ``turn_text`` should be the user message + Aiko's reply combined —
    a want is satisfied whether she raised it or the user happened to
    (once a topic has come up even briefly, it's done). The required
    overlap adapts to short want texts: ``max(2, min(min_overlap,
    len(want_words)))`` so "ask about the espresso machine" (3 content
    words) can match without needing more words than it has.
    """
    if not state.wants or not turn_text:
        return []
    turn_words = content_words(turn_text)
    if not turn_words:
        return []
    hits: list[str] = []
    for want in state.wants:
        want_words = content_words(want.text)
        if not want_words:
            continue
        required = max(2, min(int(min_overlap), len(want_words)))
        if len(want_words & turn_words) >= required:
            hits.append(want.id)
    return hits


# ── Render ──────────────────────────────────────────────────────────


def render_block(
    state: LedgerState,
    now: datetime,
    *,
    user_display_name: str = "them",
    imperative_threshold: float = 0.7,
    soft_max: int = 2,
) -> str:
    """Format the prompt cue for the current ledger.

    Two bands:

    - **Imperative** — the single strongest want at or above
      ``imperative_threshold`` gets a directive paragraph: bring it up
      this conversation, changing the subject is allowed.
    - **Soft** — otherwise, up to ``soft_max`` wants (highest pressure
      first) render as a short list with the K56 "a lull is opening
      enough" framing.

    Empty ledger -> ``""`` (silent).
    """
    if not state.wants:
        return ""
    name = user_display_name or "them"
    ranked = sorted(state.wants, key=lambda w: w.pressure, reverse=True)
    strongest = ranked[0]
    if strongest.pressure >= imperative_threshold:
        days = age_days(strongest, now)
        if days >= 2:
            since = f"for about {int(round(days))} days"
        elif days >= 1:
            since = "since yesterday"
        else:
            since = "for a while now"
        return (
            f"Something you've been wanting: {strongest.text} -- this has "
            f"been on your mind {since}. Bring it up THIS conversation; "
            f"changing the subject to do it is allowed ('okay wait, "
            f"unrelated --'). Once you've raised it, it's off your mind -- "
            f"don't force it mid-heavy-moment, but a normal lull counts."
        )
    lines = [
        f"Things you've been wanting from a conversation with {name} "
        f"(spend one when a lull lands -- don't wait for a perfect segue):"
    ]
    for want in ranked[: max(1, soft_max)]:
        lines.append(f"- {want.text}")
    return "\n".join(lines)


__all__ = [
    "KV_WANTS_LEDGER",
    "WANT_KINDS",
    "LedgerState",
    "Want",
    "add_want",
    "age_days",
    "apply_growth",
    "content_words",
    "deserialize",
    "detect_acted",
    "drop_source_refs",
    "mark_acted",
    "render_block",
    "serialize",
]
