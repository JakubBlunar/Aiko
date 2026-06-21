"""Soft-physicality touch gestures (K31 personality backlog).

The new ``[[touch:KIND]]`` tag family lets Aiko reach toward the user
with a small physical gesture (hug, head pat, poke, wave, ...). The
literal motion is sold by two surfaces in parallel:

  1. **Avatar** -- the new :class:`ReachChannel` in
     ``web/src/live2d/channels/`` writes a head + body pitch lean-in
     curve on top of paired ``[[overlay:X]]`` companions (``warm``,
     ``blush``, ``smile``, ...). The Alexia rig has no real Z-depth
     param wired today; the lean-in is an *approximation* that reads
     as warmth.

  2. **Transcript** -- a small footer badge on the assistant bubble
     ("Aiko gave you a hug 🫂") in chat mode, plus a transient
     :class:`PersonaActionBanner` near the avatar in the Tauri persona
     overlay (which has no transcript).

This module is the **pure data layer**: the taxonomy, per-kind axes
gates, cooldown / daily-cap state machine, kv_meta JSON round-trip,
and the dispatch-decision verdict shape. It has no I/O beyond the
``ChatDatabase`` it persists state through, no LLM call, no
scheduler. The lifecycle wiring lives in
[`avatar_mixin.py`](../session/avatar_mixin.py) (``_emit_avatar_touch``)
and [`session_controller.py`](../session/session_controller.py)
(``TouchService`` construction).

Design choices:

- **Per-kind axes gate**. Light gestures (``wave``, ``poke``,
  ``boop``, ``nudge``) have no relationship gate; intimate gestures
  (``hug``, ``head_pat``, ``cuddle``) require closeness + trust to
  pass a small floor. ``high_five`` gates on humor. The gate uses
  the current :class:`RelationshipAxesState` snapshot at dispatch
  time -- the LLM doesn't have to know which gestures it's allowed
  to emit; if it asks for one we don't pass, we silently drop the
  tag and the avatar stays still.

- **Sibling of K15**. Cooldown + daily cap are the *physical*
  budget analogue of K15's *disclosure* budget. Both prevent the
  LLM from compulsively repeating an authentic-feeling beat until
  it stops reading as authentic. Both surface in the prompt as a
  cue, not a wall.

- **Storage on ``kv_meta``, not a new schema**. One JSON key
  ``aiko.touch_state`` carrying
  ``{last_fired: {kind: iso}, daily_counts: {kind: int},
  daily_date: "YYYY-MM-DD"}``. Same shape as K15 and K27. Daily
  counts reset whenever ``daily_date`` doesn't match today's UTC
  date -- cheap to compute, no nightly job required.

- **Per-kind overrides honoured at dispatch time**. The
  ``agent.touch_per_kind_overrides`` settings field can adjust
  ``cooldown_seconds`` and ``daily_cap`` per kind without code
  changes. Unknown override fields are ignored.

The pure module can be unit-tested in milliseconds; the controller
plumbing is exercised separately through ``tests/test_touch_dispatch.py``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.core.touch.touch_gestures")


# ── kv_meta key ─────────────────────────────────────────────────────


# Single key under the ``aiko.*`` namespace, matching K15 / K27
# conventions. Exported so the MCP debug tool and tests share the
# exact same key string.
KV_TOUCH_STATE = "aiko.touch_state"


# ── Taxonomy ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TouchGesture:
    """One entry in the touch taxonomy.

    Field meanings:

    - ``kind`` -- canonical identifier, lowercase. Stored in
      ``messages.gestures`` JSON arrays, broadcast over WS, surfaced
      to the LLM grammar.
    - ``label`` -- verbatim phrasing for the chat-bubble footer
      badge ("gave you a hug"). The full rendered line lands as
      "Aiko {label} {emoji}" -- no trailing punctuation so the
      transcript reads smoothly.
    - ``emoji`` -- the badge / banner glyph.
    - ``min_closeness`` / ``min_trust`` / ``min_humor`` -- axes
      floors. ``-1.0`` means "no gate" (axes range is ``[-1, 1]``).
    - ``cooldown_seconds`` -- minimum wall-clock seconds between
      consecutive dispatches of THIS kind. Other kinds are
      independent.
    - ``daily_cap`` -- max dispatches per UTC day. ``0`` means
      uncapped.
    - ``duration_ms`` -- intended Live2D animation duration (ramp
      + hold + release). The frontend ``ReachChannel`` reads this
      to schedule its lean-in curve.
    - ``lean_amount`` -- multiplier ``[0, 1]`` on the head + body
      pitch displacement. ``0`` means no lean (e.g. a wave is
      mostly arm-up); ``1.0`` means full lean (cuddle).
    - ``overlays`` -- the ``[[overlay:X]]`` companions to fire
      alongside the touch (``warm``, ``blush``, ``smile``,
      ``smirk``). The frontend picks these up through the same
      ``OverlayChannel`` path the LLM uses today.
    """

    kind: str
    label: str
    emoji: str
    min_closeness: float
    min_trust: float
    min_humor: float
    cooldown_seconds: int
    daily_cap: int
    duration_ms: int
    lean_amount: float
    overlays: tuple[str, ...]


# Ordered, from most casual to most intimate. The order matters only
# for diagnostic outputs (MCP debug tool, log lines); dispatch is
# always by kind lookup.
TOUCH_KINDS: tuple[str, ...] = (
    "wave",
    "poke",
    "boop",
    "nudge",
    "high_five",
    "hug",
    "head_pat",
    "cuddle",
)


# Canonical taxonomy table. The numeric tuning is the v1 default;
# `agent.touch_per_kind_overrides` lets the user adjust cooldown +
# daily cap without code changes.
_TOUCH_GESTURES: dict[str, TouchGesture] = {
    "wave": TouchGesture(
        kind="wave",
        label="waved at you",
        emoji="👋",
        min_closeness=-1.0,
        min_trust=-1.0,
        min_humor=-1.0,
        cooldown_seconds=30,
        daily_cap=0,
        duration_ms=1500,
        lean_amount=0.0,
        overlays=(),
    ),
    "poke": TouchGesture(
        kind="poke",
        label="poked you",
        emoji="👉",
        min_closeness=-1.0,
        min_trust=-1.0,
        min_humor=-1.0,
        cooldown_seconds=60,
        daily_cap=10,
        duration_ms=800,
        lean_amount=0.2,
        overlays=("smirk",),
    ),
    "boop": TouchGesture(
        kind="boop",
        label="booped your nose",
        emoji="👆",
        min_closeness=-1.0,
        min_trust=-1.0,
        min_humor=-1.0,
        cooldown_seconds=60,
        daily_cap=8,
        duration_ms=800,
        lean_amount=0.3,
        overlays=("smile",),
    ),
    "nudge": TouchGesture(
        kind="nudge",
        label="nudged you",
        emoji="🫶",
        min_closeness=-1.0,
        min_trust=-1.0,
        min_humor=-1.0,
        cooldown_seconds=60,
        daily_cap=10,
        duration_ms=1000,
        lean_amount=0.2,
        overlays=(),
    ),
    "high_five": TouchGesture(
        kind="high_five",
        label="high-fived you",
        emoji="🙏",
        min_closeness=-1.0,
        min_trust=-1.0,
        min_humor=0.3,
        cooldown_seconds=120,
        daily_cap=6,
        duration_ms=1200,
        lean_amount=0.5,
        overlays=("smile",),
    ),
    "hug": TouchGesture(
        kind="hug",
        label="gave you a hug",
        emoji="🫂",
        min_closeness=0.3,
        min_trust=0.2,
        min_humor=-1.0,
        cooldown_seconds=600,
        daily_cap=4,
        duration_ms=2500,
        lean_amount=0.8,
        overlays=("warm", "blush"),
    ),
    "head_pat": TouchGesture(
        kind="head_pat",
        label="patted your head",
        emoji="🤚",
        min_closeness=0.5,
        min_trust=-1.0,
        min_humor=-1.0,
        cooldown_seconds=600,
        daily_cap=4,
        duration_ms=2000,
        lean_amount=0.6,
        overlays=("warm",),
    ),
    "cuddle": TouchGesture(
        kind="cuddle",
        label="snuggled up to you",
        emoji="🥰",
        min_closeness=0.7,
        min_trust=0.5,
        min_humor=-1.0,
        cooldown_seconds=1800,
        daily_cap=2,
        duration_ms=3000,
        lean_amount=1.0,
        overlays=("warm", "blush"),
    ),
}


def get_gesture(kind: str) -> TouchGesture | None:
    """Look up a :class:`TouchGesture` by kind. ``None`` on unknown."""
    if not kind:
        return None
    return _TOUCH_GESTURES.get(str(kind).strip().lower())


def all_gestures() -> tuple[TouchGesture, ...]:
    """Return the taxonomy in canonical order (light → intimate)."""
    return tuple(_TOUCH_GESTURES[k] for k in TOUCH_KINDS)


# ── Open-vocabulary custom gestures (B7) ────────────────────────────


# Visual defaults for a gesture Aiko coins on the fly. The Alexia rig
# has no arbitrary-motion param, so every custom gesture animates as the
# same gentle lean-in; the novelty lives in the badge label + emoji.
DEFAULT_CUSTOM_LEAN = 0.3
DEFAULT_CUSTOM_DURATION_MS = 1500
_MAX_CUSTOM_KIND_LEN = 40
_MAX_CUSTOM_LABEL_LEN = 60
_MAX_CUSTOM_EMOJI_LEN = 8


def _humanize_kind(kind: str) -> str:
    """``fist_bump`` -> ``fist bump`` for a default badge label."""
    return kind.replace("_", " ").strip()


def synthesize_custom_gesture(
    kind: str, *, emoji: str = "", label: str = "",
) -> TouchGesture:
    """Build a :class:`TouchGesture` for an off-taxonomy kind (B7).

    Used when Aiko coins a gesture that isn't one of the curated
    built-ins. Visuals are generic (a gentle lean, no overlays); the
    model-supplied ``label`` / ``emoji`` carry the meaning. Both are
    optional -- a missing label falls back to the humanized slug, a
    missing emoji renders glyph-free. Inputs are sanitised (whitespace
    collapsed, length-clamped) so an invented "gesture" can't smuggle a
    wall of text into the transcript.
    """
    safe_kind = (kind or "").strip().lower()[:_MAX_CUSTOM_KIND_LEN]
    safe_label = " ".join((label or "").split())[:_MAX_CUSTOM_LABEL_LEN]
    if not safe_label:
        safe_label = _humanize_kind(safe_kind) or "reached out"
    safe_emoji = " ".join((emoji or "").split())[:_MAX_CUSTOM_EMOJI_LEN]
    return TouchGesture(
        kind=safe_kind,
        label=safe_label,
        emoji=safe_emoji,
        min_closeness=-1.0,
        min_trust=-1.0,
        min_humor=-1.0,
        cooldown_seconds=0,
        daily_cap=0,
        duration_ms=DEFAULT_CUSTOM_DURATION_MS,
        lean_amount=DEFAULT_CUSTOM_LEAN,
        overlays=(),
    )


# ── State ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TouchServiceState:
    """Persisted cooldown + daily-cap state.

    Stored as a single JSON blob under :data:`KV_TOUCH_STATE`.
    Immutable on purpose -- :class:`TouchService` recreates it per
    successful dispatch and writes the new shape back. ``daily_date``
    is the UTC date the daily counts were last updated for; when the
    next dispatch lands on a later date the counts are zeroed before
    the per-kind increment.
    """

    last_fired: dict[str, str]  # kind -> ISO timestamp
    daily_counts: dict[str, int]  # kind -> count for ``daily_date``
    daily_date: str  # "YYYY-MM-DD" in UTC


_EMPTY_STATE = TouchServiceState(
    last_fired={}, daily_counts={}, daily_date="",
)


def serialize_state(state: TouchServiceState) -> str:
    """JSON-encode :class:`TouchServiceState` for kv_meta storage."""
    return json.dumps(
        {
            "last_fired": dict(state.last_fired),
            "daily_counts": dict(state.daily_counts),
            "daily_date": str(state.daily_date),
        },
        sort_keys=True,
    )


def deserialize_state(text: str | None) -> TouchServiceState:
    """Decode a kv_meta JSON blob into a :class:`TouchServiceState`.

    Graceful: missing / corrupt JSON returns the empty baseline. Same
    posture as K15's :func:`deserialize` -- a bad row never
    permanently silences the feature.
    """
    if not text:
        return _EMPTY_STATE
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return _EMPTY_STATE
    if not isinstance(data, dict):
        return _EMPTY_STATE
    last_fired_raw = data.get("last_fired", {})
    last_fired: dict[str, str] = {}
    if isinstance(last_fired_raw, dict):
        for kind, ts in last_fired_raw.items():
            if isinstance(kind, str) and isinstance(ts, str) and ts.strip():
                last_fired[kind.strip().lower()] = ts.strip()
    counts_raw = data.get("daily_counts", {})
    daily_counts: dict[str, int] = {}
    if isinstance(counts_raw, dict):
        for kind, count in counts_raw.items():
            if isinstance(kind, str):
                try:
                    daily_counts[kind.strip().lower()] = max(0, int(count))
                except (TypeError, ValueError):
                    continue
    raw_date = data.get("daily_date", "")
    daily_date = str(raw_date) if isinstance(raw_date, str) else ""
    return TouchServiceState(
        last_fired=last_fired,
        daily_counts=daily_counts,
        daily_date=daily_date,
    )


# ── Dispatch verdict ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """Result of :meth:`TouchService.try_dispatch`.

    ``dispatched`` is the headline ``bool``. ``reason`` is a short
    string label so the MCP debug tool, structured logs, and tests
    can verify *why* a dispatch was rejected without re-running the
    logic. ``gesture`` is the resolved taxonomy entry when the kind
    is known (even on rejection) so callers can render a useful
    diagnostic; ``None`` only when ``kind`` is unknown.
    """

    dispatched: bool
    reason: str
    gesture: TouchGesture | None
    new_state: TouchServiceState | None


# Rejection-reason labels are exported as constants so tests don't
# depend on string spellings hidden inside the service.
REASON_OK = "ok"
REASON_UNKNOWN_KIND = "unknown_kind"
REASON_DISABLED = "disabled"
REASON_GATE_CLOSENESS = "gate_closeness"
REASON_GATE_TRUST = "gate_trust"
REASON_GATE_HUMOR = "gate_humor"
REASON_COOLDOWN = "cooldown"
REASON_DAILY_CAP = "daily_cap"


# ── Helpers ─────────────────────────────────────────────────────────


def _parse_iso(text: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse, returns ``None`` on garbage."""
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


def _today_utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _resolved_cooldown_seconds(
    gesture: TouchGesture, overrides: Mapping[str, Any] | None,
) -> int:
    """Apply per-kind override for ``cooldown_seconds``, else default."""
    if not overrides:
        return int(gesture.cooldown_seconds)
    block = overrides.get(gesture.kind)
    if not isinstance(block, Mapping):
        return int(gesture.cooldown_seconds)
    raw = block.get("cooldown_seconds")
    if raw is None:
        return int(gesture.cooldown_seconds)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return int(gesture.cooldown_seconds)


def _resolved_daily_cap(
    gesture: TouchGesture, overrides: Mapping[str, Any] | None,
) -> int:
    """Apply per-kind override for ``daily_cap``, else default."""
    if not overrides:
        return int(gesture.daily_cap)
    block = overrides.get(gesture.kind)
    if not isinstance(block, Mapping):
        return int(gesture.daily_cap)
    raw = block.get("daily_cap")
    if raw is None:
        return int(gesture.daily_cap)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return int(gesture.daily_cap)


# ── Service ─────────────────────────────────────────────────────────


class TouchService:
    """In-process state machine for K31 touch dispatches.

    The service does three things on every :meth:`try_dispatch` call:

    1. **Validate** the kind against :data:`_TOUCH_GESTURES`.
    2. **Gate** on the relationship axes snapshot (closeness, trust,
       humor) per the gesture's per-kind floors.
    3. **Pace** via cooldown + daily cap, persisting the post-dispatch
       state to ``kv_meta``.

    State lives in a single JSON blob under :data:`KV_TOUCH_STATE`
    so the cooldowns survive restart. The blob is small (≤ 1 KB
    even with all 8 kinds populated); we re-read on every dispatch
    rather than holding an in-memory copy, which keeps the service
    stateless across threads and matches the K15 / K27 pattern.
    """

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        settings: Any | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._settings = settings

    # -- read / write ----------------------------------------------------

    def _load_state(self) -> TouchServiceState:
        try:
            raw = self._chat_db.kv_get(KV_TOUCH_STATE)
        except Exception:
            log.debug("touch_state kv_get failed", exc_info=True)
            return _EMPTY_STATE
        return deserialize_state(raw)

    def _save_state(self, state: TouchServiceState) -> None:
        try:
            self._chat_db.kv_set(KV_TOUCH_STATE, serialize_state(state))
        except Exception:
            log.debug("touch_state kv_set failed", exc_info=True)

    # -- public API ------------------------------------------------------

    def get_state_snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the persisted state.

        Used by the MCP debug tool ``get_touch_state()``. Includes
        the resolved gesture taxonomy alongside the live state so
        the debug surface is self-describing.
        """
        state = self._load_state()
        return {
            "kv_key": KV_TOUCH_STATE,
            "daily_date": state.daily_date,
            "last_fired": dict(state.last_fired),
            "daily_counts": dict(state.daily_counts),
            "kinds": [
                {
                    "kind": g.kind,
                    "label": g.label,
                    "emoji": g.emoji,
                    "min_closeness": g.min_closeness,
                    "min_trust": g.min_trust,
                    "min_humor": g.min_humor,
                    "cooldown_seconds": g.cooldown_seconds,
                    "daily_cap": g.daily_cap,
                    "duration_ms": g.duration_ms,
                    "lean_amount": g.lean_amount,
                    "overlays": list(g.overlays),
                }
                for g in all_gestures()
            ],
        }

    def reset(self) -> None:
        """Clear all cooldowns + daily counts (MCP debug helper)."""
        try:
            self._chat_db.kv_delete(KV_TOUCH_STATE)
        except Exception:
            log.debug("touch_state kv_delete failed", exc_info=True)

    def try_dispatch(
        self,
        kind: str,
        *,
        axes: Any | None = None,
        now: datetime | None = None,
        bypass_gates: bool = False,
        emoji: str = "",
        label: str = "",
    ) -> DispatchReport:
        """Resolve ``kind`` to a gesture and dispatch it.

        B7 removed all gating (relationship-axes floors, per-kind
        cooldowns, daily caps) and the ``kv_meta`` state machine. Every
        emitted ``[[touch:...]]`` dispatches; Aiko self-paces through the
        persona guidance ("at most once a turn, only when it's earned").
        The ``axes`` / ``now`` / ``bypass_gates`` parameters are retained
        for call-site compatibility but no longer affect the verdict.

        Built-in kinds resolve to their curated taxonomy entry. An
        off-taxonomy ``kind`` is synthesized via
        :func:`synthesize_custom_gesture` using the model-supplied
        ``emoji`` / ``label`` (B7 open vocabulary). The only thing that
        can still reject a dispatch is the ``touch_enabled`` master flag.
        """
        normalized = (kind or "").strip().lower()
        if not normalized:
            return DispatchReport(
                dispatched=False,
                reason=REASON_UNKNOWN_KIND,
                gesture=None,
                new_state=None,
            )

        gesture = get_gesture(normalized)
        if gesture is None:
            # B7: off-taxonomy kind -> generic custom gesture carrying
            # the model-supplied label / emoji.
            gesture = synthesize_custom_gesture(
                normalized, emoji=emoji, label=label,
            )

        if self._settings is not None and not bool(
            getattr(self._settings, "touch_enabled", True),
        ):
            return DispatchReport(
                dispatched=False,
                reason=REASON_DISABLED,
                gesture=gesture,
                new_state=None,
            )

        return DispatchReport(
            dispatched=True,
            reason=REASON_OK,
            gesture=gesture,
            new_state=None,
        )

    # B7: axes / cooldown / daily-cap gating and the low-budget inner-life
    # cue (``render_touch_state_block``) were retired -- Aiko self-paces
    # via the persona guidance, so the prompt carries no budget block and
    # the dispatch path never rejects on pacing.


__all__ = [
    "DispatchReport",
    "KV_TOUCH_STATE",
    "REASON_COOLDOWN",
    "REASON_DAILY_CAP",
    "REASON_DISABLED",
    "REASON_GATE_CLOSENESS",
    "REASON_GATE_HUMOR",
    "REASON_GATE_TRUST",
    "REASON_OK",
    "REASON_UNKNOWN_KIND",
    "TOUCH_KINDS",
    "TouchGesture",
    "TouchService",
    "TouchServiceState",
    "all_gestures",
    "deserialize_state",
    "get_gesture",
    "serialize_state",
    "synthesize_custom_gesture",
]
