from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController



def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_self_noticing_state() -> str:
        """K30 — dump the live self-noticing detector state.

        Returns a JSON dict with the master switch, the three
        sub-switches, the most recent verdict from each sub-detector
        (agreement-streak share + sample size, flat-affect ranges +
        notable reaction count, repeated-thought cosine + matched
        index), the live cooldown remainders, the one-shot
        ``force_next`` flags, the size of the in-memory affect ring
        and assistant-vector ring, and a settings snapshot so you
        can see what thresholds are actually in force.

        The two cooldown counters (agreement, flat-affect) decrement
        by one each provider call regardless of trigger state -- a
        value of 0 means the next eligible turn can fire. Repeated-
        thought has no multi-turn cooldown; the one-shot
        carry-forward flag is set in ``post_turn`` and consumed by
        the next provider call.
        """
        try:
            agent = session._settings.agent

            def _verdict_payload(verdict: object | None) -> dict | None:
                if verdict is None:
                    return None
                payload = {}
                for field in (
                    "fired",
                    "agreement_share",
                    "pushback_share",
                    "valence_range",
                    "arousal_range",
                    "notable_reaction_count",
                    "max_cosine",
                    "matched_index",
                    "sample_size",
                ):
                    if hasattr(verdict, field):
                        value = getattr(verdict, field)
                        if isinstance(value, bool):
                            payload[field] = bool(value)
                        elif isinstance(value, int):
                            payload[field] = int(value)
                        elif isinstance(value, float):
                            payload[field] = round(float(value), 4)
                return payload

            affect_ring = getattr(session, "_self_noticing_affect_samples", None)
            vec_ring = getattr(session, "_self_noticing_aiko_vecs", None)
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "self_noticing_enabled", True)
                    ),
                    "sub_switches": {
                        "agreement_streak": bool(
                            getattr(
                                agent,
                                "self_noticing_agreement_streak_enabled",
                                True,
                            )
                        ),
                        "flat_affect": bool(
                            getattr(
                                agent,
                                "self_noticing_flat_affect_enabled",
                                True,
                            )
                        ),
                        "repeated_thought": bool(
                            getattr(
                                agent,
                                "self_noticing_repeated_thought_enabled",
                                True,
                            )
                        ),
                    },
                    "agreement_streak": {
                        "cooldown_remaining": int(
                            getattr(
                                session,
                                "_self_noticing_agreement_cooldown",
                                0,
                            ) or 0
                        ),
                        "force_next": bool(
                            getattr(
                                session,
                                "_self_noticing_force_agreement",
                                False,
                            )
                        ),
                        "last_verdict": _verdict_payload(
                            getattr(
                                session,
                                "_last_self_noticing_agreement",
                                None,
                            )
                        ),
                    },
                    "flat_affect": {
                        "cooldown_remaining": int(
                            getattr(
                                session,
                                "_self_noticing_flat_affect_cooldown",
                                0,
                            ) or 0
                        ),
                        "force_next": bool(
                            getattr(
                                session,
                                "_self_noticing_force_flat_affect",
                                False,
                            )
                        ),
                        "affect_ring_size": (
                            len(affect_ring) if affect_ring is not None else 0
                        ),
                        "last_verdict": _verdict_payload(
                            getattr(
                                session,
                                "_last_self_noticing_flat_affect",
                                None,
                            )
                        ),
                    },
                    "repeated_thought": {
                        "flagged_for_next_turn": bool(
                            getattr(
                                session,
                                "_repeated_thought_fired_last_turn",
                                False,
                            )
                        ),
                        "force_next": bool(
                            getattr(
                                session,
                                "_self_noticing_force_repeated_thought",
                                False,
                            )
                        ),
                        "last_cosine": round(
                            float(
                                getattr(
                                    session,
                                    "_repeated_thought_last_cosine",
                                    0.0,
                                )
                            ),
                            4,
                        ),
                        "last_matched_index": int(
                            getattr(
                                session,
                                "_repeated_thought_last_matched_index",
                                -1,
                            )
                        ),
                        "vec_ring_size": (
                            len(vec_ring) if vec_ring is not None else 0
                        ),
                    },
                    "settings": {
                        "window": int(
                            getattr(agent, "self_noticing_window", 6)
                        ),
                        "warmup": int(
                            getattr(agent, "self_noticing_warmup", 4)
                        ),
                        "agreement_threshold": float(
                            getattr(
                                agent,
                                "self_noticing_agreement_threshold",
                                0.80,
                            )
                        ),
                        "max_pushback": int(
                            getattr(
                                agent, "self_noticing_max_pushback", 0,
                            )
                        ),
                        "flat_valence_range": float(
                            getattr(
                                agent,
                                "self_noticing_flat_valence_range",
                                0.10,
                            )
                        ),
                        "flat_arousal_range": float(
                            getattr(
                                agent,
                                "self_noticing_flat_arousal_range",
                                0.10,
                            )
                        ),
                        "repeated_cosine_threshold": float(
                            getattr(
                                agent,
                                "self_noticing_repeated_cosine_threshold",
                                0.85,
                            )
                        ),
                        "cooldown_turns": int(
                            getattr(
                                agent,
                                "self_noticing_cooldown_turns",
                                5,
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_self_noticing_state raised: {exc}"

    @mcp.tool()
    def force_agreement_streak() -> str:
        """K30 — arm a one-shot bypass on the agreement-streak cooldown.

        Sets ``_self_noticing_force_agreement`` so the next provider
        call ignores the cooldown counter AND fires the cue
        regardless of whether the streak actually crosses the
        threshold. Useful for verifying the Heads-up line lands in
        the rendered prompt (call this tool, then ``send_message``,
        then inspect the next ``get_last_response_detail`` output
        for the cue in ``system_prompt``).
        """
        try:
            session._self_noticing_force_agreement = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will surface the agreement-"
                        "streak Heads-up unconditionally; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_agreement_streak raised: {exc}"

    @mcp.tool()
    def force_flat_affect() -> str:
        """K30 — arm a one-shot bypass on the flat-affect cooldown.

        Sets ``_self_noticing_force_flat_affect`` so the next
        provider call ignores the cooldown counter AND fires the
        cue regardless of whether the in-memory affect ring actually
        sits below the configured thresholds. The cue surfaces the
        normal "Heads-up: your read has been pretty even-keel..."
        line; consume by sending one user message.
        """
        try:
            session._self_noticing_force_flat_affect = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will surface the flat-affect "
                        "Heads-up unconditionally; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_flat_affect raised: {exc}"

    @mcp.tool()
    def force_repeated_thought() -> str:
        """K30 — arm a one-shot bypass on the repeated-thought flag.

        Sets ``_self_noticing_force_repeated_thought`` so the next
        provider call surfaces the "your last reply was very close
        to something you already said" Heads-up regardless of the
        actual cosine measurement. The flag is consumed whether the
        cue fires or not -- strictly one-turn.
        """
        try:
            session._self_noticing_force_repeated_thought = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will surface the repeated-"
                        "thought Heads-up unconditionally; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_repeated_thought raised: {exc}"

    @mcp.tool()
    def get_pre_thought_state() -> str:
        """K11 — inspect the pre-thought / counterfactual cache.

        Returns the master switch + caps, the LLM rate-limiter
        snapshot (``hour_used``/``day_used`` vs caps), and the current
        active ``pre_thought`` memories (id, question, a short slice of
        the drafted thought, tier, salience, created_at). Use alongside
        ``force_pre_thought()`` to verify the worker end-to-end without
        waiting for the idle cadence.
        """
        try:
            agent = session._settings.agent
            memory = session._settings.memory
            out: dict[str, Any] = {
                "enabled": bool(
                    getattr(agent, "pre_thought_enabled", True)
                ),
                "interval_seconds": int(
                    getattr(memory, "pre_thought_interval_seconds", 3600)
                ),
                "max_active": int(
                    getattr(agent, "pre_thought_max_active", 12)
                ),
                "candidates": int(
                    getattr(agent, "pre_thought_candidates", 4)
                ),
                "max_per_run": int(
                    getattr(agent, "pre_thought_max_per_run", 2)
                ),
                "min_novelty": float(
                    getattr(agent, "pre_thought_min_novelty", 0.85)
                ),
            }
            limiter = getattr(session, "_pre_thought_rate_limiter", None)
            if limiter is not None:
                from datetime import datetime, timezone

                try:
                    out["rate"] = limiter.snapshot(
                        datetime.now(timezone.utc)
                    )
                except Exception:
                    out["rate"] = None
            store = getattr(session, "_memory_store", None)
            rows: list[dict[str, Any]] = []
            if store is not None:
                try:
                    for mem in store.iter_by_kind("pre_thought"):
                        if mem.tier == "archive":
                            continue
                        meta = mem.metadata or {}
                        rows.append(
                            {
                                "id": mem.id,
                                "question": meta.get("question"),
                                "thought": (
                                    str(meta.get("thought") or "")[:160]
                                ),
                                "tier": mem.tier,
                                "salience": round(
                                    float(mem.salience), 3
                                ),
                                "created_at": mem.created_at,
                            }
                        )
                except Exception:
                    pass
            out["active_count"] = len(rows)
            out["active"] = rows
            return json.dumps(out, indent=2, default=str)
        except Exception as exc:
            return f"get_pre_thought_state raised: {exc}"

    @mcp.tool()
    def force_pre_thought() -> str:
        """K11 — run the PreThoughtWorker once, ignoring its interval gate.

        Returns the worker's result dict (``wrote``, ``checked``,
        ``memory_ids``, rejection counts, ``pruned``, ``llm_ms``). The
        rate-limiter and ``max_active`` gates still apply, so a result
        of ``{"skipped": "rate_limited"}`` / ``{"skipped":
        "max_active"}`` is expected when those are exhausted.
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return "scheduler not running (memory.tiers_enabled may be off)"
        try:
            result = sched.force_run("pre_thought")
        except KeyError:
            return "pre_thought worker not registered"
        except Exception as exc:
            return f"force_pre_thought raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def get_thread_note_state() -> str:
        """K21 — inspect the fresh-eyes thread note for the active session.

        Returns the master switch + trigger knobs, the LLM rate-limiter
        snapshot, the active session's message count, and the current
        stored note (title, note, messages_at watermark, updated_at).
        Pairs with ``force_thread_resummary()`` to verify the worker
        end-to-end without waiting for the idle cadence.
        """
        try:
            agent = session._settings.agent
            memory = session._settings.memory
            out: dict[str, Any] = {
                "enabled": bool(
                    getattr(agent, "thread_resummary_enabled", True)
                ),
                "interval_seconds": int(
                    getattr(memory, "thread_resummary_interval_seconds", 3600)
                ),
                "min_messages": int(
                    getattr(agent, "thread_resummary_min_messages", 12)
                ),
                "message_interval": int(
                    getattr(agent, "thread_resummary_message_interval", 50)
                ),
                "max_age_hours": float(
                    getattr(agent, "thread_resummary_max_age_hours", 24.0)
                ),
            }
            session_key = session.session_key
            out["session_id"] = session_key
            try:
                out["message_count"] = session._chat_db.get_message_count(
                    session_key
                )
            except Exception:
                out["message_count"] = None
            limiter = getattr(session, "_thread_resummary_rate_limiter", None)
            if limiter is not None:
                from datetime import datetime, timezone

                try:
                    out["rate"] = limiter.snapshot(datetime.now(timezone.utc))
                except Exception:
                    out["rate"] = None
            try:
                row = session._chat_db.get_thread_note(session_key)
            except Exception:
                row = None
            if row is None:
                out["note"] = None
            else:
                out["note"] = {
                    "title": row.title,
                    "note": row.note,
                    "messages_at": row.messages_at,
                    "updated_at": row.updated_at,
                }
            return json.dumps(out, indent=2, default=str)
        except Exception as exc:
            return f"get_thread_note_state raised: {exc}"

    @mcp.tool()
    def force_thread_resummary() -> str:
        """K21 — run the ThreadResummaryWorker once, ignoring its interval gate.

        Returns the worker's result dict (``wrote``, ``title``,
        ``messages_at``, ``llm_ms`` on success; or a ``skipped`` reason
        like ``too_short`` / ``not_due`` / ``rate_limited``). The
        min-message and trigger gates still apply.
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return "scheduler not running (memory.tiers_enabled may be off)"
        try:
            result = sched.force_run("thread_resummary")
        except KeyError:
            return "thread_resummary worker not registered"
        except Exception as exc:
            return f"force_thread_resummary raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def get_persona_regression_state() -> str:
        """K10 — read the last persona-regression snapshot.

        Returns the JSON snapshot persisted under
        ``aiko.persona_regression.last_run`` (``{}`` until the first
        run). Pairs with ``run_persona_regression`` for end-to-end
        repro: run the eval, then read this to inspect per-turn
        pass/fail + the failure reasons.
        """
        try:
            snapshot_fn = getattr(
                session, "persona_regression_snapshot", None,
            )
            if snapshot_fn is None:
                return json.dumps({"error": "unavailable"})
            return json.dumps(snapshot_fn(), indent=2)
        except Exception as exc:
            return f"get_persona_regression_state raised: {exc}"

    @mcp.tool()
    def run_persona_regression() -> str:
        """K10 — replay the golden-turn fixture and return the snapshot.

        Builds each canonical turn's prompt (minimal persona-only or
        full live scope per fixture), runs it through the background
        worker LLM, scores the reply against the style markers, and
        persists + returns the aggregated snapshot
        (``passed/total`` + per-turn failures). On-demand; no
        background spend.
        """
        try:
            run_fn = getattr(session, "run_persona_regression", None)
            if run_fn is None:
                return json.dumps({"error": "unavailable"})
            return json.dumps(run_fn(), indent=2)
        except Exception as exc:
            return f"run_persona_regression raised: {exc}"

    @mcp.tool()
    def get_day_color_state() -> str:
        """K27 — dump the live daily personality colour state.

        Returns a JSON dict with the master switch, the current
        stored colour name + ``set_at`` ISO timestamp, the age in
        hours, an ``is_stale`` boolean (true means the next
        provider call will lazy-roll a fresh colour), the worker
        cadence in seconds, both one-shot force flags
        (``force_next`` / ``force_reroll``), and the full palette
        names so a follow-up ``force_day_color`` call doesn't need
        clairvoyance.

        Pairs with ``force_day_color`` / ``reroll_day_color`` for
        end-to-end repro without shifting the OS clock:

        1. Call ``get_day_color_state`` -- read the current name +
           palette.
        2. Call ``force_day_color(color="pensive")`` -- arm the
           one-shot override.
        3. Send a message and read ``get_last_response_detail`` --
           the rendered system prompt should contain the
           "Your day's colour today: pensive --" line.
        4. ``reroll_day_color`` -- the next ``get_day_color_state``
           shows a new name + fresh ``set_at``.
        """
        try:
            from datetime import datetime

            from app.core.affect import day_color
            from app.core.affect.day_color_worker import (
                KV_DAY_COLOR,
                KV_DAY_COLOR_SET_AT,
            )

            agent = session._settings.agent
            chat_db = getattr(session, "_chat_db", None)
            stored_name: str | None = None
            stored_at: str | None = None
            if chat_db is not None:
                try:
                    stored_name = chat_db.kv_get(KV_DAY_COLOR)
                except Exception:
                    stored_name = None
                try:
                    stored_at = chat_db.kv_get(KV_DAY_COLOR_SET_AT)
                except Exception:
                    stored_at = None

            now = datetime.now().astimezone()
            stale = day_color.is_stale(stored_at, now)

            age_hours: float | None = None
            if stored_at:
                try:
                    text = str(stored_at).strip()
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    stored_dt = datetime.fromisoformat(text)
                    if stored_dt.tzinfo is None:
                        stored_dt = stored_dt.astimezone()
                    age_hours = round(
                        (now - stored_dt).total_seconds() / 3600.0, 3,
                    )
                except Exception:
                    age_hours = None

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "day_color_enabled", True)
                    ),
                    "interval_seconds": int(
                        getattr(
                            agent,
                            "day_color_check_interval_seconds",
                            3600,
                        )
                    ),
                    "current": {
                        "name": stored_name,
                        "set_at": stored_at,
                        "age_hours": age_hours,
                        "is_stale": stale,
                    },
                    "force_flags": {
                        "force_next": getattr(
                            session, "_day_color_force_next", None,
                        ),
                        "force_reroll": bool(
                            getattr(
                                session, "_day_color_force_reroll", False,
                            )
                        ),
                    },
                    "palette": [c.name for c in day_color.PALETTE],
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_day_color_state raised: {exc}"

    @mcp.tool()
    def force_day_color(color: str) -> str:
        """K27 — arm a one-shot palette override.

        Sets ``_day_color_force_next`` so the *next* provider call
        renders the requested colour without touching ``kv_meta``
        (the persisted daily roll survives). Validates ``color``
        against the palette and returns ``{"error": "unknown
        color", ...}`` with the palette list when the name isn't
        recognised.

        One-shot: the flag is consumed by the next provider call
        whether or not the cue fires (in practice it always does
        because the validation has already passed).
        """
        try:
            from app.core.affect import day_color

            chosen = day_color.get_color_by_name(color)
            if chosen is None:
                return json.dumps(
                    {
                        "error": "unknown color",
                        "palette": [c.name for c in day_color.PALETTE],
                    },
                    indent=2,
                )
            session._day_color_force_next = chosen.name
            return json.dumps(
                {
                    "armed": True,
                    "color": chosen.name,
                    "tagline": chosen.tagline,
                    "note": (
                        "next provider call will render this colour; "
                        "kv_meta NOT modified; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_day_color raised: {exc}"

    @mcp.tool()
    def reroll_day_color() -> str:
        """K27 — arm a one-shot reroll of today's colour.

        Sets ``_day_color_force_reroll`` so the next provider call
        rolls a fresh palette entry via
        :func:`day_color.roll_for_today` and writes it to
        ``kv_meta`` (overwriting today's stored colour). Useful
        for end-to-end repro without waiting for midnight or
        shifting the OS clock.

        One-shot: the flag is consumed by the next provider call.
        The result lands in ``kv_meta`` so the new colour persists
        for the rest of the local day; subsequent calls hit the
        normal stable-read path until midnight.
        """
        try:
            session._day_color_force_reroll = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will roll a fresh colour "
                        "and write it to kv_meta; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"reroll_day_color raised: {exc}"

    @mcp.tool()
    def get_wants_state() -> str:
        """K52 — dump the wants ledger snapshot.

        Returns a JSON dict with the master switch, every live want
        (id / text / kind / source / pressure / age in days), the
        re-entry cooldown map, the rendered cue preview for the next
        turn, and the relevant settings knobs. Pair with
        ``force_want`` / ``force_want_imperative`` for end-to-end
        repro: add a want, confirm the soft band renders, force the
        imperative, send a message, and verify the directive lands
        in ``get_last_response_detail.system_prompt``.
        """
        try:
            from datetime import datetime, timezone

            from app.core.conversation import wants_ledger as _wl

            agent = session._settings.agent
            chat_db = getattr(session, "_chat_db", None)
            stored = None
            if chat_db is not None:
                try:
                    stored = chat_db.kv_get(_wl.KV_WANTS_LEDGER)
                except Exception:
                    stored = None
            state = _wl.deserialize(stored)
            now = datetime.now(timezone.utc)
            payload = {
                "enabled": bool(
                    getattr(agent, "wants_ledger_enabled", True)
                ),
                "wants": [
                    {
                        "id": w.id,
                        "text": w.text,
                        "kind": w.kind,
                        "source": w.source,
                        "source_ref": w.source_ref,
                        "pressure": round(float(w.pressure), 3),
                        "age_days": round(_wl.age_days(w, now), 2),
                    }
                    for w in sorted(
                        state.wants,
                        key=lambda w: w.pressure,
                        reverse=True,
                    )
                ],
                "recently_acted": dict(state.recently_acted),
                "cue_preview": _wl.render_block(
                    state, now,
                    user_display_name=session.user_display_name,
                    imperative_threshold=float(
                        getattr(agent, "wants_imperative_threshold", 0.7)
                    ),
                ) or None,
                "force_imperative_armed": bool(
                    getattr(session, "_wants_force_imperative", False)
                ),
                "settings": {
                    "growth_per_day": float(
                        getattr(agent, "wants_growth_per_day", 0.25)
                    ),
                    "imperative_threshold": float(
                        getattr(agent, "wants_imperative_threshold", 0.7)
                    ),
                    "cap": int(getattr(agent, "wants_cap", 8)),
                    "max_age_days": float(
                        getattr(agent, "wants_max_age_days", 14.0)
                    ),
                    "reentry_cooldown_days": float(
                        getattr(agent, "wants_reentry_cooldown_days", 5.0)
                    ),
                    "worker_interval_seconds": float(
                        getattr(
                            agent, "wants_worker_interval_seconds", 3600.0,
                        )
                    ),
                },
            }
            return json.dumps(payload, indent=2)
        except Exception as exc:
            return f"get_wants_state raised: {exc}"

    @mcp.tool()
    def force_want(
        text: str,
        kind: str = "ask",
        pressure: float = 0.3,
    ) -> str:
        """K52 — insert a manual want into the ledger.

        ``kind`` is one of ``ask`` / ``share`` / ``steer``;
        ``pressure`` in ``[0, 1]`` sets the starting intensity (use
        >= the imperative threshold, default 0.7, to see the
        directive band immediately). Returns the updated ledger
        snapshot. Dedup / cap rules apply — a refusal reports why.
        """
        try:
            from datetime import datetime, timezone

            from app.core.conversation import wants_ledger as _wl

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"error": "no chat db"})
            state = _wl.deserialize(chat_db.kv_get(_wl.KV_WANTS_LEDGER))
            new_state, added = _wl.add_want(
                state,
                text=text,
                kind=kind,
                source="manual",
                source_ref="",
                now=datetime.now(timezone.utc),
                cap=int(
                    getattr(session._settings.agent, "wants_cap", 8)
                ),
                initial_pressure=float(pressure),
            )
            if not added:
                return json.dumps({
                    "added": False,
                    "reason": "refused (cap reached, empty text, or "
                              "duplicate of an existing want)",
                    "live": len(state.wants),
                })
            chat_db.kv_set(_wl.KV_WANTS_LEDGER, _wl.serialize(new_state))
            return json.dumps({
                "added": True,
                "live": len(new_state.wants),
                "want": {
                    "id": new_state.wants[-1].id,
                    "text": new_state.wants[-1].text,
                    "pressure": new_state.wants[-1].pressure,
                },
            })
        except Exception as exc:
            return f"force_want raised: {exc}"

    @mcp.tool()
    def force_want_imperative() -> str:
        """K52 — arm a one-shot imperative-band bypass.

        The next turn's wants provider renders the strongest live
        want as the imperative directive regardless of its pressure.
        No-op when the ledger is empty.
        """
        try:
            session._wants_force_imperative = True
            return json.dumps({"armed": True})
        except Exception as exc:
            return f"force_want_imperative raised: {exc}"

    @mcp.tool()
    def get_initiative_state() -> str:
        """K53 — dump the initiative-turns director state.

        Returns the master switch, the per-session counters
        (``turns_since_initiative`` / ``session_turn_count``), the
        last decision (fire / reason / effective period), the armed
        force flag, and the settings knobs. The director is lazily
        created on the first evaluated turn — ``null`` counters mean
        no turn has been evaluated yet this session.
        """
        try:
            agent = session._settings.agent
            director = getattr(session, "_initiative_director", None)
            last = getattr(director, "last_decision", None)
            payload = {
                "enabled": bool(
                    getattr(agent, "initiative_turns_enabled", True)
                ),
                "turns_since_initiative": (
                    director.turns_since_initiative
                    if director is not None else None
                ),
                "session_turn_count": (
                    director.session_turn_count
                    if director is not None else None
                ),
                "last_decision": (
                    {
                        "fire": last.fire,
                        "reason": last.reason,
                        "effective_period": last.effective_period,
                    }
                    if last is not None else None
                ),
                "force_armed": bool(
                    getattr(session, "_initiative_force_next", False)
                ),
                "settings": {
                    "base_period": int(
                        getattr(agent, "initiative_base_period", 8)
                    ),
                    "warmup_turns": int(
                        getattr(agent, "initiative_warmup_turns", 3)
                    ),
                    "substantial_chars": int(
                        getattr(agent, "initiative_substantial_chars", 240)
                    ),
                },
            }
            return json.dumps(payload, indent=2)
        except Exception as exc:
            return f"get_initiative_state raised: {exc}"

    @mcp.tool()
    def force_initiative_turn() -> str:
        """K53 — arm a one-shot initiative directive.

        The next turn's provider bypasses every gate except the
        support / reflection arc block and renders the "this turn is
        yours" directive (pointing at the strongest live K52 want
        when one exists). Verify via
        ``get_last_response_detail.system_prompt``.
        """
        try:
            session._initiative_force_next = True
            return json.dumps({"armed": True})
        except Exception as exc:
            return f"force_initiative_turn raised: {exc}"

    @mcp.tool()
    def get_thread_ownership_state() -> str:
        """K55 — dump the opened-thread slot + settings.

        ``owned_thread`` is the topic Aiko opened on her last
        directive turn, awaiting exactly one reply evaluation
        (``null`` when no thread is open — the normal state).
        ``pending_open`` shows a stamp armed at assembly time that
        the post-turn hook hasn't consumed yet (only ever non-null
        mid-turn).
        """
        try:
            agent = session._settings.agent
            thread = getattr(session, "_owned_thread", None)
            pending = getattr(session, "_pending_thread_open", None)
            payload = {
                "enabled": bool(
                    getattr(agent, "thread_ownership_enabled", True)
                ),
                "owned_thread": (
                    {
                        "topic": thread.topic,
                        "source": thread.source,
                        "embedded": thread.embedding is not None,
                        "opened_at": thread.opened_at.isoformat(),
                    }
                    if thread is not None else None
                ),
                "pending_open": pending,
                "settings": {
                    "engaged_chars": int(
                        getattr(agent, "thread_engaged_chars", 80)
                    ),
                    "min_topical_similarity": float(
                        getattr(
                            agent, "thread_min_topical_similarity", 0.30,
                        )
                    ),
                },
            }
            return json.dumps(payload)
        except Exception as exc:
            return f"get_thread_ownership_state raised: {exc}"

    @mcp.tool()
    def force_thread_open(topic: str) -> str:
        """K55 — stamp an owned thread directly (bypasses K53/K52).

        The next user message gets the one-shot engaged-or-pivot
        evaluation against ``topic``: send a short off-topic reply
        and the "circle back" cue should land in
        ``get_last_response_detail.system_prompt``; send an engaged
        reply and the thread clears silently (watch
        ``tail_logs(module_contains="thread")`` for the verdict).
        """
        try:
            from app.core.conversation import thread_ownership as _town

            text = (topic or "").strip()
            if not text:
                return json.dumps({"error": "topic is required"})
            embedding = None
            embedder = getattr(session, "_embedder", None)
            if embedder is not None:
                try:
                    embedding = embedder.embed(text)
                except Exception:
                    embedding = None
            session._owned_thread = _town.OwnedThread(
                topic=_town.derive_topic(text, ""),
                source=_town.SOURCE_FORCED,
                embedding=embedding,
            )
            return json.dumps(
                {
                    "stamped": True,
                    "topic": session._owned_thread.topic,
                    "embedded": embedding is not None,
                }
            )
        except Exception as exc:
            return f"force_thread_open raised: {exc}"

    @mcp.tool()
    def get_topic_appetite_state() -> str:
        """K54 — dump the topic-appetite gate inputs + settings.

        ``lull_mean`` is the K18 standing rolling mean (low =
        circling; ``null`` until the window first fills).
        ``fired_this_session`` is the once-per-conversation latch —
        flip sessions or use ``force_topic_appetite`` to re-arm.
        """
        try:
            agent = session._settings.agent
            detector = getattr(
                session, "_topic_stagnation_detector", None,
            )
            payload = {
                "enabled": bool(
                    getattr(agent, "topic_appetite_enabled", True)
                ),
                "fired_this_session": bool(
                    getattr(session, "_topic_appetite_fired", False)
                ),
                "force_armed": bool(
                    getattr(session, "_topic_appetite_force_next", False)
                ),
                "lull_mean": getattr(detector, "last_mean", None),
                "settings": {
                    "short_reply_chars": int(
                        getattr(agent, "appetite_short_reply_chars", 160)
                    ),
                    "short_share_threshold": float(
                        getattr(
                            agent, "appetite_short_share_threshold", 0.6,
                        )
                    ),
                    "window": int(getattr(agent, "appetite_window", 6)),
                    "min_want_pressure": float(
                        getattr(agent, "appetite_min_want_pressure", 0.35)
                    ),
                    "min_axes": float(
                        getattr(agent, "appetite_min_axes", 0.15)
                    ),
                },
            }
            return json.dumps(payload)
        except Exception as exc:
            return f"get_topic_appetite_state raised: {exc}"

    @mcp.tool()
    def force_topic_appetite() -> str:
        """K54 — arm a one-shot "tapped out" negotiation slip.

        The next turn's provider bypasses every gate except the
        support / reflection arc block and the offer requirement (a
        live K52 want must exist — add one via ``force_want`` first
        if the ledger is empty). Verify via
        ``get_last_response_detail.system_prompt``.
        """
        try:
            session._topic_appetite_force_next = True
            return json.dumps({"armed": True})
        except Exception as exc:
            return f"force_topic_appetite raised: {exc}"

    @mcp.tool()
    def get_affection_style_state() -> str:
        """J11 — dump the learned affection-style weighting.

        Returns the master switch, the learned weight per affection
        kind (touch / teasing / appreciation / words / space, summing
        to ~1.0 from a uniform 0.2 baseline), the current top kind, the
        live bias multiplier each gate would apply right now, the
        previous turn's tagged kinds (next-turn attribution target),
        the reaction→kind confirmation map, and the full settings
        snapshot.

        The weighting is learned primarily from passive K14 engagement
        (no reactions required) and only ever tilts the appreciation /
        tease cooldowns — it is never rendered into a prompt and never
        announced. Repro loop:

        1. ``get_affection_style_state`` — read current weights.
        2. React to a few of Aiko's messages (or call
           ``add_user_reaction``) and/or send warm vs. curt replies.
        3. ``get_affection_style_state`` again — the relevant kind's
           weight should drift; ``force_affection_style_decay`` pulls
           it back toward uniform.
        """
        try:
            from app.core.relationship import affection_style as _af

            agent = session._settings.agent
            chat_db = getattr(session, "_chat_db", None)
            stored = None
            if chat_db is not None:
                try:
                    stored = chat_db.kv_get(_af.KV_AFFECTION_STYLE)
                except Exception:
                    stored = None
            state = _af.deserialize(stored)
            strength = float(
                getattr(agent, "affection_style_bias_strength", 0.5)
            )
            floor = float(getattr(agent, "affection_style_bias_floor", 0.6))
            ceil = float(getattr(agent, "affection_style_bias_ceil", 1.5))
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "affection_style_enabled", True)
                    ),
                    "weights": {
                        k: round(state.weight_of(k), 4)
                        for k in _af.AFFECTION_KINDS
                    },
                    "top_kind": _af.top_kind(state),
                    "updated_at": state.updated_at,
                    "bias_multipliers": {
                        k: round(
                            _af.bias_multiplier(
                                state, k, strength=strength,
                                floor=floor, ceil=ceil,
                            ),
                            4,
                        )
                        for k in _af.AFFECTION_KINDS
                    },
                    "prev_turn_kinds": list(
                        getattr(session, "_prev_affection_kinds", []) or []
                    ),
                    "reaction_to_kind": dict(_af.REACTION_TO_KIND),
                    "settings": {
                        "learning_rate": float(
                            getattr(
                                agent, "affection_style_learning_rate", 0.04,
                            )
                        ),
                        "reaction_weight": float(
                            getattr(
                                agent, "affection_style_reaction_weight", 0.06,
                            )
                        ),
                        "floor": float(
                            getattr(agent, "affection_style_floor", 0.05)
                        ),
                        "decay_half_life_days": float(
                            getattr(
                                agent,
                                "affection_style_decay_half_life_days",
                                30.0,
                            )
                        ),
                        "bias_strength": strength,
                        "bias_floor": floor,
                        "bias_ceil": ceil,
                        "decay_interval_seconds": int(
                            getattr(
                                agent,
                                "affection_style_decay_interval_seconds",
                                21600,
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_affection_style_state raised: {exc}"

    @mcp.tool()
    def set_affection_style(kind: str, weight: float) -> str:
        """J11 — force one affection kind's raw weight, then renormalise.

        Test helper to push the weighting into a known shape without
        replaying turns. ``kind`` must be one of touch / teasing /
        appreciation / words / space; ``weight`` is the pre-normalise
        raw share (the other kinds keep their current shares and the
        whole vector is re-floored + renormalised). Returns the new
        weights.
        """
        try:
            from datetime import datetime, timezone

            from app.core.relationship import affection_style as _af

            norm = (kind or "").strip().lower()
            if norm not in _af.AFFECTION_KINDS:
                return json.dumps(
                    {"error": "unknown kind", "kinds": list(_af.AFFECTION_KINDS)}
                )
            agent = session._settings.agent
            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return "chat_db not available"
            state = _af.deserialize(chat_db.kv_get(_af.KV_AFFECTION_STYLE))
            raw = {k: state.weight_of(k) for k in _af.AFFECTION_KINDS}
            raw[norm] = max(0.0, float(weight))
            # Floor + renormalise (same posture as the pure module's
            # internal _normalise). Done inline here so the debug tool
            # doesn't reach into a private helper.
            floor = max(
                0.0,
                min(
                    1.0 / len(_af.AFFECTION_KINDS),
                    float(getattr(agent, "affection_style_floor", 0.05)),
                ),
            )
            floored = {k: max(floor, raw[k]) for k in _af.AFFECTION_KINDS}
            total = sum(floored.values()) or 1.0
            persisted = _af.AffectionStyleState(
                weights={k: v / total for k, v in floored.items()},
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            chat_db.kv_set(_af.KV_AFFECTION_STYLE, _af.serialize(persisted))
            return json.dumps(
                {
                    "weights": {
                        k: round(persisted.weight_of(k), 4)
                        for k in _af.AFFECTION_KINDS
                    },
                    "top_kind": _af.top_kind(persisted),
                },
                indent=2,
            )
        except Exception as exc:
            return f"set_affection_style raised: {exc}"

    @mcp.tool()
    def reset_affection_style() -> str:
        """J11 — wipe the learned weighting back to uniform."""
        try:
            from app.core.relationship import affection_style as _af

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return "chat_db not available"
            chat_db.kv_set(
                _af.KV_AFFECTION_STYLE, _af.serialize(_af.uniform_state()),
            )
            return json.dumps({"reset": True})
        except Exception as exc:
            return f"reset_affection_style raised: {exc}"

    @mcp.tool()
    def force_affection_style_decay() -> str:
        """J11 — run the AffectionStyleDecayWorker once, ignoring gates.

        Pulls the learned weights one step toward uniform per the
        configured half-life. Returns the run result (``decayed`` /
        ``top`` or a skip reason).
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return "scheduler not running"
        try:
            result = sched.force_run("affection_style_decay")
        except KeyError:
            return (
                "affection_style_decay worker not registered "
                "(agent.affection_style_enabled may be off)"
            )
        except Exception as exc:
            return f"force_affection_style_decay raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)


