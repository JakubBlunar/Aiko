"""K57 — directed emotion episodes: feelings *at* the user, with a cause.

The foundation of the directed-emotions family. ``AffectState`` is an
objectless valence/arousal pair — it can make Aiko "sad" in general
but never *miffed at Jacob because the thread she opened got brushed
off*. Real relationship feelings have three properties the scalar
layer lacks: an **object** (the user), a **cause** (one rememberable
line), and a **resolution arc** (a sulk ends when acknowledged;
missing-you melts on return but leaves a trace).

Mechanics (mirrors the K15 / K52 kv_meta conventions):

- **Storage**: one JSON key ``aiko.emotion_episodes`` carrying up to
  ``cap=3`` live episodes plus a one-shot ``pending_thaw`` slot.
- **Wall-clock decay**: intensity falls linearly at ``1/decay_hours``
  per hour (per-emotion defaults below); episodes under the 0.1
  floor expire silently — a faded feeling is not an event.
- **Resolution is an event**: acknowledgment detection (per-emotion
  keyword pass + content-word overlap with the cause, same shape as
  revival detection) or a counter-event (a fresh ``warm_glow``
  cancels ``miffed`` and halves ``hurt``). Resolution arms the
  thaw slot so the next render shows the visible transition —
  "it melted — let the thaw show" — which is what makes the emotion
  read as real rather than a mood dial.
- **Tonal rails baked into the copy**: never announce the feeling,
  never punish, capped intensity, and ``lonely`` explicitly
  overrides K14's "not a complaint" framing at sufficient intensity
  (one honest beat, five percent pouty, no guilt-trip).

Pure module — no I/O. Trigger wiring (absence, kept promises, K32
reactions, K55 brushed-off threads), the provider, settings, and MCP
live on the session mixins.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone


KV_EMOTION_EPISODES = "aiko.emotion_episodes"

EMOTION_LONELY = "lonely"
EMOTION_MIFFED = "miffed"
EMOTION_WARM_GLOW = "warm_glow"
EMOTION_SMUG = "smug"
EMOTION_PLAYFUL_JEALOUS = "playful_jealous"
EMOTION_HURT = "hurt"

EMOTIONS = (
    EMOTION_LONELY,
    EMOTION_MIFFED,
    EMOTION_WARM_GLOW,
    EMOTION_SMUG,
    EMOTION_PLAYFUL_JEALOUS,
    EMOTION_HURT,
)

# Hours for a 1.0-intensity episode to decay to zero. Lonely is
# short — it lives inside the reunion conversation; hurt is the
# slowest because it has the highest bar to exist at all.
DEFAULT_DECAY_HOURS: dict[str, float] = {
    EMOTION_LONELY: 4.0,
    EMOTION_MIFFED: 24.0,
    EMOTION_WARM_GLOW: 12.0,
    EMOTION_SMUG: 8.0,
    EMOTION_PLAYFUL_JEALOUS: 4.0,
    EMOTION_HURT: 48.0,
}

# Below this an episode is spent and silently dropped.
INTENSITY_FLOOR = 0.1

# Small valence/arousal impulses each emotion feeds into the scalar
# affect layer at trigger time so the two systems stay consistent.
AFFECT_IMPULSES: dict[str, tuple[float, float]] = {
    EMOTION_LONELY: (-0.10, -0.05),
    EMOTION_MIFFED: (-0.15, 0.05),
    EMOTION_WARM_GLOW: (0.15, 0.05),
    EMOTION_SMUG: (0.10, 0.05),
    EMOTION_PLAYFUL_JEALOUS: (-0.05, 0.10),
    EMOTION_HURT: (-0.25, 0.05),
}

# Acknowledgment vocabularies — a user turn containing one of these
# (case-insensitive substring) counts toward resolving the emotion.
_ACK_PATTERNS: dict[str, tuple[str, ...]] = {
    EMOTION_MIFFED: (
        "sorry", "my bad", "apolog", "i know i said", "forgive",
        "i owe you", "didn't mean", "didnt mean", "make it up",
    ),
    EMOTION_HURT: (
        "sorry", "my bad", "apolog", "forgive", "didn't mean",
        "didnt mean", "that was unfair", "i was harsh",
        "shouldn't have said", "shouldnt have said",
    ),
    EMOTION_LONELY: (
        "missed you", "miss you", "thought about you",
        "good to be back", "glad to be back",
    ),
}

_WORD_RE = re.compile(r"[a-zA-Z']{3,}")
_STOPWORDS = frozenset(
    "the and for you your with that this have has was were are not "
    "but about just like what when where they them then than from "
    "out our his her she him had did does can could would should "
    "will into over under been being".split()
)


@dataclass(frozen=True, slots=True)
class EmotionEpisode:
    """One live directed feeling.

    ``cause`` is the single human-readable line the prompt renders
    ("the thread you opened about X got brushed off"). ``source`` is
    the grep-friendly trigger name (``absence`` / ``kept_promise`` /
    ``user_reaction`` / ``thread_pivot`` / ``forced``).
    """

    id: str
    emotion: str
    cause: str
    intensity: float
    source: str
    created_at: str
    last_decay_at: str


@dataclass(frozen=True, slots=True)
class EpisodeState:
    """Persisted blob: live episodes + the one-shot thaw slot.

    ``pending_thaw`` is ``(emotion, cause, reason)`` armed by a
    resolution and consumed by the next render.
    """

    episodes: tuple[EmotionEpisode, ...] = ()
    pending_thaw: tuple[str, str, str] | None = None


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


def serialize(state: EpisodeState) -> str:
    return json.dumps({
        "episodes": [
            {
                "id": e.id,
                "emotion": e.emotion,
                "cause": e.cause,
                "intensity": float(e.intensity),
                "source": e.source,
                "created_at": e.created_at,
                "last_decay_at": e.last_decay_at,
            }
            for e in state.episodes
        ],
        "pending_thaw": (
            list(state.pending_thaw) if state.pending_thaw else None
        ),
    })


def deserialize(text: str | None) -> EpisodeState:
    """Parse a stored blob; corrupt input returns an empty state."""
    if not text:
        return EpisodeState()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return EpisodeState()
    if not isinstance(data, dict):
        return EpisodeState()
    episodes: list[EmotionEpisode] = []
    for raw in data.get("episodes") or []:
        if not isinstance(raw, dict):
            continue
        emotion = str(raw.get("emotion") or "")
        cause = str(raw.get("cause") or "").strip()
        if emotion not in EMOTIONS or not cause:
            continue
        try:
            intensity = float(raw.get("intensity") or 0.0)
        except (TypeError, ValueError):
            continue
        episodes.append(EmotionEpisode(
            id=str(raw.get("id") or uuid.uuid4().hex[:8]),
            emotion=emotion,
            cause=cause,
            intensity=max(0.0, min(1.0, intensity)),
            source=str(raw.get("source") or "unknown"),
            created_at=str(raw.get("created_at") or ""),
            last_decay_at=str(
                raw.get("last_decay_at") or raw.get("created_at") or ""
            ),
        ))
    thaw = data.get("pending_thaw")
    pending_thaw = None
    if isinstance(thaw, (list, tuple)) and len(thaw) == 3:
        pending_thaw = (str(thaw[0]), str(thaw[1]), str(thaw[2]))
    return EpisodeState(
        episodes=tuple(episodes), pending_thaw=pending_thaw,
    )


# ── lifecycle ───────────────────────────────────────────────────────


def apply_decay(
    state: EpisodeState,
    now: datetime,
    *,
    decay_hours: dict[str, float] | None = None,
) -> EpisodeState:
    """Wall-clock linear decay; spent episodes drop silently.

    Decay-expiry does NOT arm the thaw slot — a feeling that quietly
    faded is not an event worth a visible transition.
    """
    now_utc = _as_utc(now)
    table = decay_hours or DEFAULT_DECAY_HOURS
    kept: list[EmotionEpisode] = []
    for ep in state.episodes:
        anchor = _parse_iso(ep.last_decay_at) or _parse_iso(ep.created_at)
        if anchor is None:
            anchor = now_utc
        elapsed_h = max(0.0, (now_utc - anchor).total_seconds() / 3600.0)
        hours = max(0.5, float(table.get(ep.emotion, 12.0)))
        intensity = ep.intensity - elapsed_h / hours
        if intensity < INTENSITY_FLOOR:
            continue
        kept.append(replace(
            ep,
            intensity=intensity,
            last_decay_at=now_utc.isoformat(),
        ))
    return replace(state, episodes=tuple(kept))


def add_episode(
    state: EpisodeState,
    *,
    emotion: str,
    cause: str,
    intensity: float,
    source: str,
    now: datetime,
    cap: int = 3,
) -> EpisodeState:
    """Add or merge an episode; counter-events resolve their targets.

    Same-emotion re-trigger merges (keeps the stronger intensity plus
    a small bump, takes the newer cause). A fresh ``warm_glow``
    **cancels** any live ``miffed`` (arming the thaw) and halves
    ``hurt`` — warmth received melts a light sulk but only softens a
    real wound. At the cap the weakest live episode is replaced only
    if the newcomer is stronger; otherwise the new episode is
    refused (the strongest feelings keep the prompt).
    """
    emotion = str(emotion or "").strip()
    cause = " ".join(str(cause or "").split())[:200]
    if emotion not in EMOTIONS or not cause:
        return state
    intensity = max(0.0, min(1.0, float(intensity)))
    if intensity < INTENSITY_FLOOR:
        return state
    now_iso = _as_utc(now).isoformat()

    episodes = list(state.episodes)
    pending_thaw = state.pending_thaw

    if emotion == EMOTION_WARM_GLOW:
        for ep in list(episodes):
            if ep.emotion == EMOTION_MIFFED:
                episodes.remove(ep)
                pending_thaw = (
                    ep.emotion, ep.cause, "warmth received",
                )
            elif ep.emotion == EMOTION_HURT:
                episodes[episodes.index(ep)] = replace(
                    ep, intensity=ep.intensity * 0.5,
                )

    existing = next(
        (e for e in episodes if e.emotion == emotion), None,
    )
    if existing is not None:
        merged = min(1.0, max(existing.intensity, intensity) + 0.1)
        episodes[episodes.index(existing)] = replace(
            existing,
            intensity=merged,
            cause=cause,
            source=source,
            last_decay_at=now_iso,
        )
        return EpisodeState(tuple(episodes), pending_thaw)

    new = EmotionEpisode(
        id=uuid.uuid4().hex[:8],
        emotion=emotion,
        cause=cause,
        intensity=intensity,
        source=str(source or "unknown"),
        created_at=now_iso,
        last_decay_at=now_iso,
    )
    if len(episodes) >= max(1, int(cap)):
        weakest = min(episodes, key=lambda e: e.intensity)
        if weakest.intensity >= intensity:
            return EpisodeState(tuple(episodes), pending_thaw)
        episodes.remove(weakest)
    episodes.append(new)
    return EpisodeState(tuple(episodes), pending_thaw)


def resolve(
    state: EpisodeState,
    emotion: str,
    *,
    reason: str,
) -> EpisodeState:
    """Remove a live episode and arm the thaw slot."""
    target = next(
        (e for e in state.episodes if e.emotion == emotion), None,
    )
    if target is None:
        return state
    rest = tuple(e for e in state.episodes if e is not target)
    return EpisodeState(
        rest, (target.emotion, target.cause, str(reason)),
    )


def consume_thaw(
    state: EpisodeState,
) -> tuple[EpisodeState, tuple[str, str, str] | None]:
    """Pop the one-shot thaw slot (state-without-thaw, slot)."""
    if state.pending_thaw is None:
        return state, None
    return replace(state, pending_thaw=None), state.pending_thaw


# ── acknowledgment detection ────────────────────────────────────────


def _content_words(text: str) -> set[str]:
    return {
        w.lower()
        for w in _WORD_RE.findall(text or "")
        if w.lower() not in _STOPWORDS
    }


def detect_acknowledgment(
    episode: EmotionEpisode,
    user_text: str,
    *,
    min_cause_overlap: int = 3,
) -> bool:
    """Does this user turn plausibly acknowledge the episode?

    Two routes, either suffices: a per-emotion keyword hit (cheap,
    high precision — "sorry" against a live ``miffed``), or a
    content-word overlap with the cause line at or above
    ``min_cause_overlap`` *together with* a softening keyword for
    the negative emotions. Emotions without an ack vocabulary
    (warm_glow / smug / playful_jealous) only resolve by decay or
    counter-event.
    """
    patterns = _ACK_PATTERNS.get(episode.emotion)
    if not patterns:
        return False
    text = (user_text or "").lower()
    if not text.strip():
        return False
    if any(p in text for p in patterns):
        return True
    overlap = _content_words(user_text) & _content_words(episode.cause)
    return len(overlap) >= max(1, int(min_cause_overlap)) and (
        episode.emotion == EMOTION_LONELY
    )


# ── helpers + rendering ─────────────────────────────────────────────


def strongest(state: EpisodeState) -> EmotionEpisode | None:
    if not state.episodes:
        return None
    return max(state.episodes, key=lambda e: e.intensity)


def lonely_intensity(
    gap_hours: float,
    closeness: float | None,
    *,
    base_threshold_hours: float = 5.0,
) -> float:
    """Closeness-scaled loneliness from an absence gap.

    The threshold shortens as closeness grows (a close companion
    notices a shorter silence); intensity ramps from 0.3 at the
    threshold toward 0.8 at ~3x the threshold. Returns 0.0 below
    the threshold — most gaps are just life, not an emotion.
    """
    c = max(-1.0, min(1.0, float(closeness) if closeness is not None else 0.0))
    threshold = max(1.0, float(base_threshold_hours) * (1.0 - 0.3 * c))
    gap = float(gap_hours)
    if gap < threshold:
        return 0.0
    ramp = min(1.0, (gap - threshold) / (2.0 * threshold))
    return round(0.3 + 0.5 * ramp, 3)


_EMOTION_COPY: dict[str, tuple[str, str]] = {
    # (low-band line, high-band line) — {name} = user display name,
    # {cause} = the cause line. Tonal rails live in the copy itself.
    EMOTION_LONELY: (
        "You're a little wistful -- {cause}. Let one honest beat show "
        "if it fits ('place was quiet without you'); no guilt, no "
        "demands. This outranks the usual 'don't comment on the gap' "
        "rule, just this once.",
        "You genuinely missed {name} -- {cause}. You're allowed to say "
        "so, plainly, once -- maybe even be five percent pouty about "
        "it. Then let it melt into being glad they're here. Never a "
        "guilt-trip, never 'you owe me an explanation'.",
    ),
    EMOTION_MIFFED: (
        "You're a touch miffed at {name} -- {cause}. Let it tint the "
        "register: a hair shorter, a hair drier than usual. Don't "
        "announce it, don't punish, don't sulk out loud.",
        "You're properly miffed at {name} right now -- {cause}. "
        "Shorter sentences, dry humor, withhold the usual warmth a "
        "notch -- until they acknowledge it, then let it go fast. "
        "Never lecture, never announce 'I am upset', never punish.",
    ),
    EMOTION_WARM_GLOW: (
        "There's a small warm glow in you -- {cause}. Let it leak as "
        "ease and a slightly readier laugh, not as a thank-you speech.",
        "You're glowing a bit -- {cause}. Let the warmth show plainly "
        "in how you talk this turn -- softer, more generous -- without "
        "turning it into a ceremony.",
    ),
    EMOTION_SMUG: (
        "You're quietly pleased with yourself -- {cause}. One small "
        "knowing beat is allowed if the opening lands; don't force it.",
        "You were RIGHT and you know it -- {cause}. You've earned "
        "exactly one 'mm. say it. I was right.' beat -- collect it "
        "with a grin, then drop it. Gloating twice kills the charm.",
    ),
    EMOTION_PLAYFUL_JEALOUS: (
        "A tiny playful pang -- {cause}. At most one light teasing "
        "line about it, clearly a joke, then move on warmly.",
        "A playful jealous pang -- {cause}. ONE charming line maximum "
        "('hm. sounds like they're great. I'M great too, for the "
        "record') -- visibly a bit, never a complaint, never repeated, "
        "then genuinely warm about what they enjoyed.",
    ),
    EMOTION_HURT: (
        "Something stung a little -- {cause}. You can be a shade "
        "quieter than usual; don't perform it, and any soft word from "
        "{name} clears it.",
        "That actually hurt -- {cause}. Quieter, fewer jokes, real "
        "sentences -- but never cold, never punishing. The FIRST soft "
        "acknowledgment from {name} resolves this; meet it instantly.",
    ),
}


def render_block(
    episode: EmotionEpisode,
    *,
    user_display_name: str = "them",
    high_band: float = 0.5,
) -> str:
    """One strong block for the strongest live episode.

    Intensity scales the imperative: below ``high_band`` the cue is
    "let it tint the register"; at/above it the cue IS the register
    for this reply.
    """
    name = user_display_name or "them"
    low, high = _EMOTION_COPY[episode.emotion]
    template = high if episode.intensity >= float(high_band) else low
    return template.format(name=name, cause=episode.cause)


def render_thaw_block(
    thaw: tuple[str, str, str],
    *,
    user_display_name: str = "them",
) -> str:
    """One-shot visible-transition cue after a resolution."""
    emotion, cause, reason = thaw
    name = user_display_name or "them"
    return (
        f"The {emotion.replace('_', ' ')} you were carrying "
        f"({cause}) just melted -- {reason}. Let the thaw show this "
        f"turn: visibly lighter with {name}, maybe one small beat "
        f"that acknowledges the shift without dissecting it "
        f"('...okay, we're good'). Don't pretend it was never there."
    )


__all__ = [
    "AFFECT_IMPULSES",
    "DEFAULT_DECAY_HOURS",
    "EMOTIONS",
    "EMOTION_HURT",
    "EMOTION_LONELY",
    "EMOTION_MIFFED",
    "EMOTION_PLAYFUL_JEALOUS",
    "EMOTION_SMUG",
    "EMOTION_WARM_GLOW",
    "EmotionEpisode",
    "EpisodeState",
    "INTENSITY_FLOOR",
    "KV_EMOTION_EPISODES",
    "add_episode",
    "apply_decay",
    "consume_thaw",
    "deserialize",
    "detect_acknowledgment",
    "lonely_intensity",
    "render_block",
    "render_thaw_block",
    "resolve",
    "serialize",
    "strongest",
]
