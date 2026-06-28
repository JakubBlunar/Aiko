"""J11 — affection-style learning ("how he likes to be cared for").

Aiko expresses care in a roughly fixed mix — teasing (K48/K59),
appreciation (J10), touch (K31), plain words, and giving space — but
nothing learns *which of those land* for this particular user. This
module distils that into a per-user weighting over five affection
**kinds** and lets the existing gesture / tease / appreciation gates
read it so her expression *mix* drifts toward what reliably lands.

Design contract (mirrors the K-state pure-module convention used by
K15 ``vulnerability_budget`` and K52 ``wants_ledger``):

- **Reaction-optional.** The primary signal is *passive*: after a turn
  where Aiko expressed a given kind, the next user turn's K14
  :class:`EngagementResult` (engaged / disengaged / abandoned + a
  length z-score) is attributed back to that kind. Warm engagement
  nudges the kind up; a short / cold reply nudges it down. Explicit K32
  reactions are only a *confirmation* booster on top — they are sparse
  by nature ("reactions should be confirmation, not required"), so the
  learner must work with zero of them.
- **Bias, never collapse.** Weights are floored (every kind keeps a
  minimum share) and the gate-bias helper clamps its multiplier to a
  ``[floor, ceil]`` band, so a learned preference *tilts* frequency
  without ever zeroing a channel. Variety is the point — she must never
  read as a single-note affection machine.
- **Never announced.** The weighting is *not* rendered into any prompt
  block. It only colours the gates. (The tonal guard: no "I've noticed
  you like it when I…".)
- **Slow forgetting.** A background idle worker decays the weights
  toward uniform over a long half-life so a stale preference fades if
  the relationship changes.
- **Storage on ``kv_meta``, no schema.** One JSON key
  ``aiko.affection_style`` carrying ``{weights: {...}, updated_at:
  ISO}``, namespaced under the existing ``aiko.*`` prefix.

The pure module has no I/O, no scheduler, no controller — it is
unit-tested in milliseconds. Lifecycle wiring lives in
``post_turn_mixin.py`` (tag + attribute), the touch / tease /
appreciation gates (read the bias), and ``affection_style_worker.py``
(decay).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


# ── kv_meta key ─────────────────────────────────────────────────────


KV_AFFECTION_STYLE = "aiko.affection_style"


# ── Taxonomy ────────────────────────────────────────────────────────


# The five ways Aiko expresses care. Ordered most-physical to
# most-absent; the order is only cosmetic (dicts below are keyed).
AFFECTION_KINDS: tuple[str, ...] = (
    "touch",
    "teasing",
    "appreciation",
    "words",
    "space",
)

_UNIFORM = 1.0 / len(AFFECTION_KINDS)


# Map a K32 user-reaction kind to the affection kind it *confirms*.
# Reactions are the confirmation channel, so every entry produces a
# small positive vote toward the mapped kind. ``surprise`` is
# signal-only by design (it carries no axis deltas either) and there is
# deliberately no reaction that maps to ``space`` — you cannot react to
# absence; ``space`` only ever moves through passive engagement.
REACTION_TO_KIND: dict[str, str] = {
    "hug": "touch",
    "laugh": "teasing",
    "eyeroll": "teasing",
    "grateful": "appreciation",
    "thumbs": "appreciation",
    "heart": "words",
    "rose": "words",
    "blush": "words",
    "moved": "words",
    # "surprise" -> intentionally unmapped (signal-only).
}


# Assistant ``[[reaction:...]]`` mood tags that read as teasing.
_TEASE_REACTIONS: frozenset[str] = frozenset(
    {
        "playful",
        "mischievous",
        "smug",
        "pouty",
        "sulky",
        "teasing",
        "amused",
    }
)


# Assistant mood tags that read as warm verbal affection ("words").
_WARM_REACTIONS: frozenset[str] = frozenset(
    {
        "warm",
        "gentle",
        "affectionate",
        "tender",
        "loving",
        "soft",
        "happy",
        "cheerful",
        "caring",
        "fond",
    }
)


# Raw ``[[touch:...]]`` tag detector (K31). The raw assistant text
# still carries the tag at the point post-turn classifies the turn.
_TOUCH_TAG_RE = re.compile(r"\[\[\s*touch\s*:", re.IGNORECASE)


# A reply shorter than this many characters reads as a low-pressure,
# "giving space" beat rather than a words-of-affirmation one.
_SHORT_REPLY_CHARS = 60


# ── State ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AffectionStyleState:
    """Persisted per-user affection weighting.

    ``weights`` maps each :data:`AFFECTION_KINDS` entry to a float share
    that sums to ~1.0 (renormalised on every mutation). ``updated_at``
    is the ISO wall-clock of the last mutation, used by the decay
    worker to compute elapsed time. A brand-new user starts uniform.
    """

    weights: dict[str, float]
    updated_at: str

    def weight_of(self, kind: str) -> float:
        """Return the share for ``kind`` (uniform fallback if missing)."""
        return float(self.weights.get(kind, _UNIFORM))


def uniform_state(now: datetime | None = None) -> AffectionStyleState:
    """Return a fresh uniform state (every kind equal)."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    return AffectionStyleState(
        weights={k: _UNIFORM for k in AFFECTION_KINDS},
        updated_at=ts,
    )


# ── Normalisation ───────────────────────────────────────────────────


def _normalise(weights: dict[str, float], *, floor: float) -> dict[str, float]:
    """Clamp each kind to ``>= floor`` then renormalise to sum 1.0.

    The floor is applied *before* renormalisation so no kind can ever
    be driven to zero ("bias, never collapse"). ``floor`` is clamped
    into ``[0, 1/len]`` so a hostile config can't make the floors sum
    past 1.0 (which would make renormalisation meaningless).
    """
    safe_floor = max(0.0, min(_UNIFORM, float(floor)))
    clamped = {
        k: max(safe_floor, float(weights.get(k, 0.0)))
        for k in AFFECTION_KINDS
    }
    total = sum(clamped.values())
    if total <= 0.0:
        return {k: _UNIFORM for k in AFFECTION_KINDS}
    return {k: v / total for k, v in clamped.items()}


# ── Signal mapping ──────────────────────────────────────────────────


def engagement_to_signal(
    label: str | None,
    length_z: float | None = None,
) -> float:
    """Map a K14 engagement outcome to a signed signal in ``[-1, 1]``.

    The label is the dominant input (it already folds latency + length
    into a band). ``length_z`` only refines the ``engaged`` /
    ``neutral`` bands slightly so a genuinely effusive reply registers
    a touch stronger than a one-word "yeah". Unknown / missing label
    returns ``0.0`` (no learning that turn).
    """
    band = (label or "").strip().lower()
    base: float
    if band == "engaged":
        base = 1.0
    elif band == "abandoned":
        base = -1.0
    elif band == "disengaged":
        base = -0.6
    else:  # neutral / unknown
        base = 0.0

    # Refine with the length z-score when present: a long warm reply
    # nudges a neutral band slightly positive, a curt one slightly
    # negative. Capped so it never flips the band's sign on its own.
    if length_z is not None and band in ("engaged", "neutral"):
        base += max(-0.3, min(0.3, float(length_z) * 0.15))
    return max(-1.0, min(1.0, base))


def classify_turn_affection(
    raw_assistant_text: str,
    reaction: str | None,
    *,
    appreciation_fired: bool = False,
    reply_chars: int | None = None,
) -> list[str]:
    """Tag which affection kind(s) a finished assistant turn carried.

    Cheap, pure, no LLM — reads only signals already available at the
    end of a turn:

    - ``touch`` — the raw reply emitted a ``[[touch:...]]`` tag (K31).
    - ``appreciation`` — the J10 appreciation cue fired this turn.
    - ``teasing`` — the ``[[reaction:...]]`` mood tag reads as teasing.
    - ``words`` — the mood tag reads as warm verbal affection, OR the
      reply is substantial and none of the above fired (a plain warm
      verbal turn).
    - ``space`` — a short, low-pressure reply with no other affection
      signal (giving room is itself a way of caring).

    A turn can carry several kinds (e.g. a hug + warm words). Returns a
    de-duplicated list preserving :data:`AFFECTION_KINDS` order. An
    empty list means "nothing attributable" — callers skip attribution
    for that turn rather than guessing.
    """
    found: set[str] = set()
    text = raw_assistant_text or ""
    react = (reaction or "").strip().lower()

    if _TOUCH_TAG_RE.search(text):
        found.add("touch")
    if appreciation_fired:
        found.add("appreciation")
    if react in _TEASE_REACTIONS:
        found.add("teasing")
    if react in _WARM_REACTIONS:
        found.add("words")

    # Length-based fallback only when no explicit affection tag fired.
    if not found:
        chars = reply_chars if reply_chars is not None else len(text.strip())
        if chars > 0:
            if chars <= _SHORT_REPLY_CHARS:
                found.add("space")
            else:
                found.add("words")

    return [k for k in AFFECTION_KINDS if k in found]


# ── Mutation ────────────────────────────────────────────────────────


def apply_observation(
    state: AffectionStyleState,
    kinds: Iterable[str],
    signal: float,
    now: datetime,
    *,
    learning_rate: float,
    floor: float,
) -> AffectionStyleState:
    """Nudge the weights for ``kinds`` by ``signal`` and renormalise.

    The signed ``signal`` (``[-1, 1]``, typically from
    :func:`engagement_to_signal`) is split evenly across the tagged
    ``kinds`` so a single turn that carried two kinds doesn't double its
    total influence. Each kind's raw share moves by
    ``learning_rate * signal / n`` before the floor + renormalise pass,
    so a positive signal lifts the tagged kinds' *share* (others shrink
    proportionally) and a negative signal does the reverse — always
    bounded by the floor.

    A zero signal or empty ``kinds`` returns the state unchanged except
    for advancing ``updated_at`` (keeps the decay baseline fresh).
    """
    kind_list = [k for k in kinds if k in AFFECTION_KINDS]
    ts = now.isoformat()
    if not kind_list or signal == 0.0 or learning_rate <= 0.0:
        return AffectionStyleState(weights=dict(state.weights), updated_at=ts)

    per = float(learning_rate) * float(signal) / float(len(kind_list))
    raw = {k: state.weight_of(k) for k in AFFECTION_KINDS}
    for k in kind_list:
        raw[k] = raw[k] + per
    return AffectionStyleState(
        weights=_normalise(raw, floor=floor),
        updated_at=ts,
    )


def apply_reaction_confirmation(
    state: AffectionStyleState,
    reaction_kind: str,
    now: datetime,
    *,
    reaction_weight: float,
    floor: float,
) -> AffectionStyleState:
    """Apply a positive confirmation vote from a K32 user reaction.

    Reactions only ever *confirm* — they map (via :data:`REACTION_TO_KIND`)
    to a single affection kind and push it up by ``reaction_weight``.
    An unmapped reaction (e.g. ``surprise``) returns the state
    unchanged. This is the sparse booster layer on top of the passive
    engagement signal; it is never required for learning to happen.
    """
    mapped = REACTION_TO_KIND.get((reaction_kind or "").strip().lower())
    if mapped is None:
        return AffectionStyleState(
            weights=dict(state.weights), updated_at=now.isoformat(),
        )
    raw = {k: state.weight_of(k) for k in AFFECTION_KINDS}
    raw[mapped] = raw[mapped] + max(0.0, float(reaction_weight))
    return AffectionStyleState(
        weights=_normalise(raw, floor=floor),
        updated_at=now.isoformat(),
    )


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
    state: AffectionStyleState,
    now: datetime,
    *,
    half_life_days: float,
    floor: float,
) -> AffectionStyleState:
    """Pull every weight a fraction of the way toward uniform.

    Exponential forgetting: over one ``half_life_days`` the gap between
    each weight and the uniform share halves. Pure math —
    ``w' = uniform + (w - uniform) * 0.5 ** (elapsed_days / half_life)``
    — then re-floored + renormalised. A non-positive half-life or
    non-positive elapsed time returns the state unchanged (timestamp
    advanced) so a disabled / freshly-written state never churns.
    """
    ts = now.isoformat()
    if half_life_days <= 0.0:
        return AffectionStyleState(weights=dict(state.weights), updated_at=ts)
    stored_at = _parse_iso(state.updated_at)
    if stored_at is None:
        return AffectionStyleState(weights=dict(state.weights), updated_at=ts)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(
        tzinfo=timezone.utc,
    )
    elapsed_days = (now_utc - stored_at).total_seconds() / 86400.0
    if elapsed_days <= 0.0:
        return AffectionStyleState(weights=dict(state.weights), updated_at=ts)

    keep = math.pow(0.5, elapsed_days / float(half_life_days))
    raw = {
        k: _UNIFORM + (state.weight_of(k) - _UNIFORM) * keep
        for k in AFFECTION_KINDS
    }
    return AffectionStyleState(weights=_normalise(raw, floor=floor), updated_at=ts)


# ── Gate bias ───────────────────────────────────────────────────────


def bias_multiplier(
    state: AffectionStyleState,
    kind: str,
    *,
    strength: float,
    floor: float = 0.6,
    ceil: float = 1.5,
) -> float:
    """Translate a learned weight into a willingness multiplier.

    Returns a multiplier centred on ``1.0`` for a kind sitting at the
    uniform share, scaling up toward ``ceil`` for an above-uniform
    (well-liked) kind and down toward ``floor`` for a below-uniform
    one. ``strength`` (0 = no effect) scales how far the relative weight
    pushes the multiplier:

        multiplier = 1 + strength * (weight / uniform - 1)

    clamped to ``[floor, ceil]``. Gates use this to *tilt* a cooldown /
    chance / cap — never to gate a channel off entirely (the clamp
    floor guarantees a minimum willingness). ``strength <= 0`` returns
    a flat ``1.0`` so the master "bias off" path is a single early
    return for callers.
    """
    if strength <= 0.0:
        return 1.0
    rel = state.weight_of(kind) / _UNIFORM  # 1.0 == uniform
    mult = 1.0 + float(strength) * (rel - 1.0)
    lo = min(floor, ceil)
    hi = max(floor, ceil)
    return max(lo, min(hi, mult))


# ── Serialise / deserialise ─────────────────────────────────────────


def serialize(state: AffectionStyleState) -> str:
    return json.dumps(
        {
            "weights": {k: float(state.weight_of(k)) for k in AFFECTION_KINDS},
            "updated_at": str(state.updated_at),
        }
    )


def deserialize(text: str | None) -> AffectionStyleState:
    """Parse stored JSON into a state. Corrupt / missing -> uniform.

    Same defensive posture as the other K-state modules: a bad kv_meta
    row must never permanently break the feature. Any missing kind is
    backfilled to the uniform share before renormalising so a state
    written before a taxonomy change still loads cleanly.
    """
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
    for k in AFFECTION_KINDS:
        try:
            weights[k] = float(raw_weights.get(k, _UNIFORM))
        except (TypeError, ValueError):
            weights[k] = _UNIFORM
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        updated_at = datetime.now(timezone.utc).isoformat()
    # Renormalise on load (no floor coercion — preserve a legitimately
    # zeroed legacy weight until the next mutation re-floors it).
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0.0:
        return uniform_state(now=_parse_iso(updated_at))
    return AffectionStyleState(
        weights={k: max(0.0, v) / total for k, v in weights.items()},
        updated_at=updated_at,
    )


def top_kind(state: AffectionStyleState) -> str:
    """Return the highest-weighted kind (ties broken by taxonomy order)."""
    return max(AFFECTION_KINDS, key=lambda k: (state.weight_of(k), -AFFECTION_KINDS.index(k)))


__all__ = [
    "AFFECTION_KINDS",
    "AffectionStyleState",
    "KV_AFFECTION_STYLE",
    "REACTION_TO_KIND",
    "apply_observation",
    "apply_reaction_confirmation",
    "bias_multiplier",
    "classify_turn_affection",
    "decay_toward_uniform",
    "deserialize",
    "engagement_to_signal",
    "serialize",
    "top_kind",
    "uniform_state",
]
