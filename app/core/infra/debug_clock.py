"""DT1 -- the virtual clock that lets us time-travel the running app.

A large share of Aiko's behaviour is time-gated: memory decay and tier
promotion, concept confidence decay, anniversaries and milestones, the
cooldown on nearly every inner-life cue, gap-return and reconnection.
Verifying any of it in a live instance used to mean **waiting real hours
or days**. This module turns that into one MCP call.

**How it works.** :class:`DebugClock` holds a single in-memory
``timedelta`` offset and installs itself as
:func:`app.core.infra.timephrase.set_now_provider`. Every read that goes
through the ``timephrase`` seam -- which, after the DT1 migration, is
the ~60 per-module ``_utcnow()`` / ``_now_iso()`` helpers, the idle
scheduler's readiness clock, and the injectable ``clock=`` default on
~20 workers -- shifts together.

**Offset, not absolute.** Time keeps *flowing* while shifted, so the
un-virtualised monotonic paths (LLM latency, audio timing, HTTP
timeouts) stay coherent with the shifted narrative time rather than
seeing a frozen clock.

**Two independent levers.** Wall-clock and *engaged* time are different
domains here, and the second one is the one that matters most:

- :meth:`advance` shifts wall-clock ``now``, which drives anniversaries,
  cooldowns, promotion age and candidate TTLs. In-memory only, so a
  restart always returns to real time.
- :meth:`advance_engaged` credits the
  :class:`~app.core.infra.engagement_clock.EngagementClock`, which is
  what concept and memory *decay* actually run on. Shifting the wall
  clock does nothing to them. This one is a **real write to persisted
  state**, undone only via :meth:`reset`.

**Deliberately not virtualised:** every ``time.monotonic()`` /
``time.time()`` reader -- turn-loop latency, TTS/STT timing, the brain
queue, HTTP and orchestrator deadlines, worker perf metrics, the
scheduler's own tick budget -- plus log and crash timestamps. The rule
is "``datetime`` narrative time moves, monotonic runtime timing does
not", which happens to match the must-not-virtualise list almost
exactly.

Gated behind the ``AIKO_DEBUG_CLOCK`` environment variable (see
:func:`debug_clock_enabled`) and never persisted, so a forgotten offset
cannot survive a restart or leak into a normal run. Advancing it while
pointed at a real database *will* leave rows stamped at the virtual
time; run against a copy.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.engagement_clock import EngagementClock

log = logging.getLogger("app.debug_clock")

# Truthy spellings accepted for the env gate.
_TRUTHY = {"1", "true", "yes", "on"}

ENV_FLAG = "AIKO_DEBUG_CLOCK"


def debug_clock_enabled(env: "dict[str, str] | None" = None) -> bool:
    """Whether the DT1 debug clock may be installed at all.

    Env-gated rather than a settings knob on purpose: this is a debug
    footgun that mutates persisted timestamps, so it should cost a
    deliberate restart to arm and must never be able to persist itself
    into ``user.json`` in the on state.
    """
    source = os.environ if env is None else env
    return str(source.get(ENV_FLAG, "")).strip().lower() in _TRUTHY


def _humanize(delta: timedelta) -> str:
    """Compact signed rendering of an offset ("+3d 4h", "real time")."""
    total = delta.total_seconds()
    if total == 0:
        return "real time"
    sign = "+" if total > 0 else "-"
    total = abs(total)
    days, rem = divmod(int(total), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return sign + " ".join(parts or ["0m"])


class DebugClock:
    """Process-wide virtual clock, installed into the ``timephrase`` seam.

    Construct one per process. A disabled instance is inert: it never
    installs a provider and every mutator refuses, so the object can be
    created unconditionally and the callers stay branch-free.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        engagement_clock: "EngagementClock | None" = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._engagement = engagement_clock
        self._offset = timedelta(0)
        self._installed = False

    # ── identity ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def offset(self) -> timedelta:
        return self._offset

    @property
    def active(self) -> bool:
        """True when the app's "now" is currently shifted off real time."""
        return self._enabled and self._offset != timedelta(0)

    # ── the provider ──────────────────────────────────────────────────

    def now(self) -> datetime:
        """Real wall clock plus the current offset (local-aware, like the
        default provider it replaces)."""
        return timephrase.real_now() + self._offset

    def install(self) -> bool:
        """Route the ``timephrase`` seam through this clock.

        No-op (returning ``False``) when disabled, so the real clock is
        never displaced in a normal run.
        """
        if not self._enabled or self._installed:
            return False
        timephrase.set_now_provider(self.now)
        self._installed = True
        log.warning(
            "DT1 debug clock installed (%s=1). Time-gated behaviour can now "
            "be advanced at runtime; timestamps written while advanced will "
            "persist. Use a database copy.",
            ENV_FLAG,
        )
        return True

    def uninstall(self) -> None:
        """Restore the real wall clock and drop the offset."""
        self._offset = timedelta(0)
        if self._installed:
            timephrase.set_now_provider(None)
            self._installed = False

    # ── mutators ──────────────────────────────────────────────────────

    def advance(self, *, days: float = 0.0, hours: float = 0.0) -> dict[str, Any]:
        """Shift ``now`` forward (or back, with negatives) by the delta."""
        if not self._enabled:
            return self._disabled()
        self._offset += timedelta(days=float(days), hours=float(hours))
        log.warning(
            "DT1 clock advanced by %+.2fd %+.2fh -> offset %s (now %s)",
            float(days), float(hours), _humanize(self._offset),
            self.now().isoformat(),
        )
        return self.status()

    def set_to(self, when: str) -> dict[str, Any]:
        """Jump to an absolute ISO-8601 instant, by deriving the offset."""
        if not self._enabled:
            return self._disabled()
        target = timephrase.parse_iso(when)
        if target is None:
            return {"ok": False, "error": f"unparseable ISO-8601 datetime: {when!r}"}
        self._offset = target - timephrase.real_now()
        log.warning(
            "DT1 clock set to %s -> offset %s",
            target.isoformat(), _humanize(self._offset),
        )
        return self.status()

    def advance_engaged(self, days: float) -> dict[str, Any]:
        """Credit synthetic *engaged* days -- the decay domain.

        Separate from :meth:`advance` because conflating them would be
        wrong: the whole point of the engagement clock is that a week
        away is *not* a week of engaged time. Persisted, and undone by
        :meth:`reset`.
        """
        if not self._enabled:
            return self._disabled()
        if self._engagement is None:
            return {"ok": False, "error": "no engagement clock on this session"}
        moved = self._engagement.debug_advance(float(days))
        out = self.status()
        out["engagement_delta"] = moved
        return out

    def reset(self) -> dict[str, Any]:
        """Return to real time and undo any credited engagement."""
        if not self._enabled:
            return self._disabled()
        self._offset = timedelta(0)
        restored = None
        if self._engagement is not None:
            restored = self._engagement.debug_restore()
        log.warning("DT1 clock reset to real time (engagement restored: %s)",
                    bool(restored))
        out = self.status()
        out["engagement_restored"] = restored
        return out

    # ── read ──────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Everything needed to tell at a glance that time is shifted."""
        real = timephrase.real_now()
        out: dict[str, Any] = {
            "ok": True,
            "enabled": self._enabled,
            "installed": self._installed,
            "active": self.active,
            "offset_seconds": round(self._offset.total_seconds(), 3),
            "offset": _humanize(self._offset),
            "real_now": real.isoformat(),
            "virtual_now": (real + self._offset).isoformat(),
        }
        if self._engagement is not None:
            anchor = self._engagement.debug_anchor()
            total = self._engagement.total()
            out["engagement"] = {
                "total_units": round(total, 1),
                "debug_anchor": None if anchor is None else round(anchor, 1),
                "synthetic_units": (
                    None if anchor is None else round(total - anchor, 1)
                ),
            }
        return out

    def _disabled(self) -> dict[str, Any]:
        return {
            "ok": False,
            "enabled": False,
            "error": (
                f"debug clock is off; restart with {ENV_FLAG}=1 to enable it "
                "(preferably against a copy of the database)"
            ),
        }


__all__ = ["ENV_FLAG", "DebugClock", "debug_clock_enabled"]
