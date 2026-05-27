"""World + shared-moments + relationship-axes mixin.

Extracted from :mod:`app.core.session_controller` to keep the controller
shell readable. Covers three loosely-related surfaces that all happen to
hang off ``SessionController`` because they share the same persistence
boundaries (``_world_store``, ``_shared_moments_store``,
``_relationship_axes_store``) and the same WS-broadcast listener
plumbing:

* **World (Aiko's room)** — snapshot, locations, items, gifts.
* **Shared moments** — third-person memorable beats surfaced in the
  Together tab and on anniversaries.
* **Relationship axes** — closeness / humor / trust / comfort
  drift broadcast to the UI (debounced to 0.05 steps).

State ownership stays in ``SessionController.__init__``; this mixin
just calls ``self.*``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from app.core.world_store import WorldStore


log = logging.getLogger("app.session")


class WorldMixin:
    """World, shared-moments, axes and ``get_together_summary``."""

    # ── World (Aiko's room) ─────────────────────────────────────────

    @property
    def world_store(self) -> "WorldStore | None":
        return self._world_store

    def world_snapshot(self) -> dict[str, Any]:
        """Return the full room state for the World tab."""
        store = self._world_store
        if store is None:
            return {"state": {}, "locations": [], "items": [], "enabled": False}
        snap = store.snapshot()
        snap["enabled"] = True
        return snap

    def add_world_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(patch)`` invoked after every world write.

        Patches are typed dicts with one of: ``state``, ``location``,
        ``item``, ``deleted_location_id``, ``deleted_item_id``. Listener
        runs synchronously on the writer thread; the WS hub broadcast
        translates each patch into a ``world_updated`` event.
        """
        if callback and callback not in self._world_listeners:
            self._world_listeners.append(callback)

    def _notify_world(self, patch: dict[str, Any]) -> None:
        for listener in list(self._world_listeners):
            try:
                listener(patch)
            except Exception:
                log.debug("world listener raised", exc_info=True)

    # ── Shared moments + axes listeners (schema v7) ──────────────────

    def add_shared_moment_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(patch)`` for shared-moment CRUD events.

        Patches carry one of ``moment`` (created/updated row dict) or
        ``deleted_moment_id`` (int). WS hub forwards as
        ``shared_moment_updated``.
        """
        if callback and callback not in self._shared_moment_listeners:
            self._shared_moment_listeners.append(callback)

    def _notify_shared_moment_added(self, row: Any) -> None:
        payload = row.to_dict() if hasattr(row, "to_dict") else {"id": row}
        self._notify_shared_moment({"moment": payload})

    def _notify_shared_moment(self, patch: dict[str, Any]) -> None:
        for listener in list(self._shared_moment_listeners):
            try:
                listener(patch)
            except Exception:
                log.debug("shared-moment listener raised", exc_info=True)

    def add_relationship_axes_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(state)`` for debounced axes updates.

        Fires only when at least one axis has drifted ≥ 0.05 from the
        last broadcast value, so a noisy chat doesn't flood the WS.
        """
        if callback and callback not in self._relationship_axes_listeners:
            self._relationship_axes_listeners.append(callback)

    def _maybe_notify_axes(self, state: Any) -> None:
        """Broadcast axes state when any axis crossed a 0.05 step."""
        try:
            current = {
                "closeness": float(state.closeness),
                "humor": float(state.humor),
                "trust": float(state.trust),
                "comfort": float(state.comfort),
            }
        except Exception:
            return
        last = self._axes_last_broadcast
        changed = any(
            abs(current[k] - float(last.get(k, 0.0))) >= 0.05 for k in current
        )
        if not changed and self._axes_last_broadcast.get("_initialised"):
            return
        self._axes_last_broadcast = dict(current)
        self._axes_last_broadcast["_initialised"] = 1.0
        payload = {
            "user_id": getattr(state, "user_id", self._user_id),
            **current,
            "updated_at": getattr(state, "updated_at", ""),
        }
        for listener in list(self._relationship_axes_listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("axes listener raised", exc_info=True)

    # ── Shared moments public API (consumed by REST layer) ──────────

    def list_shared_moments(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        vibe: str | None = None,
    ) -> dict[str, Any]:
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return {"items": [], "total": 0, "offset": int(offset), "limit": int(limit)}
        rows, total = store.list(offset=offset, limit=limit, vibe=vibe)
        return {
            "items": [r.to_dict() for r in rows],
            "total": int(total),
            "offset": int(offset),
            "limit": int(limit),
        }

    def add_shared_moment(
        self,
        *,
        summary: str,
        vibe: str,
        when: str | None = None,
        source: str = "manual",
        source_message_id: int | None = None,
    ) -> dict[str, Any] | None:
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return None
        row = store.add(
            summary=summary,
            vibe=vibe,
            when=when,
            source=source,
            source_session=self.session_key,
            source_message_id=source_message_id,
        )
        if row is None:
            return None
        self._notify_shared_moment_added(row)
        return row.to_dict()

    def update_shared_moment(
        self,
        moment_id: int,
        *,
        summary: str | None = None,
        vibe: str | None = None,
        when: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any] | None:
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return None
        row = store.update(
            int(moment_id),
            summary=summary,
            vibe=vibe,
            when=when,
            pinned=pinned,
        )
        if row is None:
            return None
        payload = row.to_dict()
        self._notify_shared_moment({"moment": payload})
        return payload

    def delete_shared_moment(self, moment_id: int) -> bool:
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return False
        ok = store.delete(int(moment_id))
        if ok:
            self._notify_shared_moment({"deleted_moment_id": int(moment_id)})
        return ok

    def mark_message_as_moment(
        self,
        message_id: int,
        *,
        vibe: str = "general",
    ) -> dict[str, Any] | None:
        """Promote an existing chat message to a shared_moment.

        Looks up the message by id, builds a "<user> and I…" summary from
        its content (capped at 200 chars), and writes a pinned moment.
        """
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return None
        try:
            rows = self._chat_db.execute_fetchone(
                "SELECT session_id, role, content, created_at "
                "FROM messages WHERE id = ?",
                (int(message_id),),
            )
        except Exception:
            log.debug("mark_message_as_moment fetch failed", exc_info=True)
            return None
        if rows is None:
            return None
        _session_id, role, content, created_at = rows
        text = str(content or "").strip()
        if len(text) < 4:
            return None
        # Build a third-person summary depending on who said it. The user
        # can always edit the summary afterwards in the Together tab.
        user_name = self.user_display_name
        if str(role) == "user":
            summary = f"{user_name} said: {text[:160]}".strip()
        elif str(role) == "assistant":
            summary = f"I said to {user_name}: {text[:160]}".strip()
        else:
            summary = text[:200]
        row = store.add(
            summary=summary,
            vibe=vibe,
            when=str(created_at) if created_at else None,
            source="manual",
            confidence=1.0,
            source_message_ids=[int(message_id)],
            source_session=self.session_key,
            source_message_id=int(message_id),
            pinned=True,
        )
        if row is None:
            return None
        self._notify_shared_moment_added(row)
        return row.to_dict()

    def get_relationship_axes(self) -> dict[str, Any]:
        store = getattr(self, "_relationship_axes_store", None)
        if store is None:
            return {
                "user_id": self._user_id,
                "closeness": 0.0,
                "humor": 0.0,
                "trust": 0.0,
                "comfort": 0.0,
                "updated_at": "",
                "enabled": False,
            }
        try:
            state = store.get(self._user_id)
        except Exception:
            log.debug("axes get failed", exc_info=True)
            return {
                "user_id": self._user_id,
                "closeness": 0.0,
                "humor": 0.0,
                "trust": 0.0,
                "comfort": 0.0,
                "updated_at": "",
                "enabled": True,
            }
        payload = state.to_payload()
        payload["enabled"] = bool(
            getattr(self._settings.agent, "relationship_axes_enabled", True),
        )
        return payload

    def get_together_summary(self) -> dict[str, Any]:
        """Combined snapshot for the Together UI tab."""
        # Relationship phase + counts.
        tracker = getattr(self, "_relationship_tracker", None)
        rel_state: Any = None
        if tracker is not None:
            try:
                rel_state = tracker.store.get(self._user_id)  # type: ignore[attr-defined]
            except Exception:
                rel_state = None
        try:
            from app.core.relationship import _MILESTONES, phase_for
        except Exception:
            _MILESTONES = ()  # type: ignore[assignment]
            def phase_for(*_a: Any, **_kw: Any) -> str:  # type: ignore[no-redef]
                return "new"

        phase = "new"
        if rel_state is not None:
            try:
                phase = phase_for(rel_state)
            except Exception:
                phase = "new"

        # Anniversary check.
        anniversary_payload: dict[str, Any] | None = None
        store = getattr(self, "_shared_moments_store", None)
        if (
            store is not None
            and bool(getattr(self._settings.agent, "anniversary_surfacing_enabled", True))
        ):
            try:
                from datetime import datetime, timezone

                from app.core.anniversary import pick_anniversary

                match = pick_anniversary(
                    store.iter_all(), now=datetime.now(timezone.utc),
                )
                if match is not None:
                    anniversary_payload = {
                        "moment_id": match.moment_id,
                        "summary": match.summary,
                        "vibe": match.vibe,
                        "days_ago": match.days_ago,
                        "window_label": match.window_label,
                    }
            except Exception:
                log.debug("together anniversary failed", exc_info=True)

        # Milestones list with crossed-off dates (when known).
        milestones: list[dict[str, Any]] = []
        last_milestone_label = None
        last_milestone_at = None
        if rel_state is not None:
            last_milestone_label = getattr(rel_state, "milestone_label", None)
            last_milestone_at = getattr(rel_state, "last_milestone_at", None)
        for label, _turns, _days in _MILESTONES:
            milestones.append({
                "label": label,
                "human": label.replace("_", " "),
                "crossed": label == last_milestone_label,
                "crossed_at": last_milestone_at if label == last_milestone_label else None,
            })

        # Days known / counts.
        first_seen = getattr(rel_state, "first_seen_at", None) if rel_state else None
        days_known = 0
        if first_seen:
            try:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days_known = int(
                    (datetime.now(timezone.utc) - dt).total_seconds() // 86400
                )
            except Exception:
                days_known = 0

        return {
            "phase": phase,
            "days_known": int(days_known),
            "total_turns": int(getattr(rel_state, "total_turns", 0) or 0)
            if rel_state else 0,
            "total_sessions": int(getattr(rel_state, "total_sessions", 0) or 0)
            if rel_state else 0,
            "first_seen_at": first_seen,
            "milestones": milestones,
            "axes": self.get_relationship_axes(),
            "anniversary_today": anniversary_payload,
            "recent_moments_count": (
                store.count() if store is not None else 0
            ),
        }

    def note_gift_received(self) -> None:
        """Hook: world layer calls this when the user just gave Aiko an item.

        Sets a single-turn flag consumed by the next ``_post_turn_inner_life``
        so the axes updater and moment detector see the signal.
        """
        self._last_turn_gift_received = True

    def note_promise_kept(self) -> None:
        """Hook: future ``promise → done`` transition will call this."""
        self._last_turn_promise_kept = True

    def update_world_state(
        self,
        *,
        location_id: int | None | object = ...,
        posture: str | None = None,
        activity: str | None = None,
        mood_note: str | None = None,
    ) -> dict[str, Any] | None:
        store = self._world_store
        if store is None:
            return None
        try:
            state = store.set_state(
                location_id=location_id,
                posture=posture,
                activity=activity,
                mood_note=mood_note,
            )
        except Exception:
            log.debug("world set_state failed", exc_info=True)
            return None
        snap = state.to_dict()
        self._notify_world({"state": snap})
        return snap

    def add_world_location(
        self,
        *,
        slug: str | None = None,
        name: str,
        description: str = "",
        position: int | None = None,
    ) -> dict[str, Any] | None:
        store = self._world_store
        if store is None:
            return None
        loc = store.add_location(
            slug=slug, name=name, description=description, position=position,
        )
        if loc is None:
            return None
        snap = loc.to_dict()
        self._notify_world({"location": snap})
        return snap

    def update_world_location(
        self,
        location_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        position: int | None = None,
    ) -> dict[str, Any] | None:
        store = self._world_store
        if store is None:
            return None
        loc = store.update_location(
            int(location_id),
            name=name,
            description=description,
            position=position,
        )
        if loc is None:
            return None
        snap = loc.to_dict()
        self._notify_world({"location": snap})
        return snap

    def delete_world_location(self, location_id: int) -> bool:
        store = self._world_store
        if store is None:
            return False
        ok = store.remove_location(int(location_id))
        if ok:
            self._notify_world({"deleted_location_id": int(location_id)})
            # Refresh items-with-cleared-location and state in one batch so
            # the UI reconciles in a single render pass.
            self._notify_world({"snapshot": store.snapshot()})
        return ok

    def add_world_item(
        self,
        *,
        name: str,
        kind: str = "other",
        slug: str | None = None,
        description: str = "",
        location_id: int | None = None,
        consumable: bool = False,
        quantity: int = 1,
        state: dict[str, Any] | None = None,
        given_by: str | None = None,
    ) -> dict[str, Any] | None:
        store = self._world_store
        if store is None:
            return None
        result = store.add_item(
            name=name,
            kind=kind,
            slug=slug,
            description=description,
            location_id=location_id,
            consumable=consumable,
            quantity=quantity,
            state=state,
            given_by=given_by,
        )
        if result is None:
            return None
        item, _created = result
        snap = item.to_dict()
        self._notify_world({"item": snap})
        return snap

    def update_world_item(
        self,
        item_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        kind: str | None = None,
        location_id: int | None | object = ...,
        quantity: int | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        store = self._world_store
        if store is None:
            return None
        item = store.update_item(
            int(item_id),
            name=name,
            description=description,
            kind=kind,
            location_id=location_id,
            quantity=quantity,
            state=state,
        )
        if item is None:
            return None
        snap = item.to_dict()
        self._notify_world({"item": snap})
        return snap

    def consume_world_item(
        self, item_id: int, *, amount: int = 1,
    ) -> dict[str, Any] | None:
        store = self._world_store
        if store is None:
            return None
        item, consumed = store.consume_item(int(item_id), amount=int(amount))
        if consumed <= 0:
            return None
        if item is None:
            self._notify_world({"deleted_item_id": int(item_id)})
            return {"deleted_item_id": int(item_id), "consumed": consumed}
        snap = item.to_dict()
        self._notify_world({"item": snap})
        return {"item": snap, "consumed": consumed}

    def delete_world_item(self, item_id: int) -> bool:
        store = self._world_store
        if store is None:
            return False
        ok = store.remove_item(int(item_id))
        if ok:
            self._notify_world({"deleted_item_id": int(item_id)})
        return ok

    def give_item(
        self,
        name: str,
        *,
        kind: str = "food",
        quantity: int = 1,
        description: str = "",
        location_slug: str | None = "kitchenette",
        consumable: bool | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Drop an item into Aiko's room, attributed to the user.

        This is the "give cookie" surface the UI calls. Defaults to
        consumable food in the kitchenette; the room's render block will
        surface it next turn so Aiko notices naturally without us
        injecting any system message.
        """
        store = self._world_store
        if store is None:
            return None
        target_loc_id: int | None = None
        if location_slug:
            loc = store.get_location(location_slug)
            if loc is None:
                # Fall back to any existing location so the gift always
                # lands somewhere.
                locations = store.list_locations()
                if locations:
                    target_loc_id = locations[0].id
            else:
                target_loc_id = loc.id
        is_consumable = (
            bool(consumable) if consumable is not None
            else (kind or "").strip().lower() == "food"
        )
        added = self.add_world_item(
            name=name,
            kind=kind,
            description=description,
            location_id=target_loc_id,
            consumable=is_consumable,
            quantity=quantity,
            state=state,
            given_by="user",
        )
        # Schema v7: nudge the next turn's relationship-axes update + the
        # moment-detector gate. The flag is consumed exactly once.
        try:
            self.note_gift_received()
        except Exception:
            pass
        return added

    def reseed_world(self, *, force: bool = True) -> dict[str, Any] | None:
        """Wipe the room and re-seed the rich default. Debug-only path."""
        store = self._world_store
        if store is None:
            return None
        store.seed_default(
            force=force,
            user_display_name=self.user_display_name,
        )
        snap = store.snapshot()
        self._notify_world({"snapshot": snap})
        return snap
