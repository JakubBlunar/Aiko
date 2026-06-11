"""Avatar + desktop-shell + circadian-outfit mixin.

Extracted from :mod:`app.core.session.session_controller` to keep the controller
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
``app.core.session.session_controller.time.monotonic`` /
``app.core.session.session_controller.persist_user_overrides`` for methods now
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

from app.core.affect import circadian as _circadian
from app.core.conversation.backchannel_classifier import BackchannelHint
from app.core.infra.settings import OUTFIT_MODES, persist_user_overrides

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from app.core.persona.avatar_profile import AvatarProfile


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


# Per-overlay duration overrides (ms). Names omitted here fall back
# to the ``_emit_avatar_overlay`` default of 1500 ms. ``tail_wag``
# gets a longer window because the prompt grammar in
# :mod:`app.core.session.prompt_assembler` advertises it as a "~2 s burst"
# and the visual is more satisfying when the wag has time to
# register on a physics-driven rig (where the boost runs through
# ``ParamBreath`` and propagates with the natural physics delay).
_OVERLAY_DURATION_OVERRIDES_MS: dict[str, int] = {
    "tail_wag": 2000,
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
        mood_inertia_damping: bool | None = None,
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
        if mood_inertia_damping is not None:
            value = bool(mood_inertia_damping)
            if value != self._avatar_settings_runtime.get(
                "mood_inertia_damping", True,
            ):
                self._avatar_settings_runtime["mood_inertia_damping"] = value
                self._settings.avatar.mood_inertia_damping = value
                persist_patch["mood_inertia_damping"] = value
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

    # ── Avatar accessories (Phase 4 expression overhaul) ───────────────

    def avatar_accessories_catalogue(self) -> dict[str, Any]:
        """Return the accessory catalogue + current per-key state.

        Each entry advertises:
          - ``key``: the capability stem (``lollipop`` /
            ``head_sunglasses`` / …);
          - ``kind``: ``"toggle"`` or ``"enum"`` (eye_color is the
            only enum today);
          - ``available``: whether the loaded rig advertises
            ``has_<key>``;
          - ``allowed_outfits``: list of outfit capability names the
            accessory renders under, or ``[]`` for unconstrained;
          - ``value``: the current persisted value
            (``False`` / ``"default"`` for missing keys);
          - ``options``: the enum value list (only set when
            ``kind == "enum"``).

        Used by ``GET /api/avatar/accessories`` and the
        SettingsDrawer Accessories sub-section. Stays empty when no
        avatar is loaded so a minimal future model degrades cleanly.
        """
        from app.core.infra.settings import ACCESSORY_KEYS, EYE_COLOR_STATES

        avatar = self._avatar
        catalogue: list[dict[str, Any]] = []
        state = self._avatar_settings_runtime.get("accessory_state") or {}
        outfit_gates: dict[str, list[str]] = {}
        capabilities: dict[str, bool] = {}
        # ``zs1`` is the only outfit-gated accessory today (crossed
        # arms only render against day_clothes). We surface the gate
        # as the accessory's ``allowed_outfits`` so the UI can disable
        # the row when the active outfit doesn't match. Reuse the
        # avatar_profile's pre-computed map instead of re-walking the
        # exp3 files at every GET.
        if avatar is not None:
            outfit_gates = dict(getattr(avatar, "outfit_gated_expressions", {}) or {})
            capabilities = dict(avatar.capabilities or {})
        # Expression-name → capability lookup so we can translate the
        # outfit gate's per-expression entry (``zs1``) into the
        # accessory key (``crossed_arms``). Live2D rigs are stamped
        # with the inverse map via ``_ALEXIA_EXPR_TO_CAPABILITY`` in
        # ``avatar_profile.py``; we reproduce the inversion lazily
        # here to avoid coupling the mixin to that module's privates.
        cap_to_expr: dict[str, str] = {}
        try:
            from app.core.persona.avatar_profile import _ALEXIA_EXPR_TO_CAPABILITY
            for expr_name, cap_name in _ALEXIA_EXPR_TO_CAPABILITY.items():
                cap_to_expr.setdefault(cap_name, expr_name)
        except Exception:
            log.debug("avatar_profile expr→cap import failed", exc_info=True)
        for key, kind in ACCESSORY_KEYS.items():
            cap_flag = f"has_{key}"
            # ``eye_color`` doesn't have a direct ``has_eye_color``
            # capability — it's two split capabilities (``has_eye_color_a``
            # / ``has_eye_color_b``). Surface availability as the
            # *intersection* so the UI only enables it when both
            # halves of the rig are usable.
            if key == "eye_color":
                available = bool(
                    capabilities.get("has_eye_color_a", False)
                    and capabilities.get("has_eye_color_b", False)
                )
            else:
                available = bool(capabilities.get(cap_flag, False))
            # Look up the expression file backing this capability
            # (e.g. ``crossed_arms`` → ``zs1``) and copy any outfit
            # gate over to the accessory metadata.
            backing_expr = cap_to_expr.get(key, "")
            allowed_outfits = list(outfit_gates.get(backing_expr, []))
            default_value: str | bool = "default" if kind == "enum" else False
            value: str | bool = state.get(key, default_value)
            if kind == "enum" and not isinstance(value, str):
                value = "default"
            if kind == "bool" and not isinstance(value, bool):
                value = bool(value)
            entry: dict[str, Any] = {
                "key": key,
                "kind": "toggle" if kind == "bool" else "enum",
                "available": available,
                "allowed_outfits": allowed_outfits,
                "value": value,
            }
            if kind == "enum" and key == "eye_color":
                entry["options"] = sorted(EYE_COLOR_STATES)
                entry["default"] = "default"
            catalogue.append(entry)
        return {
            "accessories": catalogue,
            "active_outfit": self.resolve_auto_outfit(),
        }

    def update_avatar_accessories(
        self, patch: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Apply a partial accessory-state update and broadcast it.

        Validates each key against :data:`ACCESSORY_KEYS` (booleans /
        enum allow-lists). Unknown keys raise ``ValueError`` so the
        REST layer can return a 400. Otherwise it's a merge into the
        persisted state with a settings-listener broadcast (the same
        channel ``update_avatar_settings`` uses), so the renderer's
        ``AccessoryChannel`` can re-sync on the next WS frame.
        """
        from app.core.infra.settings import (
            ACCESSORY_KEYS,
            EYE_COLOR_STATES,
            _load_accessory_state,
        )

        if not isinstance(patch, dict) or not patch:
            return dict(self._avatar_settings_runtime)
        unknown = sorted(set(patch.keys()) - set(ACCESSORY_KEYS.keys()))
        if unknown:
            raise ValueError(
                f"unknown accessory key(s): {', '.join(unknown)}"
            )
        # Round-trip through the loader so each value is coerced /
        # clamped identically to the load-time path. Then merge the
        # result onto the existing cache so untouched keys keep
        # whatever they were.
        normalized = _load_accessory_state(patch)
        # Enforce the enum allow-list explicitly here as well — the
        # loader silently falls back to the canonical default on a
        # bad enum value, but the PATCH endpoint should return a 400
        # so the client knows their PATCH was lossy.
        for key, value in patch.items():
            if ACCESSORY_KEYS.get(key) == "enum":
                token = str(value).strip().lower() if value is not None else ""
                if key == "eye_color" and token not in EYE_COLOR_STATES:
                    raise ValueError(
                        f"invalid eye_color value: {value!r}",
                    )
        current = dict(self._avatar_settings_runtime.get("accessory_state") or {})
        merged = {**current, **normalized}
        if merged == current:
            return dict(self._avatar_settings_runtime)
        self._avatar_settings_runtime["accessory_state"] = merged
        self._settings.avatar.accessory_state = dict(merged)
        try:
            persist_user_overrides({"avatar": {"accessory_state": dict(merged)}})
        except Exception:
            log.warning(
                "failed to persist accessory_state to user.json",
                exc_info=True,
            )
        snapshot = dict(self._avatar_settings_runtime)
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

    def _emit_avatar_overlay(
        self, name: str, *, duration_ms: int | None = None
    ) -> None:
        """Forward an LLM-driven ``[[overlay:X]]`` to the renderer.

        Skipped silently if the loaded avatar doesn't support the
        requested overlay (capability ``has_X`` is False) — keeps a
        minimal future model from spamming the WS with effects it
        can't render.

        **Stack form** (``[[overlay:A+B]]`` from the Phase 3
        expression-overhaul grammar): when ``name`` contains ``+``,
        the helper splits on the delimiter and dispatches each
        component as its own overlay pulse. The
        renderer's ``OverlayChannel`` natively supports concurrent
        pulses, so ``blush+grin`` paints both at once (blush is a
        Param58 pulse, grin is an ``expr:lzx`` pulse — different
        param-write channels, no fight). Per-component capability
        checks still apply, so the unsupported half of a stack is
        silently dropped without blocking the supported half.
        """
        if not name:
            return
        normalized = str(name).strip().lower()
        if not normalized:
            return
        if "+" in normalized:
            # Defer to :func:`split_reaction_stack` so the parser
            # semantics (dedup, trim, lowercase) stay in lock-step
            # with the reaction-stack side of the grammar.
            from app.core.affect.reactions import split_reaction_stack
            components = split_reaction_stack(normalized)
            for component in components:
                self._emit_avatar_overlay(component, duration_ms=duration_ms)
            return
        avatar = self._avatar
        if avatar is None:
            return
        cap_key = f"has_{normalized}"
        if not avatar.capabilities.get(cap_key, False):
            return
        if duration_ms is None:
            duration_ms = _OVERLAY_DURATION_OVERRIDES_MS.get(normalized, 1500)
        payload = {
            "name": normalized,
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

    # ── K31 soft physicality: [[touch:KIND]] ─────────────────────────────

    def add_avatar_touch_listener(
        self, cb: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a listener for K31 ``avatar_touch`` events.

        Payload shape: ``{"kind": str, "label": str, "emoji": str,
        "duration_ms": int, "lean_amount": float,
        "overlays": list[str], "message_id": int | None}``.
        The WS bridge (``app/web/server.py``) registers a listener
        here that broadcasts ``avatar_touch`` to all connected
        clients (so both chat + persona windows pick it up).
        """
        if cb not in self._avatar_touch_listeners:
            self._avatar_touch_listeners.append(cb)

    def remove_avatar_touch_listener(
        self, cb: Callable[[dict[str, Any]], None],
    ) -> None:
        if cb in self._avatar_touch_listeners:
            self._avatar_touch_listeners.remove(cb)

    def _emit_avatar_touch(self, kind: str) -> None:
        """Forward an LLM-driven ``[[touch:KIND]]`` to the renderer.

        Routes through :class:`TouchService.try_dispatch` first:

        - **Unknown kind**: silently dropped (LLM hallucinated).
        - **Disabled** (settings flag): silently dropped.
        - **Axes gate fails**: dropped with an INFO log so the
          MCP debug pass can see "Aiko asked for a hug but
          closeness was too low".
        - **Cooldown / daily cap hit**: dropped with an INFO log
          (intentional pacing, not a bug -- the persona block
          teaches her to feel the budget).
        - **OK**: accumulates the kind on
          ``self._current_turn_gestures`` so the post-turn pass
          can persist it on the assistant message row; fires
          companion overlays via the existing ``_emit_avatar_overlay``
          path; broadcasts an ``avatar_touch`` event to the
          registered listeners (the WS hub).

        The post-turn pass calls :meth:`_persist_turn_gestures`
        with the final assistant ``message_id`` to seal the
        accumulated list onto SQLite, so the bubble badge
        survives a reload.
        """
        if not kind:
            return
        normalized = str(kind).strip().lower()
        if not normalized:
            return

        touch_service = getattr(self, "_touch_service", None)
        if touch_service is None:
            # Soft-disabled: feature wasn't wired in this controller
            # build (e.g. unit-test scaffolding). Silently drop.
            return

        # Resolve the live relationship-axes snapshot for the gate
        # check. ``None`` here is OK: the service treats it as
        # "no axes data, gates pass" so test scaffolding can
        # still exercise the dispatch path.
        axes_state = None
        axes_store = getattr(self, "_relationship_axes_store", None)
        user_id = str(getattr(self, "_user_id", "") or "")
        if axes_store is not None and user_id:
            try:
                axes_state = axes_store.get(user_id)
            except Exception:
                log.debug("touch: axes_store.get failed", exc_info=True)
                axes_state = None

        from datetime import datetime, timezone

        report = touch_service.try_dispatch(
            normalized,
            axes=axes_state,
            now=datetime.now(timezone.utc),
        )
        if not report.dispatched:
            log.info(
                "touch dispatched: kind=%s rejected=true reason=%s",
                normalized,
                report.reason,
            )
            return
        gesture = report.gesture
        if gesture is None:
            # Defensive: dispatched=True always carries a gesture,
            # but guard the typed contract.
            return

        log.info(
            "touch dispatched: kind=%s rejected=false reason=%s "
            "duration_ms=%d lean_amount=%.2f overlays=%s",
            gesture.kind,
            report.reason,
            gesture.duration_ms,
            gesture.lean_amount,
            ",".join(gesture.overlays) if gesture.overlays else "-",
        )

        # Accumulate for post-turn persistence -- the controller wires
        # ``_current_turn_gestures`` in ``__init__`` and clears it on
        # the next turn.
        bucket = getattr(self, "_current_turn_gestures", None)
        if isinstance(bucket, list):
            bucket.append(gesture.kind)

        # Fire paired overlays through the existing channel so the
        # renderer paints them with no special-case routing. Errors
        # in the overlay path must NOT break the touch dispatch.
        for overlay in gesture.overlays:
            try:
                self._emit_avatar_overlay(overlay)
            except Exception:
                log.debug(
                    "touch: paired overlay emit failed (kind=%s overlay=%s)",
                    gesture.kind, overlay, exc_info=True,
                )

        payload: dict[str, Any] = {
            "kind": gesture.kind,
            "label": gesture.label,
            "emoji": gesture.emoji,
            "duration_ms": int(gesture.duration_ms),
            "lean_amount": float(gesture.lean_amount),
            "overlays": list(gesture.overlays),
        }
        for cb in list(self._avatar_touch_listeners):
            try:
                cb(dict(payload))
            except Exception:
                log.debug("avatar touch listener failed", exc_info=True)

    def _persist_turn_gestures(self, message_id: int) -> None:
        """Seal the per-turn gesture accumulator onto a message row.

        Called from :meth:`PostTurnMixin._post_turn_inner_life` after
        the assistant message has been persisted. If no gestures
        landed this turn, this is a no-op. The kinds are stored as
        a JSON array on ``messages.gestures`` so the chat bubble
        footer badge survives a reload / new tab.
        """
        bucket = getattr(self, "_current_turn_gestures", None)
        if not isinstance(bucket, list) or not bucket:
            return
        if not message_id or message_id <= 0:
            # Don't lose the data: keep the list so the next
            # successful persist can pick it up. (Caller is
            # expected to clear it on the next turn boundary.)
            return
        import json

        try:
            self._chat_db.update_message_gestures(
                int(message_id), json.dumps(list(bucket)),
            )
        except Exception:
            log.debug(
                "touch: update_message_gestures failed",
                exc_info=True,
            )
        finally:
            bucket.clear()

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
