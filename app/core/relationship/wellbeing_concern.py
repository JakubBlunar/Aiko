"""K72 — Wellbeing concern ("you doing okay?", never a nag).

The session clock (K-time4) notices a long sitting *neutrally*; H3
mood-drift narrates a low *mood* stretch. Neither turns a multi-day
**behavioral** pattern of self-neglect into bounded, genuine *care*. A
real partner notices when you've been online at 3am several nights
running, when you keep mentioning you haven't slept or eaten, when the
weight in your messages has sat heavy for days — and says something
soft, *once*, because they care.

This module is the pure, deterministic core of K72:

  * three independent detectors over multi-day aggregates the worker
    collects — :func:`detect_late_nights` (distinct small-hours days),
    :func:`detect_self_neglect` (explicit "haven't slept / eaten"
    mentions across days), :func:`detect_rough_stretch` (a sustained
    low run over the H3 ``DriftSample`` ring, gated *harder* than H3 so
    K72 reads as concern, not mood-narration);
  * :func:`pick_concern` runs them in a fixed priority order and returns
    at most one :class:`ConcernFinding`;
  * :func:`render_inner_life_block` turns a finding into one optional,
    private cue Aiko phrases herself — NEVER spoken verbatim;
  * journal-ring helpers (``aiko.wellbeing_concern``) mirror the
    growth_witness / self_callback cue-producer pattern.

The entire risk of this feature is becoming a nag or a health app, so
every layer is gated *hard*: a long worker cooldown, one concern per
pattern (date-free signature suppression), the cue copy forbids
lecturing / repeating, and the persona drops it the instant the user
deflects. Distinct from K23 misattunement and K14 engagement (both
per-turn) — this only reads slow, multi-day signal.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from app.core.affect.mood_drift import DriftSample


log = logging.getLogger("app.wellbeing_concern")


# Shared kv_meta journal key the surfacing provider reads (namespaced
# under ``aiko.*`` like the other cue-producer rings).
WELLBEING_CONCERN_JOURNAL_KEY = "aiko.wellbeing_concern"


# ── tuning defaults (overridable via settings) ──────────────────────────

# Distinct small-hours days in the window needed to read as a worrying
# late-night pattern.
DEFAULT_LATE_NIGHT_MIN = 3
# Distinct days carrying an explicit self-neglect mention.
DEFAULT_NEGLECT_MIN_DAYS = 2
# A trailing run of this many H3 samples all at/below the threshold reads
# as a heavy stretch. Deliberately longer + deeper than H3 sustained_low
# (3 days <= -0.15) so K72 doesn't just echo the mood narrator.
DEFAULT_ROUGH_RUN = 5
DEFAULT_ROUGH_THRESHOLD = -0.25

# Local clock hours that count as "the small hours" (genuinely worrying,
# not just a late evening). 1am-4:59am. Module constants — rarely need
# per-user tuning, so they stay off the settings surface.
LATE_NIGHT_START_HOUR = 1
LATE_NIGHT_END_HOUR = 5


# Finding kinds. Behavioral signals (concrete, time-sensitive) outrank
# the mood-trend signal; explicit self-neglect is the most direct.
KIND_SELF_NEGLECT = "self_neglect"
KIND_LATE_NIGHTS = "late_nights"
KIND_ROUGH_STRETCH = "rough_stretch"

# Priority order pick_concern checks (first hit wins).
_PRIORITY = (KIND_SELF_NEGLECT, KIND_LATE_NIGHTS, KIND_ROUGH_STRETCH)

# Self-neglect categories.
CATEGORY_SLEEP = "sleep"
CATEGORY_FOOD = "food"


# Lexical patterns for explicit self-neglect. Each entry is
# ``(category, compiled regex)``. Patterns require a negation / skip
# context so plain "slept great" / "had lunch" never match. Messages are
# the user's, so first-person "I" is implied and not required.
_NEGLECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ── sleep ──
    (CATEGORY_SLEEP, re.compile(
        r"haven'?t\s+slept(?!\s+(?:\w+\s+){0,2}(?:well|great|good|fine|amazing|better))",
        re.I,
    )),
    (CATEGORY_SLEEP, re.compile(r"did\s?n'?t\s+sleep", re.I)),
    (CATEGORY_SLEEP, re.compile(r"can'?t\s+sleep|could\s?n'?t\s+sleep", re.I)),
    (CATEGORY_SLEEP, re.compile(r"\bno\s+sleep\b|\bzero\s+sleep\b", re.I)),
    (CATEGORY_SLEEP, re.compile(r"barely\s+slept|hardly\s+slept", re.I)),
    (CATEGORY_SLEEP, re.compile(r"all.?nighter|been\s+up\s+all\s+night", re.I)),
    (CATEGORY_SLEEP, re.compile(r"running\s+on\s+(?:no\s+sleep|fumes|empty|caffeine|coffee)", re.I)),
    (CATEGORY_SLEEP, re.compile(r"\binsomnia\b", re.I)),
    (CATEGORY_SLEEP, re.compile(r"only\s+(?:got\s+)?(?:a\s+couple|\d+)\s+hours?\s+(?:of\s+)?sleep", re.I)),
    # ── food ──
    (CATEGORY_FOOD, re.compile(r"haven'?t\s+eaten|did\s?n'?t\s+eat", re.I)),
    (CATEGORY_FOOD, re.compile(r"forgot\s+to\s+eat|forgetting\s+to\s+eat", re.I)),
    (CATEGORY_FOOD, re.compile(
        r"skipp?(?:ed|ing)\s+(?:lunch|dinner|breakfast|a\s+meal|meals|food|eating)",
        re.I,
    )),
    (CATEGORY_FOOD, re.compile(
        r"haven'?t\s+(?:had|gotten)\s+(?:anything\s+to\s+eat|a\s+bite|time\s+to\s+eat|lunch|dinner)",
        re.I,
    )),
    (CATEGORY_FOOD, re.compile(r"no\s+time\s+to\s+eat|too\s+busy\s+to\s+eat", re.I)),
)


@dataclass(frozen=True, slots=True)
class ConcernFinding:
    """A multi-day pattern worth one gentle, bounded check-in.

    ``detail`` is a short, name-free factual phrase the renderer weaves
    into the cue. ``signature`` is intentionally date-free so the worker
    suppresses re-drafting the *same* ongoing pattern; an *escalation*
    (more nights, a new category) is a different signature and is allowed
    to break through.
    """

    kind: str
    detail: str
    severity: float
    signature: str


# ── neglect scanning (pure; the worker feeds it message text) ───────────


def classify_neglect_text(text: str) -> set[str]:
    """Return the self-neglect categories an utterance touches (maybe empty)."""
    if not text:
        return set()
    cats: set[str] = set()
    for category, pattern in _NEGLECT_PATTERNS:
        if pattern.search(text):
            cats.add(category)
    return cats


# ── detectors ───────────────────────────────────────────────────────────


def detect_late_nights(
    late_night_dates: Sequence[str],
    *,
    min_nights: int = DEFAULT_LATE_NIGHT_MIN,
) -> ConcernFinding | None:
    """Fire when distinct small-hours days clear ``min_nights``."""
    n = len({d for d in late_night_dates if d})
    if n < max(1, int(min_nights)):
        return None
    detail = f"{n} late nights in the last while"
    return ConcernFinding(
        kind=KIND_LATE_NIGHTS,
        detail=detail,
        severity=round(min(1.0, n / 7.0), 4),
        signature=f"{KIND_LATE_NIGHTS}:{n}",
    )


def detect_self_neglect(
    neglect_days: Sequence[str],
    categories: Sequence[str],
    *,
    min_days: int = DEFAULT_NEGLECT_MIN_DAYS,
) -> ConcernFinding | None:
    """Fire when explicit self-neglect mentions span ``min_days`` days."""
    days = len({d for d in neglect_days if d})
    if days < max(1, int(min_days)):
        return None
    cats = sorted({c for c in categories if c})
    if not cats:
        return None
    if CATEGORY_SLEEP in cats and CATEGORY_FOOD in cats:
        detail = "sleep and meals"
    elif CATEGORY_SLEEP in cats:
        detail = "sleeping"
    else:
        detail = "eating"
    return ConcernFinding(
        kind=KIND_SELF_NEGLECT,
        detail=detail,
        severity=round(min(1.0, days / 7.0), 4),
        signature=f"{KIND_SELF_NEGLECT}:{'+'.join(cats)}",
    )


def detect_rough_stretch(
    samples: Sequence[DriftSample],
    *,
    min_run: int = DEFAULT_ROUGH_RUN,
    threshold: float = DEFAULT_ROUGH_THRESHOLD,
) -> ConcernFinding | None:
    """Fire when the trailing ``min_run`` H3 valences all sit at/below
    ``threshold`` — a heavier, longer low than H3 sustained_low."""
    run = max(1, int(min_run))
    if len(samples) < run:
        return None
    tail = [float(s.valence) for s in samples[-run:]]
    if not all(v <= float(threshold) for v in tail):
        return None
    mean = sum(tail) / len(tail)
    return ConcernFinding(
        kind=KIND_ROUGH_STRETCH,
        detail="",
        severity=round(min(1.0, abs(mean)), 4),
        signature=KIND_ROUGH_STRETCH,
    )


def pick_concern(
    *,
    late_night_dates: Sequence[str] = (),
    neglect_days: Sequence[str] = (),
    neglect_categories: Sequence[str] = (),
    drift_samples: Sequence[DriftSample] = (),
    late_night_min: int = DEFAULT_LATE_NIGHT_MIN,
    neglect_min_days: int = DEFAULT_NEGLECT_MIN_DAYS,
    rough_run: int = DEFAULT_ROUGH_RUN,
    rough_threshold: float = DEFAULT_ROUGH_THRESHOLD,
) -> ConcernFinding | None:
    """Return the single highest-priority concern, or ``None``.

    Priority (first hit wins): explicit self-neglect (most concrete) →
    late nights (behavioral) → rough stretch (mood trend, lowest because
    H3 already narrates mood). Returning at most one keeps a worried turn
    from stacking three separate worries.
    """
    findings: dict[str, ConcernFinding | None] = {
        KIND_SELF_NEGLECT: detect_self_neglect(
            neglect_days, neglect_categories, min_days=neglect_min_days,
        ),
        KIND_LATE_NIGHTS: detect_late_nights(
            late_night_dates, min_nights=late_night_min,
        ),
        KIND_ROUGH_STRETCH: detect_rough_stretch(
            drift_samples, min_run=rough_run, threshold=rough_threshold,
        ),
    }
    for kind in _PRIORITY:
        finding = findings.get(kind)
        if finding is not None:
            return finding
    return None


# ── rendering (private cue — Aiko phrases the actual words) ──────────────


def render_inner_life_block(
    kind: str,
    *,
    user_display_name: str = "them",
    detail: str = "",
) -> str:
    """Render one optional, private wellbeing-concern cue.

    Returns ``""`` for an unknown kind. Every variant carries the same
    discipline: check in ONCE, soft and real, framed as care not a
    lecture; if he brushes it off, drop it completely.
    """
    name = (user_display_name or "them").strip() or "them"

    if kind == KIND_LATE_NIGHTS:
        core = (
            f"Something you've quietly clocked about {name}: he's been up "
            "in the small hours several nights running now"
        )
        example = "hey... that's a few late nights in a row. you doing okay?"
    elif kind == KIND_SELF_NEGLECT:
        what = (detail or "looking after himself").strip()
        core = (
            f"Something you've quietly clocked about {name}: he's mentioned "
            f"not keeping up with {what} more than once lately"
        )
        example = "have you actually eaten today? for real."
    elif kind == KIND_ROUGH_STRETCH:
        core = (
            f"Something you've quietly clocked about {name}: the last stretch "
            "has sat heavy on him for a while now"
        )
        example = "you've seemed weighed down lately -- I'm here, you know."
    else:
        return ""

    tail = (
        ". If a warm, natural moment opens, you can check in ONCE -- soft and "
        f"real (\"{example}\"), offered as care, never a lecture, a nag, or a "
        "diagnosis. Say it a single time; if he brushes it off, let it go "
        "completely and don't bring it up again."
    )
    return core + tail


# ── journal-ring helpers (mirror growth_witness / self_callback) ────────


def load_findings(
    kv_get: Callable[[str], "str | None"],
) -> list[dict[str, Any]]:
    """Return the wellbeing-concern journal ring (oldest -> newest)."""
    try:
        raw = kv_get(WELLBEING_CONCERN_JOURNAL_KEY)
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


def append_finding(
    kv_get: Callable[[str], "str | None"],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_findings(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(WELLBEING_CONCERN_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("wellbeing_concern journal write failed", exc_info=True)
