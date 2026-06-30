"""World + shared-moments + relationship-axes mixin.

Extracted from :mod:`app.core.session.session_controller` to keep the controller
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

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

# kv_meta key the WorldNoticeWorker watches for a freshly user-given item.
# Holds a small JSON blob ``{"id", "name", "at"}`` stamped by
# ``add_world_item`` whenever ``given_by == "user"``.
WORLD_LAST_USER_GIFT_KEY = "world.last_user_gift"

# kv_meta key stamped (ISO-8601 UTC) whenever the brain (move_to /
# change_posture tools) or the user (World tab PATCH) *intentionally* sets
# Aiko's room state via ``update_world_state``. The autonomous movers
# (away-activity location beats, garden visit worker, circadian default)
# read it and defer for ``agent.world_intentional_hold_seconds`` so a spot
# Aiko deliberately chose ("I'll stay in the garden") isn't yanked away by
# a background worker. Duplicated as a literal in those worker modules to
# avoid importing the session package.
WORLD_INTENTIONAL_STATE_KEY = "world.intentional_state_at"

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from app.core.world.world_store import WorldStore


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

    # ── K21 fresh-eyes thread-note listeners ─────────────────────────

    def add_thread_note_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(payload)`` invoked when the
        ThreadResummaryWorker upserts a fresh-eyes note.

        Payload carries ``{session_id, title, note, messages_at}``. The
        WS hub translates it into a ``thread_note_updated`` event so the
        sidebar can refetch its session list and pick up the new title.
        """
        bucket = getattr(self, "_thread_note_listeners", None)
        if bucket is None:
            self._thread_note_listeners = []
            bucket = self._thread_note_listeners
        if callback and callback not in bucket:
            bucket.append(callback)

    def _notify_thread_note(self, payload: dict[str, Any]) -> None:
        for listener in list(getattr(self, "_thread_note_listeners", []) or []):
            try:
                listener(payload)
            except Exception:
                log.debug("thread-note listener raised", exc_info=True)

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

    # ── Knowledge gap listeners (F2 personality backlog) ─────────────

    def add_knowledge_gap_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(patch)`` for knowledge-gap CRUD events.

        Patches carry one of ``gap`` (created/updated row dict) or
        ``deleted_gap_id`` (int). WS hub forwards as
        ``knowledge_gap_updated``.
        """
        if callback and callback not in self._knowledge_gap_listeners:
            self._knowledge_gap_listeners.append(callback)

    def _notify_knowledge_gap_added(self, memory: Any) -> None:
        try:
            payload = memory.to_dict() if hasattr(memory, "to_dict") else {"id": memory}
        except Exception:
            payload = {"id": getattr(memory, "id", None)}
        self._notify_knowledge_gap({"gap": payload})

    def _notify_knowledge_gap(self, patch: dict[str, Any]) -> None:
        for listener in list(self._knowledge_gap_listeners):
            try:
                listener(patch)
            except Exception:
                log.debug("knowledge-gap listener raised", exc_info=True)

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

    # ── K68 embodied vitality broadcast ─────────────────────────────

    def add_vitality_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(patch)`` for body-energy updates.

        Patch shape: ``{energy, expressiveness_mult, band}``. Debounced by
        :meth:`_notify_vitality` (fires only on a ≥ 0.03 energy step) so a
        chatty session doesn't flood the WS with micro-movements.
        """
        if callback and callback not in self._vitality_listeners:
            self._vitality_listeners.append(callback)

    def vitality_snapshot(self) -> dict[str, Any]:
        """Current energy + derived embodiment fields (for the WS hello).

        Reads the persisted ``aiko.vitality`` state, applies lazy
        recovery toward the circadian baseline so a long idle gap is
        reflected, and returns the same patch shape the listeners get.
        Best-effort: returns a neutral default on any failure or when the
        feature is disabled.
        """
        try:
            from datetime import datetime

            from app.core.affect import vitality as _vit
            from app.core.affect import vitality_rhythm as _vr

            mem = self._memory_settings
            agent = self._settings.agent
            if not bool(getattr(agent, "vitality_enabled", True)):
                return {"energy": None, "expressiveness_mult": 1.0, "band": "normal"}
            chat_db = getattr(self, "_chat_db", None)
            now = datetime.now().astimezone()
            baseline, _rhythm = _vr.current_baseline(
                chat_db,
                now,
                enabled=bool(getattr(agent, "vitality_rhythm_enabled", True)),
                exception_chance=float(
                    getattr(mem, "vitality_rhythm_exception_chance", 0.3)
                ),
            )
            raw = chat_db.kv_get(_vit.KV_VITALITY) if chat_db is not None else None
            state = _vit.deserialize(raw, baseline=baseline, now=now)
            state = _vit.step_recover(
                state, baseline, now,
                half_life_hours=float(
                    getattr(mem, "vitality_recover_half_life_hours", 2.0)
                ),
            )
            return self._vitality_patch(state.energy)
        except Exception:
            log.debug("vitality_snapshot failed", exc_info=True)
            return {"energy": None, "expressiveness_mult": 1.0, "band": "normal"}

    def _vitality_patch(self, energy: float) -> dict[str, Any]:
        """Build the ``{energy, expressiveness_mult, band}`` patch."""
        from app.core.affect import vitality as _vit

        mem = self._memory_settings
        e = max(0.0, min(1.0, float(energy)))
        return {
            "energy": round(e, 4),
            "expressiveness_mult": _vit.expressiveness_multiplier(
                e,
                floor=float(getattr(mem, "vitality_expressiveness_floor", 0.7)),
                ceil=float(getattr(mem, "vitality_expressiveness_ceil", 1.2)),
            ),
            "band": _vit.band(
                e,
                low_threshold=float(getattr(mem, "vitality_low_threshold", 0.30)),
                high_threshold=float(
                    getattr(mem, "vitality_high_threshold", 0.70)
                ),
            ),
        }

    def _notify_vitality(self, energy: float, *, force: bool = False) -> None:
        """Broadcast a vitality patch when energy moved ≥ 0.03 (debounced).

        ``force=True`` bypasses the debounce (used for the one-shot MCP
        override + the first broadcast of a session).
        """
        try:
            e = max(0.0, min(1.0, float(energy)))
        except (TypeError, ValueError):
            return
        last = getattr(self, "_vitality_last_broadcast", None)
        if (
            not force
            and last is not None
            and abs(e - float(last)) < 0.03
        ):
            return
        self._vitality_last_broadcast = e
        patch = self._vitality_patch(e)
        for listener in list(getattr(self, "_vitality_listeners", [])):
            try:
                listener(patch)
            except Exception:
                log.debug("vitality listener raised", exc_info=True)

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

    # ── K32 user reactions: API used by /api/chat/messages/{id}/reactions

    def add_message_reaction_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(payload)`` for K32 reaction updates.

        Payload shape: ``{"message_id": int, "reactions": dict[str, int]}``.
        Fires on add AND on remove (the reactions map is the full
        post-edit state). The WS hub forwards as
        ``message_reaction_updated``.
        """
        listeners = self._message_reaction_listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def _notify_message_reaction(self, payload: dict[str, Any]) -> None:
        for listener in list(self._message_reaction_listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("message-reaction listener raised", exc_info=True)

    def _load_message_reactions(self, message_id: int) -> dict[str, int]:
        """Return the persisted ``reactions`` map for a message, or {}."""
        import json as _json

        try:
            row = self._chat_db.execute_fetchone(
                "SELECT reactions FROM messages WHERE id = ?",
                (int(message_id),),
            )
        except Exception:
            log.debug("_load_message_reactions failed", exc_info=True)
            return {}
        if row is None or row[0] is None:
            return {}
        try:
            data = _json.loads(row[0])
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for kind, count in data.items():
            if not isinstance(kind, str):
                continue
            try:
                out[kind.strip().lower()] = max(0, int(count))
            except (TypeError, ValueError):
                continue
        return out

    def apply_user_reaction(
        self, message_id: int, kind: str,
    ) -> dict[str, Any] | None:
        """Add a K32 reaction to a message and propagate side-effects.

        Returns ``{"message_id": int, "reactions": dict[str, int]}`` on
        success, ``None`` when the message doesn't exist or the kind is
        invalid. Side-effects:

        1. The reactions JSON column is updated in SQLite.
        2. If the message is from Aiko (``role == "assistant"``) and
           ``agent.user_reactions_axes_enabled`` is set, the
           relationship axes are bumped via the daily-cap state machine
           and the axes broadcaster fires.
        3. A ``(message_id, kind)`` entry is appended to
           ``_pending_user_reactions`` so the next assistant turn's
           inner-life provider can render the "Jacob just X-ed your
           reply" cue.
        4. The ``message_reaction_updated`` listener fires with the
           new full reactions map so both webviews stay in sync.
        """
        from app.core.relationship import user_reactions as _ur

        if not _ur.is_valid_kind(kind):
            return None
        if not message_id or int(message_id) <= 0:
            return None

        message_row = self._chat_db.get_message_row(int(message_id))
        if message_row is None:
            return None

        normalized_kind = str(kind).strip().lower()
        # Reject reactions on user messages -- the K32 tray only lives
        # on Aiko's bubbles. Reacting to your own message is a feature
        # mis-use and the inner-life cue would read as nonsense
        # ("Jacob hearted his own message").
        if str(message_row.role) != "assistant":
            return None

        # K32 master switch: when off, the REST handler should already
        # have 503'd, but guard here too so direct callers (MCP debug
        # tool) respect the same posture.
        agent = self._settings.agent
        if not bool(getattr(agent, "user_reactions_enabled", True)):
            return None

        existing = self._load_message_reactions(int(message_id))
        existing[normalized_kind] = existing.get(normalized_kind, 0) + 1

        import json as _json

        try:
            self._chat_db.update_message_reactions(
                int(message_id), _json.dumps(existing, sort_keys=True),
            )
        except Exception:
            log.debug("update_message_reactions failed", exc_info=True)
            return None

        # Arm the inner-life cue regardless of whether the axes bump
        # fires. ``surprise`` for instance has no axes delta but should
        # still appear in the next-turn cue.
        try:
            self._pending_user_reactions.append(
                (int(message_id), normalized_kind),
            )
        except Exception:
            log.debug(
                "K32 _pending_user_reactions append failed", exc_info=True,
            )

        # K57: warm reactions feed the episode store. Small intensity
        # per click — the merge bump in ``add_episode`` lets a burst
        # of hearts build a real glow (and a fresh warm_glow cancels
        # a live miffed). Applied by the post-turn drain.
        if normalized_kind in ("heart", "hug", "rose"):
            try:
                self._queue_emotion_trigger(
                    emotion="warm_glow",
                    cause=f"they reacted to you with a {normalized_kind}",
                    intensity=0.3,
                    source="user_reaction",
                )
            except Exception:
                log.debug("reaction warm_glow queue failed", exc_info=True)

        # J11: affection-style confirmation booster. A K32 reaction is
        # the explicit confirmation channel — map it to the affection
        # kind it reads as (REACTION_TO_KIND) and nudge the learned
        # weighting toward it. Sparse by design and never required:
        # J11 learns primarily from passive engagement (post-turn), so
        # this only refines. ``surprise`` (and any unmapped kind) is a
        # no-op inside ``apply_reaction_confirmation``.
        if bool(getattr(agent, "affection_style_enabled", True)):
            try:
                from datetime import datetime, timezone

                from app.core.relationship import affection_style as _af

                chat_db = getattr(self, "_chat_db", None)
                if (
                    chat_db is not None
                    and normalized_kind in _af.REACTION_TO_KIND
                ):
                    now = datetime.now(timezone.utc)
                    state = _af.deserialize(
                        chat_db.kv_get(_af.KV_AFFECTION_STYLE)
                    )
                    new_state = _af.apply_reaction_confirmation(
                        state,
                        normalized_kind,
                        now,
                        reaction_weight=float(
                            getattr(
                                agent, "affection_style_reaction_weight", 0.06,
                            ),
                        ),
                        floor=float(
                            getattr(agent, "affection_style_floor", 0.05),
                        ),
                    )
                    chat_db.kv_set(
                        _af.KV_AFFECTION_STYLE, _af.serialize(new_state),
                    )
                    log.info(
                        "affection-style confirm: reaction=%s -> kind=%s",
                        normalized_kind,
                        _af.REACTION_TO_KIND[normalized_kind],
                    )
            except Exception:
                log.debug(
                    "affection-style reaction confirm failed", exc_info=True,
                )

        # J12 — intimacy pacing. An affectionate reaction is sparse but
        # high-quality evidence that the user is running warm; blend its
        # forwardness score into the user-pace EMA (upward only — you
        # can't react your way to a colder pace). Gated by the learned
        # half's master switch.
        if bool(getattr(agent, "intimacy_pacing_enabled", True)):
            try:
                from datetime import datetime, timezone

                from app.core.relationship import intimacy_pacing as _ip

                chat_db = getattr(self, "_chat_db", None)
                score = _ip.score_user_reaction(normalized_kind)
                if chat_db is not None and score is not None:
                    now = datetime.now(timezone.utc)
                    state = _ip.deserialize(
                        chat_db.kv_get(_ip.KV_INTIMACY_PACING)
                    )
                    state = _ip.decay_pace(
                        state, now,
                        half_life_days=float(
                            getattr(
                                agent,
                                "intimacy_pacing_decay_half_life_days",
                                14.0,
                            )
                        ),
                    )
                    state = _ip.update_pace(
                        state, score, now,
                        learning_rate=float(
                            getattr(
                                agent, "intimacy_pacing_learning_rate", 0.15,
                            )
                        ),
                    )
                    chat_db.kv_set(
                        _ip.KV_INTIMACY_PACING, _ip.serialize(state),
                    )
                    log.info(
                        "intimacy-pacing reaction: kind=%s score=%.2f "
                        "pace=%.3f",
                        normalized_kind, score, state.user_pace,
                    )
            except Exception:
                log.debug(
                    "intimacy-pacing reaction update failed", exc_info=True,
                )

        # Axes bump (optional master switch). Driven by the
        # RelationshipAxesUpdater so the per-turn clamp, broadcast
        # debounce, and persist path all behave identically to the
        # existing reaction-tag bumps.
        updater = getattr(self, "_relationship_axes_updater", None)
        if (
            updater is not None
            and bool(getattr(agent, "user_reactions_axes_enabled", True))
        ):
            try:
                state = updater.apply_user_reaction(
                    self._user_id,
                    kind=normalized_kind,
                    daily_cap=float(
                        getattr(
                            agent, "user_reactions_daily_axis_cap", 0.15,
                        ),
                    ),
                )
                if state is not None:
                    self._maybe_notify_axes(state)
            except Exception:
                log.debug(
                    "RelationshipAxesUpdater.apply_user_reaction failed",
                    exc_info=True,
                )

        payload = {
            "message_id": int(message_id),
            "reactions": dict(existing),
        }
        self._notify_message_reaction(payload)
        log.info(
            "user_reaction applied: message_id=%d kind=%s reactions=%s",
            int(message_id),
            normalized_kind,
            existing,
        )
        return payload

    def remove_user_reaction(
        self, message_id: int, kind: str,
    ) -> dict[str, Any] | None:
        """Undo one K32 reaction click (decrement the counter).

        Symmetric with :meth:`apply_user_reaction` for the persistence
        + broadcast path, but NOT for axes -- removing a reaction
        does NOT subtract from the relationship axes. The reasoning:
        a click that already moved closeness +0.03 was a real
        signal at the time; undoing the UI affordance shouldn't
        unwind the moment.
        """
        from app.core.relationship import user_reactions as _ur

        if not _ur.is_valid_kind(kind):
            return None
        if not message_id or int(message_id) <= 0:
            return None

        existing = self._load_message_reactions(int(message_id))
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in existing:
            return {
                "message_id": int(message_id),
                "reactions": dict(existing),
            }

        existing[normalized_kind] = max(0, existing[normalized_kind] - 1)
        if existing[normalized_kind] <= 0:
            del existing[normalized_kind]

        import json as _json

        try:
            self._chat_db.update_message_reactions(
                int(message_id),
                _json.dumps(existing, sort_keys=True) if existing else None,
            )
        except Exception:
            log.debug("update_message_reactions (delete) failed", exc_info=True)
            return None

        payload = {
            "message_id": int(message_id),
            "reactions": dict(existing),
        }
        self._notify_message_reaction(payload)
        return payload

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
            from app.core.relationship.relationship import _MILESTONES, phase_for
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

                from app.core.relationship.anniversary import pick_anniversary

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
        # Stamp the intentional-placement watermark so the autonomous
        # movers defer to this deliberate choice (brain tool / World tab).
        # Best-effort: a failed stamp just means the hold doesn't apply.
        try:
            self._chat_db.kv_set(
                WORLD_INTENTIONAL_STATE_KEY,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except Exception:
            log.debug("intentional-state stamp failed", exc_info=True)
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
        # When the user drops something in Aiko's room (the UI's "give"
        # surface hits this path directly via POST /api/world/items, NOT
        # ``give_item``), arm the single-turn gift signal so the next
        # post-turn axes update + moment detector see it, and stamp a
        # kv watermark the WorldNoticeWorker reads to prime a proactive
        # "I noticed what you left me" nudge. Best-effort: never let a
        # bookkeeping hiccup break the item insert.
        if (given_by or "").strip().lower() == "user":
            try:
                self.note_gift_received()
            except Exception:
                log.debug("note_gift_received failed", exc_info=True)
            try:
                self._chat_db.kv_set(
                    WORLD_LAST_USER_GIFT_KEY,
                    json.dumps({
                        "id": snap.get("id"),
                        "name": snap.get("name") or name,
                        "at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    }),
                )
            except Exception:
                log.debug("world gift watermark write failed", exc_info=True)
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
