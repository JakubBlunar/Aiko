"""Avatar + desktop-shell + circadian-outfit mixin.

Extracted from :mod:`app.core.session_controller` to keep the controller
shell readable. Covers everything appearance-related that hangs off
``SessionController``:

* Avatar profile accessors (``avatar``, ``avatar_root``, ``avatar_payload``).
* User-tunable avatar knobs (``update_avatar_settings`` +
  ``add/remove_avatar_settings_listener``), persisted to ``config/user.json``.
* Desktop / Tauri shell knobs (persona-window geometry, same persist /
  listener pattern as avatar settings).
* Avatar overlay listener wiring.
* Circadian-driven outfit resolution (``current_circadian_period``,
  ``resolve_auto_outfit``) plus the LLM-driven ``[[outfit:X]]``
  override that auto-expires on period rollover.
* LLM-tag forwarding to the renderer: ``_emit_avatar_overlay``,
  ``_emit_avatar_outfit``, ``_emit_avatar_motion``.
* Backchannel-driven listening motions (``_emit_backchannel_motion``)
  with per-instance rate-limiting via ``_backchannel_motion_gate``.

State ownership stays in ``SessionController.__init__``; this mixin
just reads/writes ``self.*``.

NB: tests that previously patched
``app.core.session_controller.time.monotonic`` /
``app.core.session_controller.persist_user_overrides`` for methods now
living here patch
``app.core.session.avatar_mixin.time.monotonic`` /
``app.core.session.avatar_mixin.persist_user_overrides`` instead. The
patch must target the module where the symbol is *looked up*.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core import circadian as _circadian
from app.core.backchannel_classifier import BackchannelHint
from app.core.settings import OUTFIT_MODES, persist_user_overrides

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from app.core.avatar_profile import AvatarProfile


log = logging.getLogger("app.session")


# ── Backchannel-driven motion mapping (Phase B2) ─────────────────────────
# Maps each backchannel hint the classifier emits to one or more motion
# *names* that the renderer should fire as a "listening micro-cue". Names
# that resolve to a real motion file in the loaded rig's
# ``motions`` map are dispatched via ``_emit_avatar_motion``-style
# fan-out at ``priority="idle"`` so a regular reaction motion fired
# during the same turn cleanly pre-empts them.
#
# Hints that aren't here are intentionally skipped:
#   - ``surprise`` / ``amusement`` — already covered by the reaction
#     overlay path (``_emit_avatar_overlay``).
#   - ``concern`` — concern reads more naturally as the auto-sweat /
#     concerned-mood path than as a body motion.
#
# The single-element tuples produce deterministic motions; the
# multi-element tuple for ``thinking`` is alternated by
# ``SessionController._backchannel_thinking_index``.
_BACKCHANNEL_MOTION_MAP: dict[str, tuple[str, ...]] = {
    "agreement":    ("nod",),
    "disagreement": ("shake",),
    "thinking":     ("tilt_left", "tilt_right"),
    "confused":     ("microshake",),
}


class AvatarMixin:
    """Avatar, desktop-shell, circadian-outfit and LLM-tag emit methods."""

    # ── Avatar (fixed Alexia bundle) ─────────────────────────────────

    @property
    def avatar(self) -> "AvatarProfile | None":
        """The loaded :class:`AvatarProfile`, or ``None`` if files are missing."""
        return self._avatar

    @property
    def avatar_root(self) -> Path:
        return self._avatar_root

    def avatar_payload(self) -> dict[str, Any]:
        """Wire-format payload combining the immutable profile + runtime knobs."""
        base = self._avatar.to_dict() if self._avatar is not None else {
            "display_name": "",
            "entry_filename": "",
            "cubism_version": 3,
            "expressions": [],
            "motions": {},
            "reaction_mapping": {},
            "lip_sync_ids": [],
            "eye_blink_ids": [],
            "parameters": [],
            "parts": [],
            "capabilities": {},
            "overlays": {},
            "outfits": {},
        }
        base["settings"] = dict(self._avatar_settings_runtime)
        base["loaded"] = self._avatar is not None
        # Snapshot the world state the renderer needs for Tier-3 effects:
        # the circadian period drives auto-outfit; the resolved outfit
        # tells the renderer the *current* answer (which it can then
        # cross-fade into without recomputing the rule).
        base["circadian_period"] = self.current_circadian_period()
        base["resolved_outfit"] = self.resolve_auto_outfit()
        return base

    def update_avatar_settings(
        self,
        *,
        scale_multiplier: float | None = None,
        auto_outfit: str | None = None,
        expressiveness: float | None = None,
    ) -> dict[str, Any]:
        """Patch the user-tunable avatar knobs and notify listeners.

        Changes are written back to ``config/user.json`` so the next
        app launch starts with the user's preferred scale / outfit
        instead of resetting to the defaults baked into the dataclass.
        """
        changed = False
        persist_patch: dict[str, Any] = {}
        if scale_multiplier is not None:
            try:
                value = max(0.1, min(8.0, float(scale_multiplier)))
            except (TypeError, ValueError):
                value = self._avatar_settings_runtime["scale_multiplier"]
            if value != self._avatar_settings_runtime["scale_multiplier"]:
                self._avatar_settings_runtime["scale_multiplier"] = value
                self._settings.avatar.scale_multiplier = value
                persist_patch["scale_multiplier"] = value
                changed = True
        if auto_outfit is not None:
            normalized = str(auto_outfit).strip().lower()
            if normalized in OUTFIT_MODES:
                if normalized != self._avatar_settings_runtime["auto_outfit"]:
                    self._avatar_settings_runtime["auto_outfit"] = normalized
                    self._settings.avatar.auto_outfit = normalized
                    persist_patch["auto_outfit"] = normalized
                    changed = True
        if expressiveness is not None:
            try:
                value = max(0.0, min(1.5, float(expressiveness)))
            except (TypeError, ValueError):
                value = self._avatar_settings_runtime["expressiveness"]
            if value != self._avatar_settings_runtime["expressiveness"]:
                self._avatar_settings_runtime["expressiveness"] = value
                self._settings.avatar.expressiveness = value
                persist_patch["expressiveness"] = value
                changed = True
        snapshot = dict(self._avatar_settings_runtime)
        if changed:
            if persist_patch:
                # Best-effort: a write failure (e.g. read-only volume)
                # must not break the in-memory update or the WS push.
                try:
                    persist_user_overrides({"avatar": persist_patch})
                except Exception:
                    log.warning(
                        "failed to persist avatar settings to user.json",
                        exc_info=True,
                    )
            for cb in list(self._avatar_settings_listeners):
                try:
                    cb(dict(snapshot))
                except Exception:
                    log.debug("avatar settings listener failed", exc_info=True)
        return snapshot

    def add_avatar_settings_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb not in self._avatar_settings_listeners:
            self._avatar_settings_listeners.append(cb)

    def remove_avatar_settings_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb in self._avatar_settings_listeners:
            self._avatar_settings_listeners.remove(cb)

    # ── Desktop / Tauri shell knobs ──────────────────────────────────────

    def desktop_settings(self) -> dict[str, Any]:
        """Return a deep copy of the desktop runtime cache.

        The web layer hands this off as part of the WS ``hello`` snapshot
        so a freshly-connected window (main or persona) immediately knows
        the configured persona-window geometry.
        """
        persona = self._desktop_settings_runtime["persona_window"]
        return {
            "persona_window": dict(persona),
        }

    def update_desktop_settings(
        self,
        *,
        persona_window_width: int | None = None,
        persona_window_height: int | None = None,
        persona_window_always_on_top: bool | None = None,
    ) -> dict[str, Any]:
        """Patch persona-window geometry and notify listeners.

        Mirrors :meth:`update_avatar_settings`: clamps via the helpers in
        ``app.core.settings``, persists the change to ``config/user.json``
        (so an app restart picks the new value up), and broadcasts a
        ``desktop_settings_changed`` event to every connected client.
        """
        from app.core.settings import (
            clamp_persona_window_width,
            clamp_persona_window_height,
        )

        persona = self._desktop_settings_runtime["persona_window"]
        changed = False
        persist_patch: dict[str, Any] = {}

        if persona_window_width is not None:
            value = clamp_persona_window_width(
                persona_window_width, fallback=int(persona["width"])
            )
            if value != int(persona["width"]):
                persona["width"] = value
                self._settings.desktop.persona_window.width = value
                persist_patch["width"] = value
                changed = True
        if persona_window_height is not None:
            value = clamp_persona_window_height(
                persona_window_height, fallback=int(persona["height"])
            )
            if value != int(persona["height"]):
                persona["height"] = value
                self._settings.desktop.persona_window.height = value
                persist_patch["height"] = value
                changed = True
        if persona_window_always_on_top is not None:
            value = bool(persona_window_always_on_top)
            if value != bool(persona["always_on_top"]):
                persona["always_on_top"] = value
                self._settings.desktop.persona_window.always_on_top = value
                persist_patch["always_on_top"] = value
                changed = True

        snapshot = self.desktop_settings()
        if changed:
            if persist_patch:
                try:
                    persist_user_overrides(
                        {"desktop": {"persona_window": persist_patch}}
                    )
                except Exception:
                    log.warning(
                        "failed to persist desktop settings to user.json",
                        exc_info=True,
                    )
            for cb in list(self._desktop_settings_listeners):
                try:
                    cb(dict(snapshot))
                except Exception:
                    log.debug("desktop settings listener failed", exc_info=True)
        return snapshot

    def add_desktop_settings_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb not in self._desktop_settings_listeners:
            self._desktop_settings_listeners.append(cb)

    def remove_desktop_settings_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb in self._desktop_settings_listeners:
            self._desktop_settings_listeners.remove(cb)

    def add_avatar_overlay_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb not in self._avatar_overlay_listeners:
            self._avatar_overlay_listeners.append(cb)

    def remove_avatar_overlay_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb in self._avatar_overlay_listeners:
            self._avatar_overlay_listeners.remove(cb)

    def current_circadian_period(self) -> str:
        """Return the current period name (``morning``, ``night``, ...)."""
        try:
            return str(_circadian.compute().period)
        except Exception:
            return ""

    def resolve_auto_outfit(self) -> str:
        """Resolve the active outfit according to priority rules.

        Returns ``"pajamas"``, ``"pajamas_hooded"``, ``"day"``, or ``""``
        (no preference / model doesn't support outfits at all).

        Priority (highest → lowest):
          1. User-forced ``auto_outfit`` (set via ``/api/avatar``).
             Always wins; clears any LLM override as a side-effect.
          2. LLM-driven ``[[outfit:X]]`` override. Sticky until the next
             circadian period boundary, then auto-expired.
          3. Circadian default (``night``/``late_night`` → pajamas
             variant; falls back to the hooded variant when the bare
             one isn't supported).
        """
        avatar = self._avatar
        if avatar is None:
            return ""
        mode = self._avatar_settings_runtime.get("auto_outfit", "auto")
        caps = avatar.capabilities
        has_pajamas = bool(caps.get("has_pajamas", False))
        has_pajamas_hooded = bool(caps.get("has_pajamas_hooded", False))
        has_day = bool(caps.get("has_day_clothes", False))
        # User-forced modes (priority 1). Each falls back through the
        # other pajama variant before giving up to ``day`` / ``""`` so a
        # rig that only ships one of the two still respects the user's
        # intent ("they wanted pajamas, give them whichever exists").
        if mode == "pajamas":
            self._llm_outfit_override = ""
            self._llm_outfit_override_period = ""
            if has_pajamas:
                return "pajamas"
            if has_pajamas_hooded:
                return "pajamas_hooded"
            return "day" if has_day else ""
        if mode == "pajamas_hooded":
            self._llm_outfit_override = ""
            self._llm_outfit_override_period = ""
            if has_pajamas_hooded:
                return "pajamas_hooded"
            if has_pajamas:
                return "pajamas"
            return "day" if has_day else ""
        if mode == "day":
            self._llm_outfit_override = ""
            self._llm_outfit_override_period = ""
            return "day" if has_day else ""
        period = self.current_circadian_period()
        # LLM override applies in "auto" mode only, and only inside the
        # circadian period it was set in. Crossing the period boundary
        # auto-expires it so morning naturally flips back to day clothes.
        if self._llm_outfit_override:
            if (
                self._llm_outfit_override_period
                and period
                and period != self._llm_outfit_override_period
            ):
                self._llm_outfit_override = ""
                self._llm_outfit_override_period = ""
            else:
                override = self._llm_outfit_override
                if override == "pajamas" and has_pajamas:
                    return "pajamas"
                if override == "pajamas_hooded" and has_pajamas_hooded:
                    return "pajamas_hooded"
                if override == "day" and has_day:
                    return "day"
                # Override no longer realisable (capability vanished on
                # avatar swap); clear and fall through.
                self._llm_outfit_override = ""
                self._llm_outfit_override_period = ""
        # Auto: night/late_night → pajamas (preferred bare variant when
        # supported, else hooded), otherwise day clothes.
        if period in {"night", "late_night"}:
            if has_pajamas:
                return "pajamas"
            if has_pajamas_hooded:
                return "pajamas_hooded"
        return "day" if has_day else ""

    def _emit_avatar_overlay(self, name: str, *, duration_ms: int = 1500) -> None:
        """Forward an LLM-driven ``[[overlay:X]]`` to the renderer.

        Skipped silently if the loaded avatar doesn't support the
        requested overlay (capability ``has_X`` is False) — keeps a
        minimal future model from spamming the WS with effects it
        can't render.
        """
        if not name:
            return
        avatar = self._avatar
        if avatar is None:
            return
        cap_key = f"has_{name.strip().lower()}"
        if not avatar.capabilities.get(cap_key, False):
            return
        payload = {
            "name": name.strip().lower(),
            "duration_ms": int(max(150, duration_ms)),
        }
        for cb in list(self._avatar_overlay_listeners):
            try:
                cb(dict(payload))
            except Exception:
                log.debug("avatar overlay listener failed", exc_info=True)

    def _emit_avatar_outfit(self, name: str) -> None:
        """Apply an LLM-driven ``[[outfit:X]]`` directive.

        Sticky until the circadian period rolls over (handled lazily
        in :meth:`resolve_auto_outfit`). Ignored entirely when the
        user has manually forced an outfit via the settings panel —
        we don't want a stale narrative line ("…and slip into
        pajamas…") fighting an explicit user choice.
        """
        if not name:
            return
        normalized = str(name).strip().lower()
        if normalized not in {"pajamas", "pajamas_hooded", "day"}:
            return
        avatar = self._avatar
        if avatar is None:
            return
        mode = self._avatar_settings_runtime.get("auto_outfit", "auto")
        if mode != "auto":
            return  # User override wins; silently drop the LLM directive.
        caps = avatar.capabilities
        if normalized == "pajamas" and not caps.get("has_pajamas", False):
            return
        if normalized == "pajamas_hooded" and not caps.get(
            "has_pajamas_hooded", False,
        ):
            return
        if normalized == "day" and not caps.get("has_day_clothes", False):
            return
        period = self.current_circadian_period()
        prev_resolved = self.resolve_auto_outfit()
        self._llm_outfit_override = normalized
        self._llm_outfit_override_period = period
        new_resolved = self.resolve_auto_outfit()
        if new_resolved == prev_resolved:
            # No-op (already in this outfit). Don't spam listeners.
            return
        snapshot = dict(self._avatar_settings_runtime)
        for cb in list(self._avatar_settings_listeners):
            try:
                cb(dict(snapshot))
            except Exception:
                log.debug("avatar settings listener failed", exc_info=True)

    def _emit_avatar_motion(self, name: str) -> None:
        """Forward an LLM-driven ``[[motion:X]]`` to the renderer.

        Looks up the motion file in the loaded rig's ``motions`` map
        and emits an ``avatar_motion`` event with the resolved
        ``group`` + ``index`` so ``pixi-live2d-display`` can call
        ``model.motion(group, index)`` directly.

        Safety net: when ``name`` is NOT a motion file stem but IS a
        known overlay/gesture capability on the loaded rig (e.g. the
        LLM emitted ``[[motion:tail_wag]]`` instead of the correct
        ``[[overlay:tail_wag]]``), re-route to ``_emit_avatar_overlay``
        so the action still plays. Logged at INFO so the misroute is
        visible alongside the ``llm tags:`` line — the prompt grammar
        should still steer the model to the right channel, but
        forgiving the mistake is much better than silently dropping.

        Unknown names that match neither a motion nor an overlay are
        still silently dropped (LLM hallucinated).
        """
        if not name:
            return
        avatar = self._avatar
        if avatar is None:
            return
        normalized = str(name).strip().lower()
        if not normalized:
            return
        for group, refs in (avatar.motions or {}).items():
            for idx, ref in enumerate(refs):
                if (ref.name or "").lower() == normalized:
                    payload = {
                        "name": ref.name,
                        "group": str(group),
                        "index": int(idx),
                    }
                    for cb in list(self._avatar_motion_listeners):
                        try:
                            cb(dict(payload))
                        except Exception:
                            log.debug(
                                "avatar motion listener failed",
                                exc_info=True,
                            )
                    return
        # Misroute safety net: the LLM emitted ``[[motion:foo]]`` but
        # ``foo`` is an overlay capability on this rig. Forward to the
        # overlay path so the visual effect actually plays.
        if avatar.capabilities.get(f"has_{normalized}", False):
            log.info(
                "avatar motion '%s' has no motion file but matches "
                "an overlay capability; routing as overlay",
                normalized,
            )
            self._emit_avatar_overlay(normalized)

    def add_avatar_motion_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb not in self._avatar_motion_listeners:
            self._avatar_motion_listeners.append(cb)

    def remove_avatar_motion_listener(
        self, cb: Callable[[dict[str, Any]], None]
    ) -> None:
        if cb in self._avatar_motion_listeners:
            self._avatar_motion_listeners.remove(cb)

    def _emit_backchannel_motion(self, hint: BackchannelHint, partial: str) -> None:
        """Dispatch a low-priority motion in response to a backchannel hint.

        Wired in ``__init__`` as a backchannel listener so every hint
        the classifier emits goes through this filter. Unmapped hints
        and hints that arrive within the rate-limit window are
        silently dropped — the mapping table at
        :data:`_BACKCHANNEL_MOTION_MAP` is the single source of truth
        for which hints get a motion at all.

        ``priority="idle"`` is added to the payload so the frontend's
        ``MotionChannel`` queues at ``MotionPriority.IDLE``; a regular
        ``[[motion:X]]`` reaction motion fired during the same turn
        cleanly pre-empts the listening cue without explicit
        cancellation logic.
        """
        del partial  # not used; kept for listener-signature compatibility
        avatar = self._avatar
        if avatar is None:
            return
        candidates = _BACKCHANNEL_MOTION_MAP.get(hint)
        if not candidates:
            return  # surprise / amusement / concern -> handled elsewhere
        # Alternate ``thinking`` between tilt_left and tilt_right so a
        # long pondering window doesn't read as "stuck on one side".
        if len(candidates) > 1:
            picked = candidates[self._backchannel_thinking_index % len(candidates)]
            self._backchannel_thinking_index += 1
        else:
            picked = candidates[0]
        # Resolve the motion file in the loaded rig. We don't pre-bind
        # to specific groups so a re-grouping in the model3.json (e.g.
        # moving ``microshake`` to a different bucket) doesn't require
        # a code change here.
        resolved: tuple[str, int] | None = None
        for group, refs in (avatar.motions or {}).items():
            for idx, ref in enumerate(refs):
                if (ref.name or "").lower() == picked:
                    resolved = (str(group), int(idx))
                    break
            if resolved is not None:
                break
        if resolved is None:
            # Rig doesn't have the motion file (e.g. minimal model
            # without the Backchannel bucket). Drop silently — the
            # listening session still feels OK because the overlay
            # path is unaffected.
            return
        if not self._backchannel_motion_gate.consider(now=time.monotonic()):
            return
        group, idx = resolved
        payload = {
            "name": picked,
            "group": group,
            "index": idx,
            "priority": "idle",
        }
        for cb in list(self._avatar_motion_listeners):
            try:
                cb(dict(payload))
            except Exception:
                log.debug(
                    "avatar motion listener (backchannel) failed",
                    exc_info=True,
                )
