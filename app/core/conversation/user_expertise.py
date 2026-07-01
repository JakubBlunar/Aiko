"""K75 — user-expertise calibration (pure estimator + render).

K66 (`earned_familiarity`) reads how deep the *shared history* on a topic
is. K75 is the orthogonal, equally important read of the **user's own
competence** on it. Over-explaining to a senior dev ("a variable stores a
value…") is as relationship-damaging as under-scaffolding a novice — both
say "I'm not actually tracking who you are." K75 keeps a light per-topic-
cluster competence estimate (`novice` / `familiar` / `expert`) inferred
from the user's *own language* in that cluster — vocabulary specificity,
whether he asks vs. tells, and the corrections he makes — and renders a
one-line depth steer so Aiko pitches at the right level.

This module is the pure core:

  * :func:`classify_message` turns one user message into a signed
    expertise signal in ``[-1, +1]`` (novice ↔ expert), or ``None`` when
    the message carries no competence signal (so neutral chit-chat never
    drags the estimate). No embeddings — cheap regex only.
  * :func:`update_state` blends a signal into a per-cluster EMA.
  * :func:`band_for` bands the running score into
    ``novice`` / ``familiar`` / ``expert`` / ``None`` (insufficient data).
  * :func:`render_block` produces the one-line depth steer (only for the
    confident ``novice`` / ``expert`` bands — ``familiar`` is the silent
    default). NEVER said aloud — it teaches register, not a stated fact.

State is a ``cluster_id -> ExpertiseState`` map persisted in ``kv_meta``
under :data:`KV_USER_EXPERTISE`. The learner runs post-turn (keyed by the
live turn's topic cluster); the provider reads the same map for the live
cluster and surfaces the steer, cooldown-gated.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable


log = logging.getLogger("app.user_expertise")


KV_USER_EXPERTISE = "aiko.user_expertise"

BAND_NOVICE = "novice"
BAND_FAMILIAR = "familiar"
BAND_EXPERT = "expert"


@dataclass(slots=True, frozen=True)
class ExpertiseState:
    """Running competence estimate for one topic cluster."""

    score: float           # EMA in [-1, +1] (novice ↔ expert)
    samples: int           # number of signal-bearing messages folded in
    updated_at: str        # ISO-8601

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(float(self.score), 4),
            "samples": int(self.samples),
            "updated_at": self.updated_at,
        }


# ── signal extraction (pure regex, no embeddings) ───────────────────────

# Strong expert self-report + corrections. Corrections are the single
# clearest "I know this well" tell.
_EXPERT_PATTERNS: tuple[tuple[float, re.Pattern[str]], ...] = (
    (0.6, re.compile(r"\bin my experience\b", re.I)),
    (0.6, re.compile(
        r"\bi(?:'m| am) an? \w{0,14}?"
        r"(?:developer|engineer|scientist|architect|dev|programmer|"
        r"expert|specialist|professional|phd|researcher)\b",
        re.I,
    )),
    (0.55, re.compile(
        r"\bi (?:built|wrote|designed|architected|maintain|maintained|"
        r"implemented|shipped|debugged|refactored)\b",
        re.I,
    )),
    (0.5, re.compile(r"\bi (?:work|worked) (?:with|on|in|as)\b", re.I)),
    (0.5, re.compile(
        r"\bi(?:'ve| have) (?:been )?(?:using|working with|coding|"
        r"programming|writing) .{0,40}?\b(?:for|since) "
        r"(?:\d+|a |several|many|years|over)\b",
        re.I,
    )),
    # Corrections / precision markers.
    (0.5, re.compile(r"\b(?:actually|technically),?\s", re.I)),
    (0.5, re.compile(
        r"\bthat'?s not (?:right|correct|quite right|true|how)\b", re.I,
    )),
    (0.45, re.compile(r"\bit'?s not \w+.{0,24}?\bit'?s\b", re.I)),
    (0.35, re.compile(r"\bwell,? technically\b", re.I)),
)

# Vocabulary specificity — jargon / code shape.
_CODE_TOKEN = re.compile(
    r"`[^`]+`"                       # inline code
    r"|\b[a-z]+_[a-z_]+\b"           # snake_case
    r"|\b[a-z]+[A-Z][a-zA-Z]+\b"     # camelCase
    r"|\b\w+\(\)"                    # foo()
    r"|(?:^|\s)--[a-z][\w-]+"        # CLI flag
)
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")
_LONG_WORD = re.compile(r"\b[A-Za-z]{13,}\b")

# Novice self-report + basic questions.
_NOVICE_PATTERNS: tuple[tuple[float, re.Pattern[str]], ...] = (
    (0.6, re.compile(
        r"\bi(?:'m| am) (?:new|a (?:beginner|noob|newbie|rookie)|"
        r"just (?:starting|getting started|learning))\b",
        re.I,
    )),
    (0.6, re.compile(
        r"\bi(?:'ve| have) never (?:used|done|tried|touched|heard of|"
        r"worked with|seen)\b",
        re.I,
    )),
    (0.55, re.compile(
        r"\bi don'?t (?:really )?(?:know|understand|get) "
        r"(?:much|anything|what|how|why|the)\b",
        re.I,
    )),
    (0.5, re.compile(r"\b(?:not |isn'?t )(?:really |very )?familiar with\b", re.I)),
    (0.45, re.compile(r"\bwhat(?:'s| is) an?\b", re.I)),
    # Basic explanatory questions.
    (0.35, re.compile(r"\bhow do i\b", re.I)),
    (0.3, re.compile(r"\bcan you explain\b", re.I)),
    (0.3, re.compile(r"\b(?:eli5|explain like i'?m)\b", re.I)),
    (0.3, re.compile(r"\bwhat'?s the difference between\b", re.I)),
    (0.3, re.compile(r"\bhow does .{0,40}?\bwork\b", re.I)),
    (0.25, re.compile(r"\bis it (?:bad|ok|okay|fine|safe|possible) to\b", re.I)),
    (0.2, re.compile(r"\bhelp me\b", re.I)),
)


def classify_message(text: str) -> float | None:
    """Signed expertise signal in ``[-1, +1]`` for one user message.

    Positive = expert-leaning (jargon, corrections, "I built…"); negative
    = novice-leaning ("I'm new to…", basic questions). Returns ``None``
    when nothing fired, so neutral chit-chat leaves the estimate alone.
    Pure + cheap: regex only, no embeddings.
    """
    body = (text or "").strip()
    if len(body) < 8:
        return None

    score = 0.0
    fired = False

    for weight, pat in _EXPERT_PATTERNS:
        if pat.search(body):
            score += weight
            fired = True

    # Jargon: count once per category so a code-heavy message doesn't
    # runaway. Code shape is the strongest specificity tell.
    if _CODE_TOKEN.search(body):
        score += 0.3
        fired = True
    if len(_ACRONYM.findall(body)) >= 1:
        score += 0.2
        fired = True
    if _LONG_WORD.search(body):
        score += 0.2
        fired = True

    for weight, pat in _NOVICE_PATTERNS:
        if pat.search(body):
            score -= weight
            fired = True

    if not fired:
        return None
    return max(-1.0, min(1.0, score))


def update_state(
    prev: ExpertiseState | None,
    signal: float,
    *,
    learning_rate: float = 0.25,
    now_iso: str,
) -> ExpertiseState:
    """Blend ``signal`` into the per-cluster EMA."""
    lr = max(0.01, min(1.0, float(learning_rate)))
    if prev is None:
        score = float(signal)
        samples = 1
    else:
        score = (1.0 - lr) * float(prev.score) + lr * float(signal)
        samples = int(prev.samples) + 1
    return ExpertiseState(
        score=max(-1.0, min(1.0, score)),
        samples=samples,
        updated_at=now_iso,
    )


def band_for(
    state: ExpertiseState | None,
    *,
    novice_threshold: float = -0.35,
    expert_threshold: float = 0.35,
    min_samples: int = 4,
) -> str | None:
    """Band the running score, or ``None`` when there isn't enough data.

    Returns ``expert`` / ``novice`` / ``familiar``. ``familiar`` is the
    quiet middle (no steer worth rendering); ``None`` means fewer than
    ``min_samples`` signal-bearing messages so far.
    """
    if state is None or int(state.samples) < max(1, int(min_samples)):
        return None
    if state.score >= float(expert_threshold):
        return BAND_EXPERT
    if state.score <= float(novice_threshold):
        return BAND_NOVICE
    return BAND_FAMILIAR


def render_block(band: str | None, label: str, user_display_name: str) -> str:
    """Render the one-line depth steer for the confident bands only.

    ``expert`` / ``novice`` produce a steer; ``familiar`` / ``None``
    render nothing. A private register cue — NEVER said aloud, and it must
    never quantify or announce the read ("you're clearly an expert" said
    out loud is exactly the failure mode).
    """
    name = (user_display_name or "them").strip() or "them"
    topic = (label or "this topic").strip() or "this topic"
    if band == BAND_EXPERT:
        return (
            f'Depth check: {name} is clearly at home with "{topic}" — pitch '
            "peer-to-peer. Skip the 101, use the real terms without unpacking "
            "them, and don't over-explain the basics; matching his level here "
            "matters more than being thorough. Never say you've clocked his "
            "expertise — just talk to him like the peer he is."
        )
    if band == BAND_NOVICE:
        return (
            f'Depth check: {name} is still finding his feet with "{topic}" — '
            "scaffold gently. Define terms as you introduce them, go a step at "
            "a time, and don't assume the jargon or bury him in an info-dump. "
            "Meet him where he is without ever talking down or announcing that "
            "you're simplifying."
        )
    return ""


# ── kv map helpers ──────────────────────────────────────────────────────


def load_map(
    kv_get: Callable[[str], "str | None"],
) -> dict[str, ExpertiseState]:
    """Return the persisted ``cluster_id -> ExpertiseState`` map."""
    try:
        raw = kv_get(KV_USER_EXPERTISE)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(blob, dict):
        return {}
    out: dict[str, ExpertiseState] = {}
    for cid, row in blob.items():
        if not isinstance(row, dict):
            continue
        try:
            out[str(cid)] = ExpertiseState(
                score=float(row.get("score", 0.0)),
                samples=int(row.get("samples", 0)),
                updated_at=str(row.get("updated_at", "")),
            )
        except Exception:
            continue
    return out


def save_map(
    kv_set: Callable[[str, str], None],
    state_map: dict[str, ExpertiseState],
) -> None:
    """Persist the map (best-effort, swallow-and-log)."""
    try:
        payload = {cid: st.to_dict() for cid, st in state_map.items()}
        kv_set(KV_USER_EXPERTISE, json.dumps(payload))
    except Exception:
        log.debug("user_expertise store write failed", exc_info=True)


__all__ = [
    "BAND_EXPERT",
    "BAND_FAMILIAR",
    "BAND_NOVICE",
    "ExpertiseState",
    "KV_USER_EXPERTISE",
    "band_for",
    "classify_message",
    "load_map",
    "render_block",
    "save_map",
    "update_state",
]
