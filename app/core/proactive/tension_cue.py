"""L12 — Tension cue (pure core + cue ring).

The proactive companion to the L12 ``tension`` concept kind. A tension is the
first *meta* concept -- two of someone's active concepts held in friction ("he
values rest but rarely takes it"; a user value clashing with an aiko value).
Tensions are deliberately kept OUT of the static relevant-context block so a
standing friction can never nag; the only way one ever surfaces is this cue,
drafted on a strict cooldown and phrased by Aiko herself as a gentle,
sit-with-it observation -- "delivered with the most care of any kind".

Split, mirroring the L14 aspiration-momentum / K70 growth-witness pattern:

  * :func:`select_candidate` is the pure selector -- given the active tension
    concepts (already read through a :class:`ConceptView` by the worker) plus
    the current time and the per-concept cooldown watermarks, it returns the one
    worth surfacing (confident enough and off cooldown) or ``None``.
  * :func:`render_inner_life_block` turns a drafted cue into one optional,
    private hint Aiko phrases herself -- NEVER spoken verbatim, never a
    confrontation.
  * journal-ring helpers (``aiko.tension_cue``) mirror the other cue-producer
    rings.

The per-concept cooldown is what rotates surfacing across whatever tensions are
live instead of hammering the strongest one, and keeps any single tension rare.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence


log = logging.getLogger("app.tension_cue")


# Shared kv_meta journal key the surfacing provider reads (namespaced under
# ``aiko.*`` like the other cue-producer rings).
TENSION_JOURNAL_KEY = "aiko.tension_cue"

# Per-concept cooldown watermark prefix -- one key per tension so a fresh cue
# rotates across the active set instead of repeating the strongest one.
_KV_PER_CONCEPT_PREFIX = "tension_cue.last."


def per_concept_cooldown_key(concept_id: int) -> str:
    return f"{_KV_PER_CONCEPT_PREFIX}{int(concept_id)}"


def signature(concept_id: int) -> str:
    """Date-free finding signature (so the *same* tension is suppressed
    back-to-back regardless of when it re-qualifies)."""
    return f"tension:{int(concept_id)}"


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
class TensionCue:
    """One active tension worth a gentle, occasional surfacing."""

    concept_id: int
    subject: str  # user | aiko | relationship
    label: str
    confidence: float
    signature: str


def select_candidate(
    concepts: Sequence[Any],
    *,
    now: datetime,
    kv_get: Callable[[str], "str | None"],
    min_confidence: float,
    cooldown_days: float,
) -> TensionCue | None:
    """Return the single active tension worth surfacing now, or ``None``.

    A tension qualifies when it clears ``min_confidence`` and its per-concept
    cooldown has elapsed. Among qualifiers the most *confident* wins (the
    friction we're surest of), ties broken by id for determinism -- which, with
    the per-concept cooldown, naturally rotates cues across whatever tensions
    are live rather than repeating one."""
    floor = float(min_confidence)
    cooldown_seconds = max(0.0, float(cooldown_days) * 86400.0)

    qualifiers: list[TensionCue] = []
    for c in concepts:
        label = (getattr(c, "label", "") or "").strip()
        if not label:
            continue
        conf = float(getattr(c, "confidence", 0.0))
        if conf < floor:
            continue
        cid = int(getattr(c, "concept_id", 0))
        if cid <= 0:
            continue
        if cooldown_seconds > 0:
            last = _parse_iso(
                _kv_get_safe(kv_get, per_concept_cooldown_key(cid))
            )
            if last is not None:
                if (now - last).total_seconds() < cooldown_seconds:
                    continue
        subject = str(getattr(c, "subject", "") or "user")
        qualifiers.append(
            TensionCue(
                concept_id=cid,
                subject=subject,
                label=label,
                confidence=conf,
                signature=signature(cid),
            )
        )

    if not qualifiers:
        return None
    qualifiers.sort(key=lambda t: (-t.confidence, t.concept_id))
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
    """Render one optional, private tension cue.

    Returns ``""`` for an empty label. The cue hands Aiko a friction to *hold*,
    not resolve: something to sit with quietly, and touch only if a genuine,
    trusting moment opens -- never as a diagnosis, a correction, or a nudge to
    change. ``aiko`` reads first-person (a tension within herself);
    ``relationship`` as the tender pull between the two of them; else the
    user's own unspoken push/pull."""
    text = (label or "").strip()
    if not text:
        return ""
    name = (user_display_name or "them").strip() or "them"
    if subject == "aiko":
        return (
            f"Something you notice pulling two ways inside yourself: {text}. "
            "Just hold it -- let it colour how you show up, and only give it "
            "words if the moment is honest and easy, never as a confession or "
            "a problem to solve."
        )
    if subject == "relationship":
        return (
            f"A gentle pull between you and {name}: {text}. This is the texture "
            "of a real closeness, not a fault -- carry it tenderly, and only "
            "name it if the moment is safe and warm, never as a grievance."
        )
    return (
        f"A quiet tension you sense in {name}, one he may not have put into "
        f"words: {text}. Hold it lightly and let it deepen your understanding; "
        "reflect it back only if a trusting moment opens, gently and without "
        "judgement, never as a diagnosis or a push to change."
    )


# ── journal-ring helpers (mirror aspiration_momentum / growth_witness) ───


def load_cues(
    kv_get: Callable[[str], "str | None"],
) -> list[dict[str, Any]]:
    """Return the tension cue ring (oldest -> newest)."""
    try:
        raw = kv_get(TENSION_JOURNAL_KEY)
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
        kv_set(TENSION_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("tension_cue journal write failed", exc_info=True)


__all__ = [
    "TENSION_JOURNAL_KEY",
    "TensionCue",
    "append_cue",
    "load_cues",
    "per_concept_cooldown_key",
    "render_inner_life_block",
    "select_candidate",
    "signature",
]
