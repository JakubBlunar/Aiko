"""K74 — humor-style calibration ("what kind of funny lands").

K48 tease-rhythm governs the *budget* (how much snark) and K59 the
*economy* (payback timing), but nothing tracks **which kind of humor**
actually lands for this user — puns vs dry/deadpan vs absurdist vs
self-deprecating vs playful-roast. This module mirrors the J11
``affection_style`` learner exactly: a per-user weighting over a small
humor-kind taxonomy on ``kv_meta``, learned **passively** from the K14
engagement read attributed to the humor kind Aiko used last turn, with
K32 reactions (😂 / 🙄) as a sparse confirmation booster.

Design contract (same as J11 affection-style):

- **Reaction-optional.** The primary signal is passive: after a turn
  where Aiko's humor read as a given kind, the next user turn's K14
  engagement (engaged / disengaged + length z) is attributed back to
  that kind. Reactions only *confirm*.
- **Bias, never collapse.** Weights are floored; nothing zeroes a
  register. Variety is the point.
- **Sparse by construction.** Unlike affection (every turn carries
  *some* care), most turns carry *no* humor — :func:`classify_turn_humor`
  returns ``[]`` on a non-funny turn, so learning only happens on turns
  where humor was actually detectable.
- **Slow forgetting.** A background idle worker decays toward uniform.
- **Storage on ``kv_meta``, no schema** — one JSON key
  ``aiko.humor_style``.

Behavioural effect: the learned top register is surfaced **only** as a
short suffix on the *existing* K48 tease-rhythm cue (when humor is
already in play) via :func:`register_hint` — it never introduces a new
standalone narrated block. This is the honest "tilt the existing tease
paths" lever (there is no deterministic humor-register selector in code
to multiply a cooldown against).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


KV_HUMOR_STYLE = "aiko.humor_style"


# ── Taxonomy ────────────────────────────────────────────────────────


HUMOR_KINDS: tuple[str, ...] = (
    "pun",
    "deadpan",
    "absurdist",
    "self_deprecating",
    "playful_roast",
)

_UNIFORM = 1.0 / len(HUMOR_KINDS)

# Human-readable register labels for the cue suffix.
_REGISTER_LABEL: dict[str, str] = {
    "pun": "wordplay / puns",
    "deadpan": "dry, deadpan",
    "absurdist": "absurd, over-the-top",
    "self_deprecating": "self-deprecating",
    "playful_roast": "playful roasting",
}


# Assistant ``[[reaction:...]]`` mood tags that signal humour is in play.
_HUMOR_REACTIONS: frozenset[str] = frozenset(
    {"playful", "mischievous", "smug", "amused", "pouty", "sulky", "teasing"}
)


# Per-kind text markers (cheap, conservative — bias, not truth).
_SELF_DEPRECATING_RE = re.compile(
    r"\b("
    r"i'm hopeless|i'm useless|i'm a (?:mess|disaster|wreck)|"
    r"i'm the worst|don't ask me|clearly i (?:have no|know nothing)|"
    r"look at me (?:failing|messing)|i can't even|"
    r"i'm such a (?:dork|goof|disaster)|my one job|i had one job"
    r")\b",
    re.IGNORECASE,
)

_ABSURDIST_RE = re.compile(
    r"\b("
    r"plot twist|alternate universe|narrator voice|in my defen[cs]e|"
    r"obviously the (?:cat|dog|ghost)|breaking news|"
    r"scientists hate|local (?:woman|man|cat)|"
    r"this is fine|legally i (?:can|cannot)|"
    r"in this economy|the prophecy (?:is|was)"
    r")\b",
    re.IGNORECASE,
)

_PUN_RE = re.compile(
    r"(no pun intended|pun intended|i'll see myself out|"
    r"\bpunny\b|if you (?:catch|get) my drift)",
    re.IGNORECASE,
)

# Playful-roast: a second-person jab. Reuse a small marker set close to
# the K48 tease text markers.
_ROAST_RE = re.compile(
    r"\b("
    r"you (?:absolute |total |complete )?(?:dork|nerd|goof(?:ball)?|"
    r"goose|menace|disaster|clown)|"
    r"sure you (?:did|will|are)|yeah,? right|nice (?:try|one,? genius)|"
    r"of course you (?:did|do|would)|real smooth|big talk|"
    r"oh,? please|cute that you"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HumorStyleState:
    weights: dict[str, float]
    updated_at: str

    def weight_of(self, kind: str) -> float:
        return float(self.weights.get(kind, _UNIFORM))


def uniform_state(now: datetime | None = None) -> HumorStyleState:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    return HumorStyleState(
        weights={k: _UNIFORM for k in HUMOR_KINDS}, updated_at=ts,
    )


def _normalise(weights: dict[str, float], *, floor: float) -> dict[str, float]:
    safe_floor = max(0.0, min(_UNIFORM, float(floor)))
    clamped = {
        k: max(safe_floor, float(weights.get(k, 0.0))) for k in HUMOR_KINDS
    }
    total = sum(clamped.values())
    if total <= 0.0:
        return {k: _UNIFORM for k in HUMOR_KINDS}
    return {k: v / total for k, v in clamped.items()}


# ── Signal mapping ──────────────────────────────────────────────────


def engagement_to_signal(
    label: str | None, length_z: float | None = None,
) -> float:
    """Map a K14 engagement outcome to a signed signal in ``[-1, 1]``."""
    band = (label or "").strip().lower()
    if band == "engaged":
        base = 1.0
    elif band == "abandoned":
        base = -1.0
    elif band == "disengaged":
        base = -0.6
    else:
        base = 0.0
    if length_z is not None and band in ("engaged", "neutral"):
        base += max(-0.3, min(0.3, float(length_z) * 0.15))
    return max(-1.0, min(1.0, base))


def classify_turn_humor(
    raw_assistant_text: str, reaction: str | None,
) -> list[str]:
    """Tag which humor kind(s) a finished assistant turn carried.

    Returns ``[]`` when no humour is detectable (the common case) — the
    caller then skips learning for that turn. A humour-signalling
    ``[[reaction:...]]`` (playful / smug / amused / …) with *no* overt
    text marker is read as ``deadpan`` (dry delivery has no lexical
    tell). Multiple markers can fire (e.g. a self-deprecating pun).
    """
    text = raw_assistant_text or ""
    react = (reaction or "").strip().lower()
    found: set[str] = set()

    if _PUN_RE.search(text):
        found.add("pun")
    if _SELF_DEPRECATING_RE.search(text):
        found.add("self_deprecating")
    if _ABSURDIST_RE.search(text):
        found.add("absurdist")
    if _ROAST_RE.search(text):
        found.add("playful_roast")

    humour_reaction = react in _HUMOR_REACTIONS
    # Dry-delivery fallback: humour was clearly in play (reaction tag)
    # but no overt marker fired -> deadpan.
    if not found and humour_reaction:
        found.add("deadpan")

    # If only text markers fired but no humour reaction, keep them —
    # they are explicit enough on their own.
    return [k for k in HUMOR_KINDS if k in found]


# ── Mutation ────────────────────────────────────────────────────────


def apply_observation(
    state: HumorStyleState,
    kinds: Iterable[str],
    signal: float,
    now: datetime,
    *,
    learning_rate: float,
    floor: float,
) -> HumorStyleState:
    kind_list = [k for k in kinds if k in HUMOR_KINDS]
    ts = now.isoformat()
    if not kind_list or signal == 0.0 or learning_rate <= 0.0:
        return HumorStyleState(weights=dict(state.weights), updated_at=ts)
    per = float(learning_rate) * float(signal) / float(len(kind_list))
    raw = {k: state.weight_of(k) for k in HUMOR_KINDS}
    for k in kind_list:
        raw[k] = raw[k] + per
    return HumorStyleState(weights=_normalise(raw, floor=floor), updated_at=ts)


def apply_reaction_confirmation(
    state: HumorStyleState,
    kinds: Iterable[str],
    now: datetime,
    *,
    reaction_weight: float,
    floor: float,
) -> HumorStyleState:
    """Boost the given humor ``kinds`` (a 😂/🙄 confirmed they landed).

    Unlike J11 (which maps each reaction to one fixed affection kind), a
    laugh confirms *whichever humor register Aiko just used* — so the
    caller passes the previous turn's tagged kinds. Empty -> no-op.
    """
    kind_list = [k for k in kinds if k in HUMOR_KINDS]
    ts = now.isoformat()
    if not kind_list:
        return HumorStyleState(weights=dict(state.weights), updated_at=ts)
    bump = max(0.0, float(reaction_weight)) / float(len(kind_list))
    raw = {k: state.weight_of(k) for k in HUMOR_KINDS}
    for k in kind_list:
        raw[k] = raw[k] + bump
    return HumorStyleState(weights=_normalise(raw, floor=floor), updated_at=ts)


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


def decay_toward_uniform(
    state: HumorStyleState,
    now: datetime,
    *,
    half_life_days: float,
    floor: float,
) -> HumorStyleState:
    ts = now.isoformat()
    if half_life_days <= 0.0:
        return HumorStyleState(weights=dict(state.weights), updated_at=ts)
    stored_at = _parse_iso(state.updated_at)
    if stored_at is None:
        return HumorStyleState(weights=dict(state.weights), updated_at=ts)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(
        tzinfo=timezone.utc,
    )
    elapsed_days = (now_utc - stored_at).total_seconds() / 86400.0
    if elapsed_days <= 0.0:
        return HumorStyleState(weights=dict(state.weights), updated_at=ts)
    keep = math.pow(0.5, elapsed_days / float(half_life_days))
    raw = {
        k: _UNIFORM + (state.weight_of(k) - _UNIFORM) * keep
        for k in HUMOR_KINDS
    }
    return HumorStyleState(weights=_normalise(raw, floor=floor), updated_at=ts)


# ── Surfacing (rides the existing K48 tease cue) ────────────────────


def register_hint(
    state: HumorStyleState,
    user_name: str | None = None,
    *,
    min_rel: float = 1.25,
) -> str:
    """Short register suffix for an existing humour cue, or ``""``.

    Only fires when the top register sits ``>= min_rel`` of the uniform
    share (a learned preference that has genuinely emerged); otherwise
    silent so a near-uniform learner adds nothing. Phrased as a soft
    register nudge, never a "I've noticed you like…" narration.
    """
    kind = top_kind(state)
    if state.weight_of(kind) < min_rel * _UNIFORM:
        return ""
    name = (user_name or "they").strip() or "they"
    label = _REGISTER_LABEL.get(kind, kind.replace("_", " "))
    return (
        f" The kind of funny that's been landing with {name} lately is "
        f"{label} -- lean that way if you push."
    )


# ── Serialise / deserialise ─────────────────────────────────────────


def serialize(state: HumorStyleState) -> str:
    return json.dumps(
        {
            "weights": {k: float(state.weight_of(k)) for k in HUMOR_KINDS},
            "updated_at": str(state.updated_at),
        }
    )


def deserialize(text: str | None) -> HumorStyleState:
    if not text:
        return uniform_state()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return uniform_state()
    if not isinstance(data, dict):
        return uniform_state()
    raw_weights = data.get("weights")
    if not isinstance(raw_weights, dict):
        return uniform_state()
    weights: dict[str, float] = {}
    for k in HUMOR_KINDS:
        try:
            weights[k] = float(raw_weights.get(k, _UNIFORM))
        except (TypeError, ValueError):
            weights[k] = _UNIFORM
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        updated_at = datetime.now(timezone.utc).isoformat()
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0.0:
        return uniform_state(now=_parse_iso(updated_at))
    return HumorStyleState(
        weights={k: max(0.0, v) / total for k, v in weights.items()},
        updated_at=updated_at,
    )


def top_kind(state: HumorStyleState) -> str:
    return max(
        HUMOR_KINDS,
        key=lambda k: (state.weight_of(k), -HUMOR_KINDS.index(k)),
    )


__all__ = [
    "HUMOR_KINDS",
    "HumorStyleState",
    "KV_HUMOR_STYLE",
    "apply_observation",
    "apply_reaction_confirmation",
    "classify_turn_humor",
    "decay_toward_uniform",
    "deserialize",
    "engagement_to_signal",
    "register_hint",
    "serialize",
    "top_kind",
    "uniform_state",
]
