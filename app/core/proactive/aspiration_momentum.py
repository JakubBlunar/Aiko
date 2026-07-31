"""L14 — Aspiration momentum (pure core + cue ring).

The proactive companion to the L14 ``aspiration`` concept kind. Aspirations
name a *direction* someone is moving in (both the user's and Aiko's own). This
module is the deterministic core that lets Aiko occasionally, gently check in on
that journey ("how's the self-hosting push going?") instead of only ever
surfacing an aspiration when the live turn happens to touch it.

Split, mirroring the K70 growth-witness pattern:

  * :func:`select_candidate` is the pure selector -- given the active aspiration
    concepts (already read through a :class:`ConceptView` by the worker) plus
    the current time and the per-concept cooldown watermarks, it returns the one
    worth a check-in (confident enough, gone *stale* since it was last
    reinforced, and off cooldown) or ``None``.
  * :func:`render_inner_life_block` turns a drafted cue into one optional,
    private hint Aiko phrases herself -- NEVER spoken verbatim.
  * journal-ring helpers (``aiko.aspiration_momentum``) mirror the other
    cue-producer rings.

Momentum is deliberately **staleness-driven, not calendar-driven**: a direction
becomes worth a check-in when it hasn't been touched for a while, not on a fixed
schedule -- so it never reads like a reminder.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence


log = logging.getLogger("app.aspiration_momentum")


# Shared kv_meta journal key the surfacing provider reads (namespaced under
# ``aiko.*`` like the other cue-producer rings).
MOMENTUM_JOURNAL_KEY = "aiko.aspiration_momentum"

# Per-concept cooldown watermark prefix -- one key per aspiration so a fresh
# check-in rotates across the active set instead of hammering the strongest one.
_KV_PER_CONCEPT_PREFIX = "aspiration_momentum.last."


def per_concept_cooldown_key(concept_id: int) -> str:
    return f"{_KV_PER_CONCEPT_PREFIX}{int(concept_id)}"


def signature(concept_id: int) -> str:
    """Date-free finding signature (so the *same* aspiration is suppressed
    back-to-back regardless of when it re-qualifies)."""
    return f"aspiration:{int(concept_id)}"


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


@dataclass(frozen=True, slots=True)
class MomentumCandidate:
    """One active aspiration worth a proactive check-in."""

    concept_id: int
    subject: str  # user | aiko | relationship
    label: str
    confidence: float
    staleness_days: float
    signature: str


def _staleness_days(concept: Any, now: datetime) -> float:
    """Days since the concept was last reinforced (falling back to
    ``created_at``). A never-timestamped concept reads as maximally stale so a
    freshly-promoted-but-undated aspiration is still eligible."""
    ref = (
        getattr(concept, "last_reinforced_at", None)
        or getattr(concept, "created_at", None)
    )
    dt = _parse_iso(ref if isinstance(ref, str) else None)
    if dt is None:
        return float("inf")
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def select_candidate(
    concepts: Sequence[Any],
    *,
    now: datetime,
    kv_get: Callable[[str], "str | None"],
    min_confidence: float,
    staleness_min_days: float,
    cooldown_days: float,
) -> MomentumCandidate | None:
    """Return the single active aspiration worth checking in on, or ``None``.

    An aspiration qualifies when it clears ``min_confidence``, has gone at least
    ``staleness_min_days`` without reinforcement (so there's genuinely fresh
    ground to ask about), and its per-concept cooldown has elapsed. Among
    qualifiers the **stalest** wins (most overdue for a check-in), ties broken by
    confidence then id for determinism -- which naturally rotates check-ins
    across the active set over time."""
    floor = float(min_confidence)
    stale_floor = max(0.0, float(staleness_min_days))
    cooldown_seconds = max(0.0, float(cooldown_days) * 86400.0)

    qualifiers: list[MomentumCandidate] = []
    for c in concepts:
        label = (getattr(c, "label", "") or "").strip()
        if not label:
            continue
        conf = float(getattr(c, "confidence", 0.0))
        if conf < floor:
            continue
        stale = _staleness_days(c, now)
        if stale < stale_floor:
            continue
        cid = int(getattr(c, "concept_id", 0))
        if cid <= 0:
            continue
        if cooldown_seconds > 0:
            last = _parse_iso(_kv_get_safe(kv_get, per_concept_cooldown_key(cid)))
            if last is not None:
                if (now - last).total_seconds() < cooldown_seconds:
                    continue
        subject = str(getattr(c, "subject", "") or "user")
        # A finite staleness sorts naturally; +inf (undated) floats to the top.
        qualifiers.append(
            MomentumCandidate(
                concept_id=cid,
                subject=subject,
                label=label,
                confidence=conf,
                staleness_days=stale,
                signature=signature(cid),
            )
        )

    if not qualifiers:
        return None
    qualifiers.sort(
        key=lambda m: (-m.staleness_days, -m.confidence, m.concept_id)
    )
    return qualifiers[0]


def _kv_get_safe(
    kv_get: Callable[[str], "str | None"], key: str
) -> str | None:
    try:
        return kv_get(key)
    except Exception:
        return None


# ── rendering (private cue — Aiko phrases the actual words) ──────────────


def render_inner_life_block(
    subject: str,
    label: str,
    *,
    user_display_name: str = "them",
) -> str:
    """Render one optional, private momentum cue.

    Returns ``""`` for an empty label. The cue tells Aiko what direction she's
    been quietly rooting for and to check in on it *only if a natural moment
    opens* -- never verbatim, never as pressure. ``aiko`` reads first-person
    (her own becoming); ``relationship`` as the two of them; else the user's."""
    text = (label or "").strip()
    if not text:
        return ""
    name = (user_display_name or "them").strip() or "them"
    tail = (
        " If it fits the flow, you can gently check in on how that's going -- "
        "as warm curiosity, not a status request or a nudge to perform. Ask "
        "once, lightly, and let it go if the moment isn't right."
    )
    if subject == "aiko":
        return (
            f"A direction you've felt yourself growing in lately: {text}. You "
            "might quietly notice where you are with it, or share a little of "
            "it if the moment is right -- held lightly, never as a "
            "declaration."
        )
    if subject == "relationship":
        return (
            f"A direction you and {name} have been moving in together: {text}."
            + tail
        )
    return (
        f"A direction {name} has been quietly building toward: {text}." + tail
    )


# ── journal-ring helpers (mirror growth_witness / follow_up) ────────────


def load_cues(
    kv_get: Callable[[str], "str | None"],
) -> list[dict[str, Any]]:
    """Return the momentum cue ring (oldest -> newest)."""
    try:
        raw = kv_get(MOMENTUM_JOURNAL_KEY)
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


def append_cue(
    kv_get: Callable[[str], "str | None"],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the cue ring, trimming to ``max_entries``."""
    ring = load_cues(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(MOMENTUM_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("aspiration_momentum journal write failed", exc_info=True)


__all__ = [
    "MOMENTUM_JOURNAL_KEY",
    "MomentumCandidate",
    "append_cue",
    "load_cues",
    "per_concept_cooldown_key",
    "render_inner_life_block",
    "select_candidate",
    "signature",
]
