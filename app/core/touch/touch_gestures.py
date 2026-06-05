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
        axes: Any | None,
        now: datetime,
        bypass_gates: bool = False,
    ) -> DispatchReport:
        """Decide whether to fire ``kind``; persist the new state on yes.

        ``axes`` is the current :class:`RelationshipAxesState` snapshot
        (or ``None`` if the relationship-axes subsystem isn't wired,
        e.g. in unit tests). When ``None``, all gates are treated as
        passing -- callers that wire the service through the controller
        always have an axes snapshot, so ``None`` is a test convenience.

        ``bypass_gates=True`` skips ALL gates (axes + cooldown + daily
        cap) and force-fires the gesture. Used by the MCP debug tool
        ``send_touch(kind)`` so a developer can exercise the avatar
        path without first nudging axes / waiting cooldown.

        On success: returns ``DispatchReport(dispatched=True, ...)``
        AND writes the updated :class:`TouchServiceState` back to
        ``kv_meta``. On rejection: returns the verdict WITHOUT
        touching ``kv_meta``.
        """
        gesture = get_gesture(kind)
        if gesture is None:
            return DispatchReport(
                dispatched=False,
                reason=REASON_UNKNOWN_KIND,
                gesture=None,
                new_state=None,
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

        state = self._load_state()
        today = _today_utc(now)

        if not bypass_gates:
            verdict = self._check_gates(gesture, axes, state, now, today)
            if verdict is not None:
                return DispatchReport(
                    dispatched=False,
                    reason=verdict,
                    gesture=gesture,
                    new_state=None,
                )

        # Roll daily counts forward if the date moved on.
        new_counts = (
            dict(state.daily_counts)
            if state.daily_date == today
            else {}
        )
        new_counts[gesture.kind] = new_counts.get(gesture.kind, 0) + 1

        new_last = dict(state.last_fired)
        new_last[gesture.kind] = now.astimezone(timezone.utc).isoformat()

        new_state = TouchServiceState(
            last_fired=new_last,
            daily_counts=new_counts,
            daily_date=today,
        )
        self._save_state(new_state)
        return DispatchReport(
            dispatched=True,
            reason=REASON_OK,
            gesture=gesture,
            new_state=new_state,
        )

    # -- gate evaluation -------------------------------------------------

    def _check_gates(
        self,
        gesture: TouchGesture,
        axes: Any | None,
        state: TouchServiceState,
        now: datetime,
        today: str,
    ) -> str | None:
        """Return rejection reason or ``None`` if all gates pass."""
        # Axes gates -- only check when axes are wired AND the kind
        # carries a non-trivial floor. ``-1.0`` is the sentinel for
        # "no gate" so the comparison is uniformly written.
        if axes is not None:
            closeness = float(getattr(axes, "closeness", 0.0))
            trust = float(getattr(axes, "trust", 0.0))
            humor = float(getattr(axes, "humor", 0.0))
            if gesture.min_closeness > -1.0 and closeness < gesture.min_closeness:
                return REASON_GATE_CLOSENESS
            if gesture.min_trust > -1.0 and trust < gesture.min_trust:
                return REASON_GATE_TRUST
            if gesture.min_humor > -1.0 and humor < gesture.min_humor:
                return REASON_GATE_HUMOR

        overrides = (
            getattr(self._settings, "touch_per_kind_overrides", None)
            if self._settings is not None
            else None
        )

        # Cooldown gate.
        cooldown = _resolved_cooldown_seconds(gesture, overrides)
        if cooldown > 0:
            last_fired_iso = state.last_fired.get(gesture.kind)
            last_fired = _parse_iso(last_fired_iso)
            if last_fired is not None:
                now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
                elapsed = (now_utc - last_fired).total_seconds()
                if elapsed < cooldown:
                    return REASON_COOLDOWN

        # Daily-cap gate. A daily_cap of 0 means "uncapped".
        daily_cap = _resolved_daily_cap(gesture, overrides)
        if daily_cap > 0:
            current = (
                state.daily_counts.get(gesture.kind, 0)
                if state.daily_date == today
                else 0
            )
            if current >= daily_cap:
                return REASON_DAILY_CAP

        return None


# ── Inner-life cue (low physical budget) ────────────────────────────


def render_touch_state_block(
    state: TouchServiceState,
    *,
    now: datetime,
    user_display_name: str = "them",
) -> str:
    """Render a short prompt cue when the *physical* budget is low.

    Mirrors :func:`vulnerability_budget.render_inner_life_block` but
    looks at touch usage rather than disclosure depth. The cue stays
    silent unless Aiko has actually been touchy today -- the feature
    is "stop her from spamming hugs", not "advertise the budget".

    Heuristic:

    - Today's combined intimate-gesture count (hug + head_pat +
      cuddle) >= 3 -> "you've been pretty physical with X today,
      let some space land".
    - Today's combined cap-hit count (any kind that already maxed
      its daily cap) >= 1 -> "you've maxed out something today,
      hold off on that beat for now".

    Returns ``""`` when none of the above; the feature is silent on
    the common case so the prompt cue budget isn't wasted.
    """
    today = _today_utc(now)
    name = user_display_name or "them"
    if state.daily_date != today:
        return ""

    intimate_kinds = ("hug", "head_pat", "cuddle")
    intimate_total = sum(
        int(state.daily_counts.get(k, 0)) for k in intimate_kinds
    )
    if intimate_total >= 3:
        return (
            f"You've been pretty physical with {name} today -- let some "
            f"space land before the next hug or cuddle."
        )

    capped: list[str] = []
    for kind, count in state.daily_counts.items():
        gesture = get_gesture(kind)
        if gesture is None or gesture.daily_cap <= 0:
            continue
        if count >= gesture.daily_cap:
            capped.append(kind)
    if capped:
        return (
            f"You've maxed out {'/'.join(sorted(capped))} with {name} "
            f"today -- pick a lighter beat for now."
        )
    return ""


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
    "render_touch_state_block",
    "serialize_state",
]
