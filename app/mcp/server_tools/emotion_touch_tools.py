from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController



def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_emotion_episodes() -> str:
        """K57 — dump the directed-emotion episode store.

        Returns the master switch, every live episode (emotion /
        cause / intensity *after* a dry-run decay to now / source /
        created_at), the pending thaw slot, and any staged
        (not-yet-drained) triggers. Read-only — does not persist the
        decayed intensities.
        """
        try:
            from datetime import datetime, timezone

            from app.core.affect import emotion_episodes as _ee

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            raw = chat_db.kv_get(_ee.KV_EMOTION_EPISODES)
            stored = _ee.deserialize(raw)
            decayed = _ee.apply_decay(
                stored, datetime.now(timezone.utc),
            )
            decayed_by_id = {e.id: e for e in decayed.episodes}
            payload = {
                "enabled": bool(
                    getattr(
                        session._settings.agent,
                        "emotion_episodes_enabled",
                        True,
                    )
                ),
                "episodes": [
                    {
                        "id": e.id,
                        "emotion": e.emotion,
                        "cause": e.cause,
                        "stored_intensity": round(e.intensity, 3),
                        "current_intensity": round(
                            decayed_by_id[e.id].intensity, 3,
                        ) if e.id in decayed_by_id else 0.0,
                        "expired": e.id not in decayed_by_id,
                        "source": e.source,
                        "created_at": e.created_at,
                    }
                    for e in stored.episodes
                ],
                "pending_thaw": (
                    list(stored.pending_thaw)
                    if stored.pending_thaw else None
                ),
                "staged_triggers": list(
                    getattr(session, "_pending_emotion_triggers", []) or []
                ),
            }
            return json.dumps(payload)
        except Exception as exc:
            return f"get_emotion_episodes raised: {exc}"

    @mcp.tool()
    def force_emotion_episode(
        kind: str,
        cause: str = "",
        intensity: float = 0.6,
    ) -> str:
        """K57 — write an episode straight into the kv store.

        ``kind`` is one of lonely / miffed / warm_glow / smug /
        playful_jealous / hurt. The next turn's provider renders it
        (verify via ``get_last_response_detail.system_prompt``).
        Counter-events apply: forcing warm_glow cancels a live
        miffed and arms the thaw.
        """
        try:
            from datetime import datetime, timezone

            from app.core.affect import emotion_episodes as _ee

            if kind not in _ee.EMOTIONS:
                return json.dumps({
                    "error": "unknown emotion",
                    "taxonomy": list(_ee.EMOTIONS),
                })
            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            now = datetime.now(timezone.utc)
            state = _ee.apply_decay(
                _ee.deserialize(chat_db.kv_get(_ee.KV_EMOTION_EPISODES)),
                now,
            )
            state = _ee.add_episode(
                state,
                emotion=kind,
                cause=cause or f"forced via MCP ({kind})",
                intensity=intensity,
                source="forced",
                now=now,
                cap=max(
                    1,
                    int(getattr(
                        session._settings.agent, "emotion_episode_cap", 3,
                    )),
                ),
            )
            chat_db.kv_set(
                _ee.KV_EMOTION_EPISODES, _ee.serialize(state),
            )
            return json.dumps({
                "written": True,
                "live": [
                    {"emotion": e.emotion, "intensity": round(e.intensity, 3)}
                    for e in state.episodes
                ],
                "pending_thaw": (
                    list(state.pending_thaw) if state.pending_thaw else None
                ),
            })
        except Exception as exc:
            return f"force_emotion_episode raised: {exc}"

    @mcp.tool()
    def resolve_emotion_episode(kind: str) -> str:
        """K57 — resolve a live episode by hand (arms the thaw cue)."""
        try:
            from app.core.affect import emotion_episodes as _ee

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            state = _ee.deserialize(
                chat_db.kv_get(_ee.KV_EMOTION_EPISODES),
            )
            before = len(state.episodes)
            state = _ee.resolve(state, kind, reason="resolved via MCP")
            chat_db.kv_set(
                _ee.KV_EMOTION_EPISODES, _ee.serialize(state),
            )
            return json.dumps({
                "resolved": len(state.episodes) < before,
                "pending_thaw": (
                    list(state.pending_thaw) if state.pending_thaw else None
                ),
            })
        except Exception as exc:
            return f"resolve_emotion_episode raised: {exc}"

    @mcp.tool()
    def clear_emotion_episodes() -> str:
        """K57 — wipe the episode store (episodes + thaw slot)."""
        try:
            from app.core.affect import emotion_episodes as _ee

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            chat_db.kv_set(
                _ee.KV_EMOTION_EPISODES,
                _ee.serialize(_ee.EpisodeState()),
            )
            session._pending_emotion_triggers = []
            return json.dumps({"cleared": True})
        except Exception as exc:
            return f"clear_emotion_episodes raised: {exc}"

    @mcp.tool()
    def get_expression_mask_state() -> str:
        """K60 — dump the tsundere-mask state.

        Returns the dial mode, the live closeness/trust axes and the
        computed erosion ``strength``, the masked-emotion set, the
        last dere-slip stamp + cooldown, and the one-shot force-slip
        flag. Read-only.
        """
        try:
            from app.core.affect import expression_mask as _mask

            chat_db = getattr(session, "_chat_db", None)
            agent = session._settings.agent
            mode = _mask.normalize_mode(
                getattr(agent, "expression_mask", "off")
            )
            closeness = trust = None
            axes_store = getattr(
                session, "_relationship_axes_store", None,
            )
            if axes_store is not None:
                try:
                    axes = axes_store.get(session._user_id)
                    closeness = float(axes.closeness)
                    trust = float(axes.trust)
                except Exception:
                    pass
            payload = {
                "mode": mode,
                "closeness": closeness,
                "trust": trust,
                "strength": _mask.mask_strength(closeness, trust),
                "masked_emotions": sorted(
                    e for e in ("lonely", "warm_glow")
                    if _mask.is_masked(e, mode)
                ),
                "last_slip_at": (
                    chat_db.kv_get(_mask.KV_LAST_SLIP_AT)
                    if chat_db is not None else None
                ),
                "slip_cooldown_days": float(
                    getattr(agent, "mask_slip_cooldown_days", 2.0)
                ),
                "force_slip_armed": bool(
                    getattr(session, "_mask_force_slip_next", False)
                ),
            }
            return json.dumps(payload)
        except Exception as exc:
            return f"get_expression_mask_state raised: {exc}"

    @mcp.tool()
    def set_expression_mask(mode: str) -> str:
        """K60 — flip the mask dial live (off / tsundere_light /
        tsundere_full). In-memory only; persist via config to keep
        it across restarts."""
        try:
            from app.core.affect import expression_mask as _mask

            normalized = _mask.normalize_mode(mode)
            if normalized != str(mode).strip().lower():
                return json.dumps({
                    "error": "unknown mode",
                    "modes": list(_mask.MODES),
                })
            session._settings.agent.expression_mask = normalized
            return json.dumps({"mode": normalized})
        except Exception as exc:
            return f"set_expression_mask raised: {exc}"

    @mcp.tool()
    def force_dere_slip() -> str:
        """K60 — arm a one-shot slip: the next masked episode render
        bypasses the intensity + cooldown gates and appends the
        genuine-line-then-snap-back permission. Pair with
        ``force_emotion_episode(kind='lonely', intensity=0.8)`` and a
        non-off mask mode."""
        try:
            session._mask_force_slip_next = True
            return json.dumps({"armed": True})
        except Exception as exc:
            return f"force_dere_slip raised: {exc}"

    @mcp.tool()
    def get_tease_ledger() -> str:
        """K59 — dump the payback ledger.

        Returns the master switch, the live humor axis vs the
        collection floor, the last-offer cooldown stamp, and every
        banked debt (what / context / source / age / offered stamp).
        Read-only.
        """
        try:
            from datetime import datetime, timezone

            from app.core.relationship import tease_ledger as _tl

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            now = datetime.now(timezone.utc)
            state = _tl.deserialize(chat_db.kv_get(_tl.KV_TEASE_LEDGER))
            humor = None
            axes_store = getattr(
                session, "_relationship_axes_store", None,
            )
            if axes_store is not None:
                try:
                    humor = float(
                        axes_store.get(session._user_id).humor
                    )
                except Exception:
                    humor = None
            agent = session._settings.agent
            payload = {
                "enabled": bool(
                    getattr(agent, "tease_economy_enabled", True)
                ),
                "humor": humor,
                "min_humor": float(
                    getattr(agent, "tease_min_humor", 0.2)
                ),
                "last_offer_at": chat_db.kv_get(
                    "aiko.tease_last_offer_at",
                ),
                "cooldown_hours": float(
                    getattr(agent, "tease_collect_cooldown_hours", 12.0)
                ),
                "force_armed": bool(
                    getattr(
                        session, "_tease_collection_force_next", False,
                    )
                ),
                "debts": [
                    {
                        "id": d.id,
                        "what": d.what,
                        "context": d.context,
                        "source": d.source,
                        "created_at": d.created_at,
                        "age_hours": (
                            round(
                                (
                                    now - parsed
                                ).total_seconds() / 3600.0, 1,
                            )
                            if (
                                parsed := _tl._parse_iso(d.created_at)
                            ) is not None
                            else None
                        ),
                        "offered_at": d.offered_at,
                    }
                    for d in state.debts
                ],
            }
            return json.dumps(payload)
        except Exception as exc:
            return f"get_tease_ledger raised: {exc}"

    @mcp.tool()
    def force_tease_debt(what: str, context: str = "") -> str:
        """K59 — bank a debt directly into the ledger.

        Pair with ``force_tease_collection`` to verify the full
        bank → offer → collect → settle loop without waiting for an
        organic K29 pushback.
        """
        try:
            added = session._bank_tease_debt(
                what=what, context=context, source="forced",
            )
            return json.dumps({"banked": bool(added)})
        except Exception as exc:
            return f"force_tease_debt raised: {exc}"

    @mcp.tool()
    def force_tease_collection() -> str:
        """K59 — arm a one-shot bypass of the humor / cooldown / age
        gates so the next turn's provider offers the oldest debt."""
        try:
            session._tease_collection_force_next = True
            return json.dumps({"armed": True})
        except Exception as exc:
            return f"force_tease_collection raised: {exc}"

    @mcp.tool()
    def clear_tease_ledger() -> str:
        """K59 — wipe the ledger and the offer-cooldown stamp."""
        try:
            from app.core.relationship import tease_ledger as _tl

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            chat_db.kv_set(
                _tl.KV_TEASE_LEDGER, _tl.serialize(_tl.LedgerState()),
            )
            chat_db.kv_set("aiko.tease_last_offer_at", "")
            return json.dumps({"cleared": True})
        except Exception as exc:
            return f"clear_tease_ledger raised: {exc}"

    @mcp.tool()
    def clear_wants() -> str:
        """K52 — wipe the wants ledger (wants + re-entry cooldowns)."""
        try:
            from app.core.conversation import wants_ledger as _wl

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            chat_db.kv_set(
                _wl.KV_WANTS_LEDGER, _wl.serialize(_wl.LedgerState()),
            )
            return json.dumps({"cleared": True})
        except Exception as exc:
            return f"clear_wants raised: {exc}"

    @mcp.tool()
    def get_vulnerability_budget_state() -> str:
        """K15 — dump the persisted vulnerability budget snapshot.

        Returns a JSON dict with the master switch, the current
        persisted ``spent`` / ``last_decay_at`` from ``kv_meta``,
        the ``capacity`` computed against the live
        ``closeness`` / ``trust`` axes, the ``ratio``
        (``spent / capacity``), the predicted cue that would
        render *right now* without arming any force flag, and a
        settings snapshot covering all 7 K15 knobs.

        Pairs with ``spend_vulnerability`` for the end-to-end
        repro: call this first to confirm the budget is healthy
        (ratio < 0.5 -> silent), call ``spend_vulnerability(3)``
        twice, then call this again -- ratio should clear 0.5
        and ``cue_preview`` should render the half-spent / at-cap
        line.
        """
        try:
            from app.core.affect import vulnerability_budget as _vb

            agent = session._settings.agent
            chat_db = getattr(session, "_chat_db", None)
            enabled = bool(
                getattr(agent, "vulnerability_budget_enabled", True),
            )

            stored = None
            if chat_db is not None:
                try:
                    stored = chat_db.kv_get(_vb.KV_BUDGET_STATE)
                except Exception:
                    stored = None
            state = _vb.deserialize(stored)

            closeness = trust = None
            store = getattr(session, "_relationship_axes_store", None)
            if store is not None:
                try:
                    axes = store.get(session._user_id)
                    closeness = float(axes.closeness)
                    trust = float(axes.trust)
                except Exception:
                    closeness = trust = None

            min_cap = int(
                getattr(agent, "vulnerability_budget_min_capacity", 1),
            )
            max_cap = int(
                getattr(agent, "vulnerability_budget_max_capacity", 12),
            )
            capacity = _vb.compute_capacity(
                closeness, trust,
                min_cap=min_cap, max_cap=max_cap,
            )
            ratio = (
                (state.spent / capacity) if capacity > 0 else 0.0
            )
            cue_preview = _vb.render_inner_life_block(
                state,
                capacity,
                user_display_name=session.user_display_name,
            )

            payload = {
                "enabled": enabled,
                "spent": float(state.spent),
                "last_decay_at": state.last_decay_at,
                "closeness": closeness,
                "trust": trust,
                "capacity": capacity,
                "ratio": float(ratio),
                "cue_preview": cue_preview or None,
                "settings": {
                    "min_capacity": min_cap,
                    "max_capacity": max_cap,
                    "regen_per_hour": float(
                        getattr(
                            agent,
                            "vulnerability_budget_regen_per_hour",
                            0.5,
                        )
                    ),
                    "tier1_cost": int(
                        getattr(
                            agent, "vulnerability_budget_tier1_cost", 1,
                        )
                    ),
                    "tier2_cost": int(
                        getattr(
                            agent, "vulnerability_budget_tier2_cost", 3,
                        )
                    ),
                    "tier3_cost": int(
                        getattr(
                            agent, "vulnerability_budget_tier3_cost", 6,
                        )
                    ),
                },
                "force_state": {
                    "force_spent": getattr(
                        session,
                        "_vulnerability_budget_force_spent",
                        None,
                    ),
                    "force_reset": bool(
                        getattr(
                            session,
                            "_vulnerability_budget_force_reset",
                            False,
                        )
                    ),
                },
            }
            return json.dumps(payload, indent=2)
        except Exception as exc:
            return f"get_vulnerability_budget_state raised: {exc}"

    @mcp.tool()
    def spend_vulnerability(tier: int = 3) -> str:
        """K15 — spend ``tier`` tokens against the budget bucket.

        Mirrors what the post-turn hook would do if Aiko emitted
        a ``[[remember:self:...]]`` tag classified at the given
        tier, but without requiring a real LLM turn. Reads the
        persisted state from ``kv_meta``, applies decay, adds
        the tier's token cost, and writes the new state back.
        Returns a JSON dict with the before / after spent values
        and the predicted next-turn cue.

        Validates ``tier`` is in ``{1, 2, 3}``; any other value
        returns a palette-style error rather than silently spending
        the wrong amount.
        """
        try:
            from datetime import datetime, timezone

            from app.core.affect import vulnerability_budget as _vb

            if tier not in (1, 2, 3):
                return json.dumps(
                    {
                        "error": (
                            f"unknown tier: {tier!r} "
                            f"(must be 1, 2, or 3)"
                        ),
                    },
                    indent=2,
                )

            agent = session._settings.agent
            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps(
                    {"error": "chat_db not available"}, indent=2,
                )

            try:
                stored = chat_db.kv_get(_vb.KV_BUDGET_STATE)
            except Exception:
                stored = None
            state = _vb.deserialize(stored)
            cost = _vb.tier_cost(int(tier), agent)
            regen = float(
                getattr(
                    agent, "vulnerability_budget_regen_per_hour", 0.5,
                )
            )
            max_cap = int(
                getattr(agent, "vulnerability_budget_max_capacity", 12),
            )
            now = datetime.now(timezone.utc)
            new_state = _vb.spend(
                state, int(cost), now,
                regen_per_hour=regen, max_capacity=max_cap,
            )
            try:
                chat_db.kv_set(
                    _vb.KV_BUDGET_STATE, _vb.serialize(new_state),
                )
            except Exception as kv_exc:
                return json.dumps(
                    {"error": f"kv_set failed: {kv_exc}"}, indent=2,
                )

            closeness = trust = None
            store = getattr(session, "_relationship_axes_store", None)
            if store is not None:
                try:
                    axes = store.get(session._user_id)
                    closeness = float(axes.closeness)
                    trust = float(axes.trust)
                except Exception:
                    closeness = trust = None
            min_cap = int(
                getattr(agent, "vulnerability_budget_min_capacity", 1),
            )
            capacity = _vb.compute_capacity(
                closeness, trust,
                min_cap=min_cap, max_cap=max_cap,
            )
            cue_preview = _vb.render_inner_life_block(
                new_state, capacity,
                user_display_name=session.user_display_name,
            )

            return json.dumps(
                {
                    "tier": int(tier),
                    "cost": int(cost),
                    "spent_before": float(state.spent),
                    "spent_after": float(new_state.spent),
                    "capacity": capacity,
                    "ratio": (
                        float(new_state.spent / capacity)
                        if capacity > 0 else 0.0
                    ),
                    "cue_preview": cue_preview or None,
                },
                indent=2,
            )
        except Exception as exc:
            return f"spend_vulnerability raised: {exc}"

    @mcp.tool()
    def reset_vulnerability_budget() -> str:
        """K15 — arm a one-shot wipe of the vulnerability budget.

        Sets ``_vulnerability_budget_force_reset`` so the next
        provider call writes a fresh ``BudgetState(spent=0)`` to
        ``kv_meta``. Useful when a test session has drifted into
        the at-cap / over-cap band and you want to confirm the
        healthy-budget silence path renders correctly.

        One-shot: the flag is consumed by the next provider call.
        """
        try:
            session._vulnerability_budget_force_reset = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will write "
                        "BudgetState(spent=0) to kv_meta; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"reset_vulnerability_budget raised: {exc}"

    @mcp.tool()
    def get_touch_state() -> str:
        """K31 — dump the persisted TouchService state.

        Returns a JSON dict with the kv_meta key, today's UTC
        date, the per-kind ``last_fired`` ISO timestamps + the
        per-kind ``daily_counts``, and the full taxonomy (every
        :class:`TouchGesture` entry). Pairs with ``send_touch``
        to verify cooldowns are accumulating between dispatches.
        """
        try:
            service = getattr(session, "_touch_service", None)
            if service is None:
                return json.dumps(
                    {"enabled": False, "reason": "TouchService not constructed"},
                    indent=2,
                )
            snapshot = service.get_state_snapshot()
            snapshot["touch_enabled"] = bool(
                getattr(session._settings.agent, "touch_enabled", True),
            )
            return json.dumps(snapshot, indent=2)
        except Exception as exc:
            return f"get_touch_state raised: {exc}"

    @mcp.tool()
    def send_touch(kind: str, emoji: str = "", label: str = "") -> str:
        """K31 / B7 — force-fire one ``[[touch:KIND]]`` gesture.

        B7 removed all touch gating (axes / cooldown / daily-cap), so
        every gesture lands regardless of relationship state. ``kind``
        may be a curated built-in (``hug``, ``poke``, ...) OR an
        invented open-vocabulary kind; for customs pass the optional
        ``emoji`` / ``label`` to exercise the badge text. Useful for
        end-to-end debugging:

        1. ``send_touch("hug")`` → verify the chat bubble grows a
           "Aiko gave you a hug 🫂" badge AND the persona action
           banner appears in any open ``#/persona`` window AND the
           Live2D rig leans in.
        2. ``send_touch("fist_bump", "🤜", "bumped your fist")`` →
           verify an invented gesture renders its custom badge.
        3. ``add_user_reaction(message_id, "heart")`` → verify the
           reciprocity loop closes.

        Routes through ``_emit_avatar_touch`` so the accumulator + WS
        broadcast + persona banner fire exactly as on the real LLM
        path. Returns the dispatch verdict as JSON.
        """
        try:
            emit = getattr(session, "_emit_avatar_touch", None)
            if emit is None:
                return json.dumps(
                    {"dispatched": False, "reason": "service_unavailable"},
                    indent=2,
                )
            report = emit(kind, emoji, label)
            if report is None:
                return json.dumps(
                    {"dispatched": True, "kind": kind, "note": "emitted"},
                    indent=2,
                )
            gesture = report.gesture
            return json.dumps(
                {
                    "dispatched": bool(report.dispatched),
                    "reason": report.reason,
                    "kind": gesture.kind if gesture else kind,
                    "label": gesture.label if gesture else label,
                    "emoji": gesture.emoji if gesture else emoji,
                    "duration_ms": gesture.duration_ms if gesture else None,
                    "lean_amount": gesture.lean_amount if gesture else None,
                    "overlays": list(gesture.overlays) if gesture else [],
                },
                indent=2,
            )
        except Exception as exc:
            return f"send_touch raised: {exc}"

    @mcp.tool()
    def add_user_reaction(message_id: int, kind: str) -> str:
        """K32 — fake a user reaction on an assistant message.

        Mirrors the REST POST endpoint exactly: persists the
        reaction, bumps the relationship axes (subject to the
        daily cap), arms the next-turn inner-life cue ("Jacob just
        hearted that line"), and fires the WS broadcaster so the
        UI strip updates.

        Use it to repro the reciprocity round-trip:
          1. ``send_touch("hug")`` first.
          2. ``add_user_reaction(<assistant_message_id>, "hug")``.
          3. ``send_message("hey")`` and call
             ``get_last_response_detail`` to verify
             ``provider_ms.user_reactions`` is non-zero.

        Returns the new reactions map as JSON, or an error string
        on bad input.
        """
        try:
            from app.core.relationship.user_reactions import is_valid_kind

            if not is_valid_kind(kind):
                return f"unknown reaction kind: {kind}"
            result = session.apply_user_reaction(int(message_id), kind)
            if result is None:
                return f"message {message_id} not found / not assistant role"
            return json.dumps(result, indent=2)
        except Exception as exc:
            return f"add_user_reaction raised: {exc}"


