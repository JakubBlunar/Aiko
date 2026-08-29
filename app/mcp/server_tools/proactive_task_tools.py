from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_turning_over_state() -> str:
        """K28 — dump the in-memory turning-over picker state.

        Returns a JSON dict with the master switch, the current
        pending-seconds slot (set by the post-turn engagement
        tracker when a long enough typed gap was observed), the
        ``force_next`` flag (armed by ``force_turning_over``),
        the most recent fire (``memory_id`` / ``age_hours`` /
        ``topical_score`` / ``topical_source`` / ``dream`` /
        truncated ``content``), the settings snapshot (5 knobs),
        plus a **dry-run picker result** that calls the picker
        against the *current* memory state without arming the
        cue -- so you can see what *would* surface on the next
        qualifying turn even when the slot isn't currently armed.

        The dry-run respects the configured age window and the
        topical-similarity threshold, so a ``would_surface: null``
        with ``reflections_in_window: N > 0`` means the threshold
        gate is rejecting every candidate.

        Pairs with ``force_turning_over`` for the end-to-end repro:

        1. Call ``get_turning_over_state`` first -- read
           ``would_surface`` to confirm there's a candidate that
           clears the gates.
        2. Call ``force_turning_over`` to arm the one-shot bypass.
        3. Send a message; verify ``tail_logs(module_contains=
           "turning_over")`` shows ``turning-over fire: ...``.
        4. Call ``get_turning_over_state`` again -- ``force_next``
           should be ``false`` (consumed), ``last_fire`` populated.
        """
        try:
            agent = session._settings.agent
            memory = session._memory_settings
            pending_s = getattr(
                session, "_pending_turning_over_seconds", None,
            )
            force_next = bool(
                session.debug_overrides.peek("turning_over_force_next", False),
            )
            last = getattr(session, "_last_turning_over", None)
            last_payload = None
            if last is not None:
                last_payload = {
                    "memory_id": int(getattr(last, "memory_id", 0) or 0),
                    "age_hours": float(getattr(last, "age_hours", 0.0)),
                    "topical_score": float(
                        getattr(last, "topical_score", 0.0)
                    ),
                    "topical_source": str(
                        getattr(last, "topical_source", "") or ""
                    ),
                    "dream": bool(getattr(last, "dream", False)),
                    "content": (
                        (getattr(last, "content", "") or "")[:200]
                    ),
                }

            # Dry-run: pick a candidate against the current memory
            # state without arming the cue. Mirrors the live provider's
            # picker call so what we show here is what would land.
            dry_run = None
            reflections_in_window = 0
            try:
                from app.core.session.inner_life import turning_over as _to

                memory_store = getattr(session, "_memory_store", None)
                if memory_store is not None:
                    reflections = list(memory_store.iter_by_kind("reflection"))
                    # Count rows in the age window for diagnostic.
                    now = timephrase.utcnow()
                    min_age = float(
                        getattr(
                            memory,
                            "turning_over_min_age_hours",
                            _to.DEFAULT_MIN_AGE_HOURS,
                        )
                    )
                    max_age = float(
                        getattr(
                            memory,
                            "turning_over_max_age_hours",
                            _to.DEFAULT_MAX_AGE_HOURS,
                        )
                    )
                    for mem in reflections:
                        age = _to._parse_age_hours(
                            getattr(mem, "created_at", None), now=now,
                        )
                        if age is None:
                            continue
                        if min_age <= age <= max_age:
                            reflections_in_window += 1
                    goal_store = getattr(session, "_goal_store", None)
                    goal_vecs = []
                    if goal_store is not None:
                        try:
                            goal_vecs = list(goal_store.active_goal_vectors())
                        except Exception:
                            goal_vecs = []
                    msg_vecs = []
                    rag_store = getattr(session, "_rag_store", None)
                    msgs_window = int(
                        getattr(
                            memory,
                            "turning_over_recent_msgs_window",
                            12,
                        )
                    )
                    if rag_store is not None and msgs_window > 0:
                        try:
                            msg_vecs = list(
                                rag_store.list_recent_user_vectors(
                                    user_id_prefix=(
                                        getattr(session, "_user_id", "") or ""
                                    ),
                                    limit=msgs_window,
                                )
                            )
                        except Exception:
                            msg_vecs = []
                    picked = _to.pick_turning_over(
                        reflections=reflections,
                        active_goal_vecs=goal_vecs,
                        recent_user_vecs=msg_vecs,
                        now=now,
                        min_age_hours=min_age,
                        max_age_hours=max_age,
                        min_topical_similarity=float(
                            getattr(
                                memory,
                                "turning_over_min_topical_similarity",
                                _to.DEFAULT_MIN_TOPICAL_SIMILARITY,
                            )
                        ),
                    )
                    if picked is not None:
                        dry_run = {
                            "memory_id": int(picked.memory_id),
                            "age_hours": float(picked.age_hours),
                            "topical_score": float(picked.topical_score),
                            "topical_source": picked.topical_source,
                            "dream": bool(picked.dream),
                            "content": (picked.content or "")[:200],
                        }
            except Exception as dry_exc:
                dry_run = {"error": str(dry_exc)}

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "turning_over_enabled", True)
                    ),
                    "pending_seconds": (
                        float(pending_s) if pending_s is not None else None
                    ),
                    "force_next": force_next,
                    "last_fire": last_payload,
                    "would_surface": dry_run,
                    "reflections_in_window": reflections_in_window,
                    "settings": {
                        "min_gap_minutes": float(
                            getattr(
                                memory,
                                "turning_over_min_gap_minutes",
                                90.0,
                            )
                        ),
                        "min_age_hours": float(
                            getattr(
                                memory,
                                "turning_over_min_age_hours",
                                24.0,
                            )
                        ),
                        "max_age_hours": float(
                            getattr(
                                memory,
                                "turning_over_max_age_hours",
                                72.0,
                            )
                        ),
                        "min_topical_similarity": float(
                            getattr(
                                memory,
                                "turning_over_min_topical_similarity",
                                0.30,
                            )
                        ),
                        "recent_msgs_window": int(
                            getattr(
                                memory,
                                "turning_over_recent_msgs_window",
                                12,
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_turning_over_state raised: {exc}"

    @mcp.tool()
    def force_turning_over() -> str:
        """K28 — arm a one-shot bypass on the turning-over gap gate.

        Sets ``_turning_over_force_next`` so the next call to the
        provider treats the pending-slot gate AND the threshold
        double-check as bypassed. The picker still runs, so a
        forced bypass on an empty reflection corpus (or one where
        nothing clears the topical-similarity gate) silently
        expires with no cue. Bypass is consumed regardless --
        strictly one-turn.

        Repro recipe:

        1. Make sure Aiko has at least one ``kind="reflection"``
           memory row written between 24h and 72h ago. Real
           reflections come from the post-turn ``ReflectionWorker``
           or ``DreamWorker``; for testing, you can insert one via
           ``POST /api/memories`` with ``kind=reflection`` and a
           ``created_at`` 30h in the past.
        2. Call ``get_turning_over_state`` -- confirm
           ``would_surface`` is non-null (i.e. there's a candidate
           that clears the gates).
        3. Call this tool.
        4. Send a message; check ``tail_logs(module_contains=
           "turning_over")`` for ``turning-over fire: memory_id=...``.
        5. Aiko's reply should fold in the reflection as a casual
           aside, not as an announcement.
        """
        try:
            session.debug_overrides.arm("turning_over_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will ignore the pending-slot "
                        "gate AND the threshold double-check; picker still "
                        "runs, so an empty reflection corpus or a "
                        "below-threshold candidate silently expires"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_turning_over raised: {exc}"

    @mcp.tool()
    def get_away_activities_state() -> str:
        """K36 — dump the idle away-activity worker + surfacing state.

        Returns a JSON dict with:

        - ``enabled``: ``agent.away_activities_enabled`` master switch.
        - ``worker_registered``: whether the IdleAwayActivityWorker
          actually wired up (needs a loaded WorldStore + idle scheduler).
        - ``pending_seconds`` / ``force_next``: the surfacing slot armed
          by the post-turn tracker on a long typed gap, and the MCP
          one-shot bypass flag.
        - ``min_gap_hours``: the typed-absence threshold the provider
          gates on.
        - ``journal``: the kv ring of recent activities (newest last).
        - ``last_surfaced_at``: watermark of the last journal entry the
          provider folded into a reply.
        """
        try:
            from app.core.world.idle_activity_worker import (
                load_journal,
                _KV_LAST_FIRED_AT,
                _KV_DAY,
                _KV_DAY_COUNT,
            )

            kv = session._chat_db.kv_get
            journal = load_journal(kv)
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "away_activities_enabled",
                            True,
                        )
                    ),
                    "worker_registered": getattr(
                        session, "_away_activity_worker", None
                    )
                    is not None,
                    "pending_seconds": getattr(
                        session, "_pending_away_activities_seconds", None
                    ),
                    "force_next": bool(
                        session.debug_overrides.peek("away_activities_force_next", False)
                    ),
                    "min_gap_hours": float(
                        getattr(
                            session._memory_settings,
                            "away_activities_min_gap_hours",
                            4.0,
                        )
                    ),
                    "journal": journal,
                    "last_surfaced_at": kv("away_activity.last_surfaced_at"),
                    "last_fired_at": kv(_KV_LAST_FIRED_AT),
                    "day": kv(_KV_DAY),
                    "day_count": kv(_KV_DAY_COUNT),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_away_activities_state raised: {exc}"

    @mcp.tool()
    def force_away_activity(key: str = "") -> str:
        """K36 — run the idle away-activity worker once, right now.

        Bypasses the worker's cooldown + daily-cap + quiet-window gates
        by calling ``run()`` directly, so it mutates the world and
        appends a fresh journal entry immediately. Pass ``key`` to force
        a specific activity (``snack`` / ``read_book`` / ``move_cat`` /
        ``look_outside`` / ``tidy_desk`` / ``doodle`` / ``wander``);
        leave blank for a random pick from what's in the room.

        Pairs with ``force_away_activities_surface`` for the end-to-end
        repro: call this to produce a journal entry, then that to make
        the next turn fold it into Aiko's reply.
        """
        try:
            worker = getattr(session, "_away_activity_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no WorldStore?)"},
                    indent=2,
                )
            if key:
                worker.force_activity(key)
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_away_activity raised: {exc}"

    @mcp.tool()
    def force_away_activities_surface() -> str:
        """K36 — arm a one-shot bypass on the away-activities gates.

        Sets ``_away_activities_force_next`` so the next provider call
        ignores the pending-slot gate, the gap-threshold double-check,
        the one-of ``turning_over`` guard, AND the last-surfaced
        watermark. The journal still has to be non-empty (run
        ``force_away_activity`` first if it isn't). Bypass is consumed
        on the next assembly regardless.

        Repro: ``force_away_activity()`` -> ``force_away_activities_
        surface()`` -> ``send_message(skip_tts=true)`` -> confirm the
        "While ... was away, you ..." line in
        ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("away_activities_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the slot, threshold, "
                        "turning_over guard, and watermark; journal must "
                        "be non-empty or the cue silently expires"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_away_activities_surface raised: {exc}"

    @mcp.tool()
    def get_garden_visit_state() -> str:
        """H15 — dump the GardenVisitWorker gate state.

        Shows the master switch, whether she's currently in the garden,
        the live need-driven trigger (a drought-stressed or ripe plant),
        the kv watermarks (return_at / next_eligible / last_visit), and
        the H15 knobs (relax ratio, dry-days threshold, visit jitter).
        """
        try:
            worker = getattr(session, "_garden_visit_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no WorldStore?)"},
                    indent=2,
                )
            return json.dumps(worker.debug_state(), indent=2, default=str)
        except Exception as exc:
            return f"get_garden_visit_state raised: {exc}"

    @mcp.tool()
    def force_garden_visit() -> str:
        """H15 — run the GardenVisitWorker once, bypassing the gates.

        Arms a one-shot bypass of the daylight + cooldown gates, then calls
        ``run()`` directly. On the outbound leg she walks to the garden
        (tending or relaxing) and a fresh entry lands in the shared
        away-activities journal — pair with ``force_away_activities_
        surface`` to make the next turn mention "I was out in the garden".
        Call again after the visit duration to walk her back home.
        """
        try:
            worker = getattr(session, "_garden_visit_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no WorldStore?)"},
                    indent=2,
                )
            worker.force_visit()
            result = worker.run()
            return json.dumps(
                {"ran": True, "result": result}, indent=2, default=str,
            )
        except Exception as exc:
            return f"force_garden_visit raised: {exc}"

    @mcp.tool()
    def force_outing() -> str:
        """H22 — force a rare "I stepped out for a bit" away-beat now.

        Arms the outing key on the away-activity worker (so its own
        daylight + cooldown + daily-cap gates are bypassed when the beat
        is offered) and calls ``run()`` once. A past-tense outing line
        lands in the away-activities journal; pair with
        ``force_away_activities_surface`` for the end-to-end repro.
        """
        try:
            worker = getattr(session, "_away_activity_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no WorldStore?)"},
                    indent=2,
                )
            worker.force_activity("outing")
            result = worker.run()
            state = (
                worker.outing_debug_state()
                if hasattr(worker, "outing_debug_state")
                else {}
            )
            return json.dumps(
                {"ran": True, "result": result, "outing_state": state},
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"force_outing raised: {exc}"

    @mcp.tool()
    def force_away_beat(activity_key: str = "") -> str:
        """K91 — run one away beat now and show what it changed.

        Bypasses the cooldown/cap gates by calling ``run()`` directly.
        Pass an ``activity_key`` (``tea``, ``read_book``, ``snack``,
        ``tidy_desk``, ``doodle``, ``look_outside``, ``nap``, ``move_cat``,
        ``wander``) to force a specific beat, or leave it blank to let the
        weighted draw choose. The result carries ``episode`` when beats
        chained, ``item_effect`` for state written back (chapter advanced,
        pot emptied, plant watered) and ``closed_intention`` when the beat
        satisfied the day's intention. Pair with
        ``force_away_activities_surface`` for the end-to-end repro.
        """
        try:
            worker = getattr(session, "_away_activity_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no WorldStore?)"},
                    indent=2,
                )
            if activity_key.strip():
                worker.force_activity(activity_key.strip())
            result = worker.run()
            intention = (
                worker.day_intention_debug_state()
                if hasattr(worker, "day_intention_debug_state")
                else {}
            )
            return json.dumps(
                {"ran": True, "result": result, "day_intention": intention},
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"force_away_beat raised: {exc}"

    @mcp.tool()
    def get_idle_seed_state() -> str:
        """H17 — dump the idle-seed ring + surfacing watermarks.

        Idle beats occasionally turn into a forward-looking conversational
        seed (LLM-composed, daily-capped) that surfaces ONCE as a private
        inner-life cue ("while I was reading earlier I started wondering
        ...") so Aiko phrases it herself. This shows the ring, the master
        switch, and both surfacing watermarks (per-seed + cooldown clock).
        """
        try:
            from app.core.world.idle_activity_worker import (
                _KV_SEED_DAY,
                _KV_SEED_DAY_COUNT,
                load_idle_seeds,
            )

            chat_db = getattr(session, "_chat_db", None)

            def kv(k: str):
                try:
                    return chat_db.kv_get(k) if chat_db else None
                except Exception:
                    return None

            ring = load_idle_seeds(chat_db.kv_get) if chat_db else []
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent, "idle_seed_enabled", True
                        )
                    ),
                    "ratio": float(
                        getattr(
                            session._memory_settings, "idle_seed_ratio", 0.25
                        )
                    ),
                    "daily_cap": int(
                        getattr(
                            session._memory_settings, "idle_seed_daily_cap", 3
                        )
                    ),
                    "surface_cooldown_seconds": int(
                        getattr(
                            session._memory_settings,
                            "idle_seed_surface_cooldown_seconds",
                            1800,
                        )
                    ),
                    "force_next": bool(
                        session.debug_overrides.peek("idle_seed_force_next", False)
                    ),
                    "ring": ring,
                    "surfaced_at": kv("idle_seed.surfaced_at"),
                    "surfaced_clock": kv("idle_seed.surfaced_clock"),
                    "seed_day": kv(_KV_SEED_DAY),
                    "seed_day_count": kv(_KV_SEED_DAY_COUNT),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_idle_seed_state raised: {exc}"

    @mcp.tool()
    def force_idle_seed_surface() -> str:
        """H17 — arm a one-shot bypass on the idle-seed surfacing gates.

        Sets ``_idle_seed_force_next`` so the next provider call ignores
        both the per-seed watermark and the wall-clock surfacing cooldown.
        The ring still has to be non-empty (run ``force_away_activity``
        with a worker model configured to produce one, or insert a row
        directly). Bypass is consumed on the next assembly.

        Repro: produce a seed -> ``force_idle_seed_surface()`` ->
        ``send_message(skip_tts=true)`` -> confirm the "Earlier, while you
        were ... a thought crossed your mind: ..." line in
        ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("idle_seed_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the per-seed watermark "
                        "and the surfacing cooldown; ring must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_idle_seed_surface raised: {exc}"

    @mcp.tool()
    def get_sleep_return_state() -> str:
        """H21 — dump the overnight-sleep return-cue state.

        On return from a long typed gap that plausibly spanned an overnight
        sleep, Aiko surfaces ONE casual "I actually dozed off ..." line
        (optionally weaving in a recent ``[dream]`` reflection). This shows
        the master switch, the gap/overnight/dream-lookback thresholds, the
        pending-slot value, the force flag, and the last fire's diagnostics.
        """
        try:
            agent = session._settings.agent
            ms = session._memory_settings
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "sleep_return_enabled", True)
                    ),
                    "min_gap_hours": float(
                        getattr(ms, "sleep_return_min_gap_hours", 5.0)
                    ),
                    "overnight_hours": float(
                        getattr(ms, "sleep_return_overnight_hours", 9.0)
                    ),
                    "dream_lookback_hours": float(
                        getattr(ms, "sleep_return_dream_lookback_hours", 18.0)
                    ),
                    "pending_seconds": getattr(
                        session, "_pending_sleep_return_seconds", None
                    ),
                    "force_next": bool(
                        session.debug_overrides.peek("sleep_return_force_next", False)
                    ),
                    "last_fire": getattr(session, "_last_sleep_return", None),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_sleep_return_state raised: {exc}"

    @mcp.tool()
    def force_sleep_return_surface() -> str:
        """H21 — arm a one-shot bypass on the sleep-return gates.

        Sets ``_sleep_return_force_next`` so the next provider call ignores
        the pending-slot gate, the gap-threshold check, the return-hour
        overnight gate, AND the one-of ``turning_over`` guard. A recent
        ``[dream]`` reflection (within ``sleep_return_dream_lookback_hours``)
        is still woven in only if one exists. Bypass is consumed on the next
        assembly regardless.

        Repro: ``force_sleep_return_surface()`` -> ``send_message(
        skip_tts=true)`` -> confirm the "While ... was away you actually
        dozed off ..." line in ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("sleep_return_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the slot, gap threshold, "
                        "overnight gate, and turning_over guard; a recent "
                        "[dream] reflection is woven in if one exists"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_sleep_return_surface raised: {exc}"

    @mcp.tool()
    def get_in_progress_beat() -> str:
        """H26 — is she in the middle of an away activity right now?

        An away beat is normally finished and journalled before he comes
        back. A minority (``memory.away_activities_in_progress_ratio``)
        are left *running* instead, with a wall-clock span, so a return
        inside that window catches her mid-something. This shows the open
        beat (if any), how far into it she is, how long is left, and
        whether a return already interrupted it — plus the ratio and the
        force flag.

        ``open`` false with a beat present means the window elapsed or a
        return interrupted it; either way the worker's next tick closes
        it out into the journal.
        """
        try:
            return json.dumps(
                session.in_progress_beat_state(), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_in_progress_beat raised: {exc}"

    @mcp.tool()
    def force_caught_mid_activity(activity_key: str = "") -> str:
        """H26 — leave a beat running and arm the return cue.

        Two steps in one, because the interesting state is hard to catch
        by hand: forces one away beat to be left *open* (rather than
        finished and journalled), then arms
        ``caught_mid_activity_force_next`` so the next assembly surfaces
        it regardless of the one-of gap-cue guard and the open-window
        check.

        Repro: ``force_caught_mid_activity("read_book")`` ->
        ``send_message(skip_tts=true)`` -> confirm the "You are in the
        middle of ..." line in ``get_last_response_detail.system_prompt``,
        then ``get_in_progress_beat()`` to see it marked interrupted, and
        ``force_away_beat()`` to watch the worker close it out with a
        "went back to it" journal entry.
        """
        try:
            return json.dumps(
                session.force_caught_mid_activity(activity_key),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"force_caught_mid_activity raised: {exc}"

    @mcp.tool()
    def get_hobby_state() -> str:
        """H19 — dump Aiko's current hobby / ongoing-project state.

        Shows the master switch, the cadence knobs, and the live
        ``aiko.current_hobby`` kv blob (label, progress, advances,
        started_at). The standing line is rendered into the prompt by
        ``_render_hobby_block``; takeaways surface through the H17 cue.
        """
        try:
            from app.core.proactive.hobby_worker import load_hobby

            chat_db = getattr(session, "_chat_db", None)
            state = load_hobby(chat_db.kv_get) if chat_db else None
            mem = session._memory_settings
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "hobby_worker_enabled",
                            True,
                        )
                    ),
                    "worker_registered": getattr(
                        session, "_hobby_worker", None
                    )
                    is not None,
                    "interval_seconds": int(
                        getattr(mem, "hobby_worker_interval_seconds", 3600)
                    ),
                    "advance_min_hours": float(
                        getattr(mem, "hobby_advance_min_hours", 6.0)
                    ),
                    "milestone_every": int(
                        getattr(mem, "hobby_milestone_every", 3)
                    ),
                    "max_advances": int(
                        getattr(mem, "hobby_max_advances", 12)
                    ),
                    "current": state,
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_hobby_state raised: {exc}"

    @mcp.tool()
    def force_hobby_advance() -> str:
        """H19 — advance the current hobby once, right now.

        Bypasses the wall-clock advance pacing by arming a one-shot flag,
        then calls ``run()`` directly so progress climbs immediately and a
        milestone seed lands if this advance hits the cadence. Starts a
        hobby first if none exists yet.
        """
        try:
            worker = getattr(session, "_hobby_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered"}, indent=2
                )
            worker._force_advance = True
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_hobby_advance raised: {exc}"

    @mcp.tool()
    def force_hobby_rotate() -> str:
        """H19 — rotate to a fresh hobby right now.

        Arms a one-shot rotate flag and calls ``run()`` so the current
        thread wraps up (emitting a "finished X, starting Y" seed via the
        H17 cue) and a new hobby begins. No-op message if no hobby exists
        yet (call ``force_hobby_advance`` first to start one).
        """
        try:
            worker = getattr(session, "_hobby_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered"}, indent=2
                )
            worker._force_rotate = True
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_hobby_rotate raised: {exc}"

    @mcp.tool()
    def get_room_evolution_state() -> str:
        """H20 — dump the room-evolution worker state + tracked item states.

        Shows the master switch, cadence knobs, the wall-clock gate stamp,
        and the live ``state`` of the three drifting items (tea pot, cookie
        jar, sci-fi paperback) so you can watch the room accrue history.
        """
        try:
            from app.core.world.room_evolution import (
                BOOK_SLUG,
                COOKIE_JAR_SLUG,
                TEA_POT_SLUG,
            )
            from app.core.world.room_evolution_worker import (
                KV_LAST_EVOLVED_AT,
            )

            chat_db = getattr(session, "_chat_db", None)
            world = getattr(session, "_world_store", None)
            mem = session._memory_settings

            items: dict[str, Any] = {}
            if world is not None:
                by_slug = {i.slug: i for i in world.list_items()}
                for slug in (TEA_POT_SLUG, COOKIE_JAR_SLUG, BOOK_SLUG):
                    it = by_slug.get(slug)
                    items[slug] = (
                        {
                            "name": it.name,
                            "quantity": it.quantity,
                            "state": it.state,
                        }
                        if it is not None
                        else None
                    )

            def kv(k: str):
                try:
                    return chat_db.kv_get(k) if chat_db else None
                except Exception:
                    return None

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "room_evolution_enabled",
                            True,
                        )
                    ),
                    "worker_registered": getattr(
                        session, "_room_evolution_worker", None
                    )
                    is not None,
                    "interval_seconds": int(
                        getattr(mem, "room_evolution_interval_seconds", 21600)
                    ),
                    "min_hours": float(
                        getattr(mem, "room_evolution_min_hours", 8.0)
                    ),
                    "last_evolved_at": kv(KV_LAST_EVOLVED_AT),
                    "items": items,
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_room_evolution_state raised: {exc}"

    @mcp.tool()
    def force_room_evolution() -> str:
        """H20 — apply one room-evolution step right now.

        Bypasses the wall-clock min-gap by arming a one-shot flag, then
        calls ``run()`` so one applicable item drifts immediately (tea pot
        level, cookie refill, or a book chapter — finishing a book emits an
        H17 seed). Broadcasts the ``world_updated`` patch to every window.
        """
        try:
            worker = getattr(session, "_room_evolution_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no WorldStore?)"},
                    indent=2,
                )
            worker._force = True
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_room_evolution raised: {exc}"

    @mcp.tool()
    def get_diary_worker_state() -> str:
        """H9 — dump the away-diary worker state.

        Returns a JSON dict with the master switch, whether the worker
        registered (needs a MemoryStore + embedder), the live
        ``away`` reading (``True`` == no UI client connected, which is
        the gate the worker fires on), whether a worker LLM is wired,
        the cadence knobs, the cooldown / daily-cap watermarks, and the
        current recent-context length. ``away=False`` means a UI client
        is connected and the worker is deferring to the live
        ``[[diary:...]]`` tag.
        """
        try:
            worker = getattr(session, "_diary_worker", None)
            if worker is None:
                return json.dumps(
                    {
                        "enabled": bool(
                            getattr(
                                session._settings.agent,
                                "diary_worker_enabled",
                                True,
                            )
                        ),
                        "worker_registered": False,
                        "connected_clients": int(
                            getattr(session, "_connected_clients", 0)
                        ),
                    },
                    indent=2,
                )
            state = worker.state()
            state["worker_registered"] = True
            state["connected_clients"] = int(
                getattr(session, "_connected_clients", 0)
            )
            return json.dumps(state, indent=2)
        except Exception as exc:
            return f"get_diary_worker_state raised: {exc}"

    @mcp.tool()
    def force_diary_entry() -> str:
        """H9 — run the away-diary worker once, right now.

        Arms a one-shot bypass of the away / cooldown / daily-cap gates
        (the recent-context + worker-LLM requirements still apply) and
        calls ``run()`` directly. On success a fresh ``diary`` memory is
        written and appears in the Diary tab. Use this to verify the
        away-journal path end-to-end without having to close every UI
        window and wait for a quiet tick.
        """
        try:
            worker = getattr(session, "_diary_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no MemoryStore/embedder?)"},
                    indent=2,
                )
            worker.force_next()
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_diary_entry raised: {exc}"

    @mcp.tool()
    def get_forward_curiosity_state() -> str:
        """K34 — dump the forward-curiosity worker + surfacing state.

        Returns a JSON dict with:

        - ``enabled``: ``agent.forward_curiosity_enabled`` master switch.
        - ``worker_registered``: whether the ForwardCuriosityWorker wired
          up (needs a loaded MemoryStore + idle scheduler).
        - ``pending_seconds`` / ``force_next``: the surfacing slot armed
          by the post-turn tracker on a long typed gap, and the MCP
          one-shot bypass flag.
        - ``min_gap_hours``: the typed-absence threshold the provider
          gates on.
        - ``questions``: the kv ring of drafted questions (newest last).
        - ``last_surfaced_at``: watermark of the last question the
          provider folded into a reply.
        """
        try:
            from app.core.proactive.forward_curiosity_worker import (
                load_questions,
                _KV_LAST_FIRED_AT,
                _KV_DAY,
                _KV_DAY_COUNT,
            )

            kv = session._chat_db.kv_get
            ring = load_questions(kv)
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "forward_curiosity_enabled",
                            True,
                        )
                    ),
                    "worker_registered": getattr(
                        session, "_forward_curiosity_worker", None
                    )
                    is not None,
                    "pending_seconds": getattr(
                        session, "_pending_forward_curiosity_seconds", None
                    ),
                    "force_next": bool(
                        session.debug_overrides.peek("forward_curiosity_force_next", False)
                    ),
                    "min_gap_hours": float(
                        getattr(
                            session._memory_settings,
                            "forward_curiosity_min_gap_hours",
                            4.0,
                        )
                    ),
                    "questions": ring,
                    "last_surfaced_at": kv(
                        "forward_curiosity.last_surfaced_at"
                    ),
                    "last_fired_at": kv(_KV_LAST_FIRED_AT),
                    "day": kv(_KV_DAY),
                    "day_count": kv(_KV_DAY_COUNT),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_forward_curiosity_state raised: {exc}"

    @mcp.tool()
    def force_forward_curiosity_draft(source_id: str = "") -> str:
        """K34 — run the forward-curiosity worker once, right now.

        Bypasses the worker's cooldown + daily-cap + quiet-window gates
        by calling ``run()`` directly, so it drafts a fresh question and
        appends it to the ring immediately. Pass ``source_id`` to force a
        specific memory (a ``future_plan`` or ``callback`` row id) as the
        topic; leave blank for a random pick among undrafted candidates.

        Pairs with ``force_forward_curiosity_surface`` for the end-to-end
        repro: call this to produce a question, then that to make the
        next turn fold it into Aiko's reply.
        """
        try:
            worker = getattr(session, "_forward_curiosity_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no MemoryStore?)"},
                    indent=2,
                )
            if source_id:
                worker.force_source(source_id)
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_forward_curiosity_draft raised: {exc}"

    @mcp.tool()
    def force_forward_curiosity_surface() -> str:
        """K34 — arm a one-shot bypass on the forward-curiosity gates.

        Sets ``_forward_curiosity_force_next`` so the next provider call
        ignores the pending-slot gate, the gap-threshold double-check,
        the one-of {turning_over, away_activities} guard, AND the
        last-surfaced watermark. The ring still has to be non-empty (run
        ``force_forward_curiosity_draft`` first if it isn't). Bypass is
        consumed on the next assembly regardless.

        Repro: ``force_forward_curiosity_draft()`` ->
        ``force_forward_curiosity_surface()`` ->
        ``send_message(skip_tts=true)`` -> confirm the "You've been
        wondering ..." line in ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("forward_curiosity_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the slot, threshold, "
                        "one-of guard, and watermark; ring must be "
                        "non-empty or the cue silently expires"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_forward_curiosity_surface raised: {exc}"

    @mcp.tool()
    def get_follow_up_state() -> str:
        """Dump the follow-up cue worker + surfacing state.

        The FollowUpWorker drafts a "you can ask how their plan went" cue
        into the ``aiko.follow_up_cues`` kv ring when a user-mentioned
        ``future_plan`` event time passes; ``_render_follow_up_block``
        surfaces the newest unseen cue on the next turn (watermark-gated).
        It is NEVER spoken verbatim — Aiko phrases the check-in herself.

        Returns a JSON dict with the master switch, whether the worker
        wired up, the MCP force-next flag, the cue ring, and the
        last-surfaced watermark.
        """
        try:
            from app.core.proactive.follow_up_worker import (
                load_follow_up_cues,
            )

            def kv(key: str) -> str | None:
                try:
                    return session._chat_db.kv_get(key)
                except Exception:
                    return None

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "follow_up_enabled",
                            True,
                        )
                    ),
                    "worker_registered": getattr(
                        session, "_follow_up_worker", None
                    )
                    is not None,
                    "force_next": bool(
                        session.debug_overrides.peek("follow_up_force_next", False)
                    ),
                    "cues": load_follow_up_cues(session._chat_db.kv_get),
                    "last_surfaced_at": kv("follow_up.last_surfaced_at"),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_follow_up_state raised: {exc}"

    @mcp.tool()
    def force_follow_up_draft(source_id: str = "") -> str:
        """Run the follow-up worker once, right now.

        Bypasses the quiet-window gate by calling ``run()`` directly so a
        cue is drafted into the ring immediately. Pass ``source_id`` (a
        ``future_plan`` memory id) to force that specific plan regardless
        of its event-time window / already-fired gate; leave blank to use
        the normal time-window scan.

        Pairs with ``force_follow_up_surface`` for the end-to-end repro:
        draft a cue, then make the next turn fold it into the prompt.
        """
        try:
            worker = getattr(session, "_follow_up_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no MemoryStore?)"},
                    indent=2,
                )
            if source_id:
                worker.force_source(source_id)
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_follow_up_draft raised: {exc}"

    @mcp.tool()
    def force_follow_up_surface() -> str:
        """Arm a one-shot bypass on the follow-up cue watermark.

        Sets ``_follow_up_force_next`` so the next provider call ignores
        the last-surfaced watermark. The ring still has to be non-empty
        (run ``force_follow_up_draft`` first if it isn't). Bypass is
        consumed on the next assembly.

        Repro: ``force_follow_up_draft(source_id=...)`` ->
        ``force_follow_up_surface()`` -> ``send_message(skip_tts=true)``
        -> confirm the "Earlier ... you can gently ask how it went" line
        in ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("follow_up_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the follow-up watermark; "
                        "ring must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_follow_up_surface raised: {exc}"

    @mcp.tool()
    def get_growth_witness_state() -> str:
        """K70 — dump the growth-witness worker + surfacing state.

        The GrowthWitnessWorker compares an older baseline window of the
        H3 mood-drift ring against a recent window and, when a durable
        POSITIVE shift clears a high bar, drafts a "you've grown since we
        met" cue into ``aiko.growth_witness``; ``_render_growth_witness_
        block`` surfaces the newest unseen finding on a later turn
        (watermark-gated). It is NEVER spoken verbatim.

        Returns a JSON dict with the master switch, cadence + cooldown,
        whether the worker wired up, the MCP force-next flag, the finding
        ring, the producer pacing watermarks, the surfacing watermark, and
        a dry-run detection over the current mood-drift ring.
        """
        try:
            from app.core.affect import mood_drift as _md
            from app.core.relationship import growth_witness as _gw

            agent = session._settings.agent
            mem = session._memory_settings

            def kv(key: str) -> str | None:
                try:
                    return session._chat_db.kv_get(key)
                except Exception:
                    return None

            samples = _md.deserialize_samples(kv(_md.KV_SAMPLES))
            dry = _gw.detect_growth(
                samples,
                min_samples=int(
                    getattr(mem, "growth_witness_min_samples", 10)
                ),
                min_valence_delta=float(
                    getattr(mem, "growth_witness_min_valence_delta", 0.25)
                ),
                min_axis_delta=float(
                    getattr(mem, "growth_witness_min_axis_delta", 0.30)
                ),
            )
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "growth_witness_enabled", True)
                    ),
                    "check_interval_seconds": int(
                        getattr(
                            agent,
                            "growth_witness_check_interval_seconds",
                            21600,
                        )
                    ),
                    "cooldown_days": float(
                        getattr(agent, "growth_witness_cooldown_days", 14.0)
                    ),
                    "min_samples": int(
                        getattr(mem, "growth_witness_min_samples", 10)
                    ),
                    "worker_registered": getattr(
                        session, "_growth_witness_worker", None
                    )
                    is not None,
                    "force_next": bool(
                        session.debug_overrides.peek("growth_witness_force_next", False)
                    ),
                    "sample_count": len(samples),
                    "findings": _gw.load_findings(session._chat_db.kv_get),
                    "last_fired_at": kv("growth_witness.last_fired_at"),
                    "last_signature": kv("growth_witness.last_signature"),
                    "last_surfaced_at": kv("growth_witness.last_surfaced_at"),
                    "dry_run": (
                        None
                        if dry is None
                        else {
                            "kind": dry.kind,
                            "magnitude": dry.magnitude,
                            "span_days": dry.span_days,
                            "signature": dry.signature,
                        }
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_growth_witness_state raised: {exc}"

    @mcp.tool()
    def force_growth_witness_draft() -> str:
        """Run the growth-witness worker once, right now.

        Arms a one-shot bypass of the cooldown + signature gates, then
        calls ``run()`` directly so a cue is drafted into the ring
        immediately (when the mood-drift ring actually shows a durable
        shift). Pairs with ``force_growth_witness_surface`` for the
        end-to-end repro.
        """
        try:
            worker = getattr(session, "_growth_witness_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered"}, indent=2
                )
            worker.force_next()
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_growth_witness_draft raised: {exc}"

    @mcp.tool()
    def force_growth_witness_surface() -> str:
        """Arm a one-shot bypass on the growth-witness cue watermark.

        Sets ``_growth_witness_force_next`` so the next provider call
        ignores the last-surfaced watermark. The ring still has to be
        non-empty (run ``force_growth_witness_draft`` first if it isn't).

        Repro: ``force_growth_witness_draft()`` ->
        ``force_growth_witness_surface()`` -> ``send_message(skip_tts=
        true)`` -> confirm the "Something you've quietly noticed ..." line
        in ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("growth_witness_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the growth-witness "
                        "watermark; ring must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_growth_witness_surface raised: {exc}"

    @mcp.tool()
    def force_aspiration_momentum_draft() -> str:
        """L14 — run the aspiration-momentum worker once, right now.

        Arms a one-shot bypass of the staleness + per-concept cooldown +
        signature gates, then calls ``run()`` directly so a cue is drafted
        into the ``aiko.aspiration_momentum`` ring immediately (when any
        active ``aspiration`` concept exists). Pairs with
        ``force_aspiration_momentum_surface`` for the end-to-end repro.
        """
        try:
            worker = getattr(session, "_aspiration_momentum_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered"}, indent=2
                )
            worker.force_next()
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_aspiration_momentum_draft raised: {exc}"

    @mcp.tool()
    def force_aspiration_momentum_surface() -> str:
        """Arm a one-shot bypass on the aspiration-momentum cue watermark.

        Sets ``_aspiration_momentum_force_next`` so the next provider call
        ignores the last-surfaced watermark. The ring still has to be
        non-empty (run ``force_aspiration_momentum_draft`` first if it isn't).

        Repro: ``force_aspiration_momentum_draft()`` ->
        ``force_aspiration_momentum_surface()`` -> ``send_message(skip_tts=
        true)`` -> confirm the "A direction ... has been quietly building
        toward" line in ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("aspiration_momentum_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the aspiration-momentum "
                        "watermark; ring must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_aspiration_momentum_surface raised: {exc}"

    @mcp.tool()
    def get_self_callback_state() -> str:
        """K71 — dump the self-callback worker + surfacing state.

        The SelfCallbackWorker mines Aiko's aged ``self`` / ``reflection``
        memories for a past feeling / intention worth revisiting and
        queues a cue in ``cue_pool``; ``_render_self_callback_block``
        claims one on a later turn. It is NEVER spoken verbatim.

        Returns a JSON dict with the master switch, the heartbeat, the
        type's surfacing cadence, whether the worker wired up, the MCP
        force-next flag, the pool rows, and the legacy kv ring (still
        written, but nothing surfaces from it any more).
        """
        try:
            from app.core.affect import self_callback as _sc
            from app.core.proactive.cue_accounting import policy_for

            agent = session._settings.agent
            mem = session._memory_settings
            policy = policy_for("self_callback")
            store = getattr(session, "_cue_store", None)

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "self_callback_enabled", True)
                    ),
                    "check_interval_seconds": int(
                        getattr(
                            agent,
                            "self_callback_check_interval_seconds",
                            21600,
                        )
                    ),
                    "surface_cooldown_hours": (
                        policy.surface_cooldown_hours if policy else None
                    ),
                    "inventory_target": (
                        policy.inventory_target if policy else None
                    ),
                    "min_age_days": int(
                        getattr(mem, "self_callback_min_age_days", 14)
                    ),
                    "llm_enabled": bool(
                        getattr(agent, "self_callback_llm_enabled", True)
                    ),
                    "llm_active": bool(
                        getattr(
                            getattr(
                                session, "_self_callback_worker", None
                            ),
                            "_worker_client",
                            None,
                        )
                        is not None
                    ),
                    "worker_registered": getattr(
                        session, "_self_callback_worker", None
                    )
                    is not None,
                    "force_next": bool(
                        session.debug_overrides.peek("self_callback_force_next", False)
                    ),
                    "pool": [
                        row.as_dict()
                        for row in store.list_for_user(
                            cue_type="self_callback", limit=10,
                        )
                    ] if store is not None else [],
                    "last_surfaced_at": (
                        store.last_surfaced_at("self_callback")
                        if store is not None
                        else None
                    ),
                    "journal_ring": _sc.load_callbacks(
                        session._chat_db.kv_get,
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_self_callback_state raised: {exc}"

    @mcp.tool()
    def force_self_callback_draft() -> str:
        """Run the self-callback worker once, right now.

        Arms a one-shot cooldown bypass, then calls ``run()`` directly so a
        cue is drafted (when an aged self / reflection memory qualifies).
        Pairs with ``force_self_callback_surface`` for the end-to-end repro.
        """
        try:
            worker = getattr(session, "_self_callback_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no MemoryStore?)"},
                    indent=2,
                )
            worker.force_next()
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_self_callback_draft raised: {exc}"

    @mcp.tool()
    def force_self_callback_surface() -> str:
        """Arm a one-shot bypass on the self-callback cue watermark.

        Sets ``_self_callback_force_next`` so the next provider call
        ignores the watermark. The ring still has to be non-empty (run
        ``force_self_callback_draft`` first if it isn't).

        Repro: ``force_self_callback_draft()`` ->
        ``force_self_callback_surface()`` -> ``send_message(skip_tts=true)``
        -> confirm the "..., you opened up about how you were feeling ..."
        line in ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("self_callback_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the self-callback "
                        "watermark; ring must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_self_callback_surface raised: {exc}"

    @mcp.tool()
    def get_second_thought_state() -> str:
        """K96 — dump the post-reply think pass ("second thought") state.

        After a reply has already been delivered, a background job re-reads
        it against the very context Aiko was handed and asks what she was
        still chewing on, or what she had available and talked past. The
        answer becomes a ``second_thought`` cue that a later turn can claim,
        which is how a thought survives the turn boundary at all.

        Read ``stats`` first, and read it as a funnel:
        ``scheduled`` is how many calls were actually spent, ``declined`` is
        the pass saying this turn needed no second thought (**the designed
        majority** — a high ratio here is healthy, not broken), and
        ``unparsed`` is the bug signal, meaning the model answered in a
        shape the parser could not read. ``skipped_*`` never spent a call:
        ``disabled`` (the master switch, off by default), ``recent``
        (``second_thought_min_gap_seconds``), ``thin`` (the character
        floors — nothing was said worth reflecting on), ``stocked`` (she is
        already holding unused thoughts, so drafting more is what turns a
        target of 2 into a shelf of 14).

        ``queued`` vs the pool's ``used`` is the real verdict: a thought
        drafted and never spoken is one she was handed and dropped.
        """
        try:
            return json.dumps(session.second_thought_state(), indent=2)
        except Exception as exc:
            return f"get_second_thought_state raised: {exc}"

    @mcp.tool()
    def force_second_thought_draft() -> str:
        """Run the think pass once, right now, against the last turn.

        Bypasses the clock, the character floors and the stock check, but
        NOT ``agent.second_thought_enabled`` — a master switch a debug tool
        can talk past is not a master switch.

        Replays the last turn's system prompt from
        ``get_last_system_prompt()`` and the newest user/assistant pair from
        the transcript, so it needs a real turn to have happened first. One
        caveat worth knowing when reading latency from this path: the
        replay carries no cache breakpoints (the accessor keeps the prompt
        string, not the offsets), so a forced draft pays more for its input
        than the live job does.

        Repro: ``send_message`` something substantial ->
        ``force_second_thought_draft()`` -> ``force_second_thought_surface()``
        -> ``send_message(skip_tts=true)`` -> confirm the "Still on your mind
        from earlier: ..." line in
        ``get_last_response_detail.system_prompt``.
        """
        try:
            return json.dumps(session.force_second_thought_draft(), indent=2)
        except Exception as exc:
            return f"force_second_thought_draft raised: {exc}"

    @mcp.tool()
    def force_second_thought_surface() -> str:
        """Arm a one-shot bypass on the second-thought surfacing cadence.

        Sets ``second_thought_force_next`` so the next assembly ignores
        ``surface_cooldown_hours``. A cue still has to be waiting — run
        ``force_second_thought_draft`` first if the pool is empty.
        """
        try:
            session.debug_overrides.arm("second_thought_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the second-thought "
                        "cadence; pool must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_second_thought_surface raised: {exc}"

    @mcp.tool()
    def force_tension_surface() -> str:
        """Arm a one-shot bypass on the tension-cue watermark.

        The provider has always named this tool in its docstring, but the
        tool did not exist until the debug overrides were collected into a
        registry -- the override could only be reached from a unit test.

        The ring still has to be non-empty.
        """
        try:
            session.debug_overrides.arm("tension_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the tension watermark; "
                        "ring must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_tension_surface raised: {exc}"

    @mcp.tool()
    def force_appreciation_surface() -> str:
        """J10 — arm a one-shot bypass on the appreciation-beat cooldown.

        The closeness gate still applies: appreciation only reads right with
        real warmth behind it, so a cold relationship stays silent even with
        this armed.
        """
        try:
            session.debug_overrides.arm("appreciation_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the appreciation cooldown; "
                        "the closeness gate still applies"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_appreciation_surface raised: {exc}"

    @mcp.tool()
    def force_reciprocal_vulnerability_surface() -> str:
        """J9 — arm a one-shot bypass on the reciprocal-vulnerability gates.

        Bypasses every gate (stage, trust floor, K15 budget, the user's
        current mood, the long wall-clock cooldown) except the master
        enable switch.
        """
        try:
            session.debug_overrides.arm("reciprocal_vulnerability_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly bypasses every reciprocal-"
                        "vulnerability gate except the master switch"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_reciprocal_vulnerability_surface raised: {exc}"

    @mcp.tool()
    def get_wellbeing_concern_state() -> str:
        """K72 — dump the wellbeing-concern worker + surfacing state.

        The WellbeingConcernWorker reads multi-day signal (small-hours
        activity, explicit "haven't slept / eaten" mentions, a heavy H3
        low stretch) and, only when a real worrying pattern clears a high
        bar + a long cooldown, drafts one gentle "you doing okay?" cue
        into ``aiko.wellbeing_concern``; ``_render_wellbeing_concern_
        block`` surfaces the newest unseen finding (watermark-gated). It is
        NEVER spoken verbatim and drops the instant the user deflects.

        Returns a JSON dict with the master switch, cadence + cooldown,
        whether the worker wired up, the MCP force-next flag, the finding
        ring, the producer pacing + surfacing watermarks, and a dry-run
        re-detection (re-runs the worker's signal collection without
        drafting).
        """
        try:
            from app.core.relationship import wellbeing_concern as _wc

            agent = session._settings.agent
            mem = session._memory_settings

            def kv(key: str) -> str | None:
                try:
                    return session._chat_db.kv_get(key)
                except Exception:
                    return None

            worker = getattr(session, "_wellbeing_concern_worker", None)
            dry: dict[str, Any] | None = None
            if worker is not None:
                try:

                    now = timephrase.utcnow()
                    late, neg_days, neg_cats = worker._collect_message_signal(
                        now
                    )
                    from app.core.affect import mood_drift as _md

                    drift = _md.deserialize_samples(kv(_md.KV_SAMPLES))
                    finding = _wc.pick_concern(
                        late_night_dates=late,
                        neglect_days=neg_days,
                        neglect_categories=neg_cats,
                        drift_samples=drift,
                        late_night_min=int(
                            getattr(mem, "wellbeing_concern_late_night_min", 3)
                        ),
                        neglect_min_days=int(
                            getattr(
                                mem, "wellbeing_concern_neglect_min_days", 2
                            )
                        ),
                        rough_run=int(
                            getattr(mem, "wellbeing_concern_rough_run", 5)
                        ),
                        rough_threshold=float(
                            getattr(
                                mem,
                                "wellbeing_concern_rough_threshold",
                                -0.25,
                            )
                        ),
                    )
                    dry = {
                        "late_night_days": len(late),
                        "neglect_days": len(neg_days),
                        "neglect_categories": neg_cats,
                        "drift_samples": len(drift),
                        "finding": (
                            None
                            if finding is None
                            else {
                                "kind": finding.kind,
                                "detail": finding.detail,
                                "severity": finding.severity,
                                "signature": finding.signature,
                            }
                        ),
                    }
                except Exception as exc:  # pragma: no cover - debug only
                    dry = {"error": str(exc)}

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "wellbeing_concern_enabled", True)
                    ),
                    "check_interval_seconds": int(
                        getattr(
                            agent,
                            "wellbeing_concern_check_interval_seconds",
                            21600,
                        )
                    ),
                    "worker_registered": worker is not None,
                    "force_next": bool(
                        session.debug_overrides.peek("wellbeing_concern_force_next", False)
                    ),
                    "cadence": session.cue_pool_cadence("wellbeing_concern"),
                    "pool": session.list_cue_pool(
                        cue_type="wellbeing_concern", limit=6,
                    ).get("cues", []),
                    "findings": _wc.load_findings(session._chat_db.kv_get),
                    "last_signature": kv("wellbeing_concern.last_signature"),
                    "dry_run": dry,
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_wellbeing_concern_state raised: {exc}"

    @mcp.tool()
    def force_wellbeing_concern_draft() -> str:
        """Run the wellbeing-concern worker once, right now.

        Arms a one-shot bypass of the signature gate, then calls ``run()``
        directly so a cue is queued (when the live signal actually shows a
        worrying pattern). Pairs with ``force_wellbeing_concern_surface``
        for the end-to-end repro.
        """
        try:
            worker = getattr(session, "_wellbeing_concern_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered"}, indent=2
                )
            worker.force_next()
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_wellbeing_concern_draft raised: {exc}"

    @mcp.tool()
    def force_wellbeing_concern_surface() -> str:
        """Arm a one-shot bypass on the wellbeing-concern surfacing cadence.

        Sets ``_wellbeing_concern_force_next`` so the next provider call
        claims a cue regardless of the week-long spacing. The pool still
        has to hold one (run ``force_wellbeing_concern_draft`` first if it
        does not).

        Repro: ``force_wellbeing_concern_draft()`` ->
        ``force_wellbeing_concern_surface()`` -> ``send_message(skip_tts=
        true)`` -> confirm the "Something you've quietly clocked ..." line
        in ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("wellbeing_concern_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the wellbeing-concern "
                        "cadence; the pool must hold a cue"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_wellbeing_concern_surface raised: {exc}"

    @mcp.tool()
    def get_shared_ritual_state() -> str:
        """K73 — dump the shared-ritual worker + surfacing state.

        The SharedRitualWorker names dyadic ``(cadence, shape)`` rituals
        that genuinely recurred across weeks into ``aiko.shared_rituals``
        and queues an offer for the strongest unnamed one;
        ``_render_shared_ritual_block`` claims it as a warm "this has
        become our thing" beat. It is NEVER spoken verbatim.

        Returns a JSON dict with the master switch, the heartbeat,
        detection thresholds, whether the worker wired up, the MCP
        force-next flag, the full ritual store, the strongest ritual still
        owed an offer, and where the type stands against its surfacing
        cadence.
        """
        try:
            from app.core.relationship import shared_ritual as _sr

            agent = session._settings.agent
            mem = session._memory_settings

            rituals = _sr.load_rituals(session._chat_db.kv_get)
            pending = _sr.pick_unacknowledged(rituals)
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "shared_ritual_enabled", True)
                    ),
                    "check_interval_seconds": int(
                        getattr(
                            agent,
                            "shared_ritual_check_interval_seconds",
                            86400,
                        )
                    ),
                    "window_days": int(
                        getattr(mem, "shared_ritual_window_days", 56)
                    ),
                    "min_weeks": int(
                        getattr(mem, "shared_ritual_min_weeks", 3)
                    ),
                    "min_share": float(
                        getattr(mem, "shared_ritual_min_share", 0.34)
                    ),
                    "min_messages": int(
                        getattr(mem, "shared_ritual_min_messages", 30)
                    ),
                    "worker_registered": getattr(
                        session, "_shared_ritual_worker", None
                    )
                    is not None,
                    "force_next": bool(
                        session.debug_overrides.peek("shared_ritual_force_next", False)
                    ),
                    "rituals": rituals,
                    "pending": pending,
                    "cadence": session.cue_pool_cadence("shared_ritual"),
                    "pool": session.list_cue_pool(
                        cue_type="shared_ritual", limit=6,
                    ).get("cues", []),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_shared_ritual_state raised: {exc}"

    @mcp.tool()
    def force_shared_ritual_draft() -> str:
        """Run the shared-ritual worker once, right now.

        Arms a one-shot bypass of the min-messages floor, then calls
        ``run()`` directly so the ritual store is refreshed from the live
        message history and an offer is queued for the strongest unnamed
        ritual. Pairs with ``force_shared_ritual_surface`` for the
        end-to-end repro.
        """
        try:
            worker = getattr(session, "_shared_ritual_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered"}, indent=2
                )
            worker.force_next()
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_shared_ritual_draft raised: {exc}"

    @mcp.tool()
    def force_shared_ritual_surface() -> str:
        """Arm a one-shot bypass on the shared-ritual surfacing gates.

        Sets ``_shared_ritual_force_next`` so the next provider call
        claims a cue regardless of the three-day spacing. The pool still
        has to hold one (run ``force_shared_ritual_draft`` first if it
        does not).

        Repro: ``force_shared_ritual_draft()`` ->
        ``force_shared_ritual_surface()`` -> ``send_message(skip_tts=true)``
        -> confirm the "... have become our thing" line in
        ``get_last_response_detail.system_prompt``.
        """
        try:
            session.debug_overrides.arm("shared_ritual_force_next")
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next assembly ignores the shared-ritual cooldown "
                        "+ ack flag; store must be non-empty"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_shared_ritual_surface raised: {exc}"

    @mcp.tool()
    def get_task_report_decision_state() -> str:
        """C6 — dump the worker-model task-report decision state.

        When a reportable background task finishes, a worker-LLM pass
        decides surface_now / park_for_natural_opening / drop and drafts
        a short "angle" framing hint the chat model uses to phrase the
        report. User-requested tasks are a hard floor (always report);
        the decision runs in shadow (log-only) on those unless
        ``floor_mode='enforce'``.

        Returns the three settings, the worker model in use, and the
        last ~20 verdicts (the in-memory ring), each tagged
        ``shadow`` / enforced + provenance.
        """
        try:
            agent = session._settings.agent
            ring = getattr(session, "_task_report_verdicts", None)
            verdicts = list(ring) if ring is not None else []
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "task_report_decision_enabled", True)
                    ),
                    "floor_mode": str(
                        getattr(
                            agent,
                            "task_report_decision_floor_mode",
                            "shadow",
                        )
                    ),
                    "angle_enabled": bool(
                        getattr(agent, "task_report_angle_enabled", True)
                    ),
                    "worker_model": str(
                        getattr(session, "_effective_worker_model", "") or ""
                    ),
                    "recent_verdicts": verdicts,
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_task_report_decision_state raised: {exc}"

    @mcp.tool()
    def force_task_report_decision(task_id: int) -> str:
        """Run the C6 report decision for a finished task — no side effects.

        Looks up the task, gathers the same stripped context the gate
        uses (provenance, origin_prompt, arc, idle, recent gist), runs
        the worker decision, and returns the verdict
        (``action`` / ``angle`` / ``reason``) WITHOUT parking a cue or
        arming escalation. Use for end-to-end repro of the worker's
        judgement on a real result.
        """
        try:
            from app.core.tasks.report_decision import decide_task_report

            orch = getattr(session, "_task_orchestrator", None)
            if orch is None:
                return json.dumps({"error": "no task orchestrator"}, indent=2)
            try:
                row = orch.get(int(task_id))
            except Exception as exc:
                return json.dumps({"error": f"get failed: {exc}"}, indent=2)
            if row is None:
                return json.dumps(
                    {"error": f"task {task_id} not found"}, indent=2
                )
            result = getattr(row, "result", None)
            summary = ""
            if isinstance(result, dict):
                summary = str(
                    result.get("summary") or result.get("content") or ""
                )
            provenance, is_floor = session._task_report_provenance(task_id)
            origin_prompt = session._task_origin_prompt(task_id)
            arc, idle_seconds, gist = session._report_decision_context()
            verdict = decide_task_report(
                ollama=getattr(session, "_maintenance_client", None),
                model=getattr(session, "_effective_worker_model", None),
                title=str(getattr(row, "title", "") or ""),
                summary=summary,
                status=str(getattr(row, "status", "done") or "done"),
                provenance=provenance,
                origin_prompt=origin_prompt,
                user_display_name=getattr(
                    session, "user_display_name", "the user"
                ),
                arc=arc,
                idle_seconds=idle_seconds,
                recent_assistant_gist=gist,
            )
            return json.dumps(
                {
                    "task_id": int(task_id),
                    "provenance": provenance,
                    "is_floor": bool(is_floor),
                    "action": verdict.action,
                    "angle": verdict.angle,
                    "reason": verdict.reason,
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_task_report_decision raised: {exc}"

    @mcp.tool()
    def get_self_correction_state() -> str:
        """K38 — dump the self-correction cue state.

        Returns a JSON dict with:

        - ``enabled``: ``agent.self_correction_enabled`` master switch.
        - ``pending``: the queued cues waiting to surface, read from
          ``cue_pool`` (the detector no longer holds them in memory), or
          an empty list. ``surfaced_count`` says whether Aiko has already
          been shown one and read past it.
        - ``cooldown_remaining``: turns left before the detector runs
          again (decrements each post-turn).
        - ``thresholds``: the ``memory.self_correction_*`` knobs the
          detector reads (min_confidence / min_overlap / max_candidates /
          cooldown_turns).
        """
        try:
            mem = session._memory_settings
            store = getattr(session, "_cue_store", None)
            pending_json: list[dict] = []
            if store is not None:
                pending_json = [
                    {
                        "id": row.id,
                        "memory_id": row.payload.get("memory_id"),
                        "label": row.payload.get("label"),
                        "reply_snippet": row.payload.get("snippet"),
                        "memory_content": row.subject,
                        "surfaced_count": row.surfaced_count,
                    }
                    for row in store.pending("self_correction", limit=10)
                ]
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "self_correction_enabled",
                            True,
                        )
                    ),
                    "pending": pending_json,
                    "cooldown_remaining": int(
                        getattr(
                            session,
                            "_self_correction_cooldown_remaining",
                            0,
                        )
                    ),
                    "thresholds": {
                        "min_confidence": float(
                            getattr(
                                mem, "self_correction_min_confidence", 0.6
                            )
                        ),
                        "min_overlap": int(
                            getattr(mem, "self_correction_min_overlap", 2)
                        ),
                        "max_candidates": int(
                            getattr(
                                mem, "self_correction_max_candidates", 50
                            )
                        ),
                        "cooldown_turns": int(
                            getattr(
                                mem, "self_correction_cooldown_turns", 3
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_self_correction_state raised: {exc}"

    @mcp.tool()
    def force_self_correction(reply_text: str = "") -> str:
        """K38 — run the self-correction detector and arm the cue.

        Runs ``detect_self_correction`` against ``reply_text`` (or the
        last assistant message if blank) over Aiko's current ``fact`` /
        ``preference`` memories, bypassing the per-fire cooldown. On a
        hit it queues the cue in ``cue_pool`` so the next turn's provider
        folds the correction into Aiko's reply -- and so the turn after
        that records whether she actually took it.

        Repro: ``force_self_correction(reply_text="My favorite color is
        blue.")`` (with a stored "favorite color is green" memory) ->
        ``send_message(skip_tts=true)`` -> confirm the "Heads-up: a
        moment ago you said ..." line in
        ``get_last_response_detail.system_prompt``.
        """
        try:
            from app.core.conversation import self_correction_detector

            text = (reply_text or "").strip()
            if not text:
                history = session._chat_db.get_messages(
                    session.session_key, limit=20
                )
                for row in reversed(history):
                    if getattr(row, "role", "") == "assistant":
                        text = (getattr(row, "content", "") or "").strip()
                        break
            if not text:
                return json.dumps(
                    {"error": "no reply_text and no recent assistant message"},
                    indent=2,
                )
            store = getattr(session, "_memory_store", None)
            if store is None:
                return json.dumps({"error": "no MemoryStore"}, indent=2)
            mem = session._memory_settings
            memories = list(store.iter_by_kind("fact"))
            memories.extend(store.iter_by_kind("preference"))
            hit = self_correction_detector.detect_self_correction(
                text,
                memories,
                min_confidence=float(
                    getattr(mem, "self_correction_min_confidence", 0.6)
                ),
                min_overlap=int(
                    getattr(mem, "self_correction_min_overlap", 2)
                ),
                max_candidates=int(
                    getattr(mem, "self_correction_max_candidates", 50)
                ),
            )
            if hit is None:
                return json.dumps(
                    {"armed": False, "hit": None, "note": "no contradiction"},
                    indent=2,
                )
            if not session.queue_self_correction_cue(hit):
                return json.dumps(
                    {"armed": False, "hit": None, "note": "cue pool refused"},
                    indent=2,
                )
            return json.dumps(
                {
                    "armed": True,
                    "hit": {
                        "memory_id": hit.memory_id,
                        "label": hit.label,
                        "overlap": hit.overlap,
                        "reply_snippet": hit.reply_snippet,
                        "memory_content": hit.memory_content,
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_self_correction raised: {exc}"

    @mcp.tool()
    def get_dropped_topic_state() -> str:
        """K82 — dump the dropped-sub-topic cue state.

        Returns a JSON dict with:

        - ``enabled``: ``agent.dropped_topic_enabled`` master switch.
        - ``pending``: the queued cues waiting to surface, read from
          ``cue_pool``, or an empty list. ``surfaced_count`` says
          whether Aiko has already been shown one and read past it.
        - ``cooldown_remaining``: turns left before the detector runs
          again (decrements each post-turn).
        - ``thresholds``: the ``memory.dropped_topic_*`` knobs the
          detector reads (min_asks / min_overlap / require_question /
          cooldown_turns).
        """
        try:
            mem = session._memory_settings
            store = getattr(session, "_cue_store", None)
            pending_json: list[dict] = []
            if store is not None:
                pending_json = [
                    {
                        "id": row.id,
                        "skipped_ask": row.payload.get("skipped_ask"),
                        "subject": row.subject,
                        "surfaced_count": row.surfaced_count,
                    }
                    for row in store.pending("dropped_topic", limit=10)
                ]
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "dropped_topic_enabled",
                            True,
                        )
                    ),
                    "pending": pending_json,
                    "cooldown_remaining": int(
                        getattr(
                            session,
                            "_dropped_topic_cooldown_remaining",
                            0,
                        )
                    ),
                    "thresholds": {
                        "min_asks": int(
                            getattr(mem, "dropped_topic_min_asks", 2)
                        ),
                        "min_overlap": int(
                            getattr(mem, "dropped_topic_min_overlap", 2)
                        ),
                        "require_question": bool(
                            getattr(
                                mem, "dropped_topic_require_question", True
                            )
                        ),
                        "cooldown_turns": int(
                            getattr(
                                mem, "dropped_topic_cooldown_turns", 3
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_dropped_topic_state raised: {exc}"

    @mcp.tool()
    def force_dropped_topic(
        user_text: str = "", reply_text: str = "",
    ) -> str:
        """K82 — run the dropped-topic detector and arm the cue.

        Runs ``detect_dropped_topic`` against ``user_text`` +
        ``reply_text`` (or the last user / assistant messages if
        blank) and queues the cue in ``cue_pool``, bypassing the
        per-fire cooldown. On a hit the next turn's provider folds
        the circle-back into Aiko's reply.

        Repro: ``force_dropped_topic(user_text="I went to the store
        and got milk, how was your day?", reply_text="Nice, milk
        from the store is the good stuff.")`` ->
        ``send_message(skip_tts=true)`` -> confirm the "Heads-up:
        last turn they also asked about ..." line in
        ``get_last_response_detail.system_prompt``.
        """
        try:
            from app.core.conversation import dropped_topic_detector

            user = (user_text or "").strip()
            reply = (reply_text or "").strip()
            if not user or not reply:
                history = session._chat_db.get_messages(
                    session.session_key, limit=20
                )
                if not user:
                    for row in reversed(history):
                        if getattr(row, "role", "") == "user":
                            user = (getattr(row, "content", "") or "").strip()
                            break
                if not reply:
                    for row in reversed(history):
                        if getattr(row, "role", "") == "assistant":
                            reply = (
                                getattr(row, "content", "") or ""
                            ).strip()
                            break
            if not user:
                return json.dumps(
                    {
                        "error": "no user_text and no recent user message",
                    },
                    indent=2,
                )
            mem = session._memory_settings
            hit = dropped_topic_detector.detect_dropped_topic(
                user,
                reply,
                min_asks=int(getattr(mem, "dropped_topic_min_asks", 2)),
                min_overlap=int(
                    getattr(mem, "dropped_topic_min_overlap", 2)
                ),
                require_question=bool(
                    getattr(mem, "dropped_topic_require_question", True)
                ),
            )
            if hit is None:
                return json.dumps(
                    {"armed": False, "hit": None, "note": "no dropped ask"},
                    indent=2,
                )
            if not session.queue_dropped_topic_cue(hit):
                return json.dumps(
                    {"armed": False, "hit": None, "note": "cue pool refused"},
                    indent=2,
                )
            return json.dumps(
                {
                    "armed": True,
                    "hit": {
                        "skipped_ask": hit.skipped_ask,
                        "covered_asks": list(hit.covered_asks),
                        "uncovered_asks": list(hit.uncovered_asks),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_dropped_topic raised: {exc}"

    @mcp.tool()
    def get_promise_followthrough_state() -> str:
        """K43 — dump the promise-lifecycle + follow-through state.

        Returns a JSON dict with:

        - ``enabled``: ``agent.promise_followthrough_enabled`` switch.
        - ``status_counts``: promise memories bucketed by lifecycle
          status (``open`` / ``surfaced`` / ``fulfilled`` / ``dropped``),
          split by side (assistant vs user).
        - ``pending``: the armed kv cue waiting for the next turn
          (memory_id / what / age_hours / at), or ``null``.
        - ``last_fired_at``: the worker's per-fire cooldown watermark.
        - ``settings``: the live cadence/age knobs.
        - ``open_assistant_promises``: up to 10 oldest open rows
          (id, what, age_hours) — the worker's candidate pool.
        """
        try:
            from app.core.memory import promise_lifecycle as lifecycle
            from app.core.proactive.promise_followthrough_worker import (
                load_pending,
            )

            store = getattr(session, "_memory_store", None)
            mem_settings = session._memory_settings
            status_counts: dict[str, dict[str, int]] = {}
            open_assistant: list[dict] = []
            if store is not None:
                for m in store.iter_by_kind("promise"):
                    side = (
                        "assistant"
                        if lifecycle.is_assistant_promise(m)
                        else "user"
                    )
                    status = lifecycle.promise_status(m)
                    status_counts.setdefault(side, {})
                    status_counts[side][status] = (
                        status_counts[side].get(status, 0) + 1
                    )
                    if side == "assistant" and status == "open":
                        open_assistant.append({
                            "id": m.id,
                            "what": lifecycle.promise_what(m)[:100],
                            "age_hours": lifecycle.promise_age_hours(m),
                        })
            open_assistant.sort(
                key=lambda d: d.get("age_hours") or 0.0, reverse=True,
            )
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "promise_followthrough_enabled",
                            True,
                        )
                    ),
                    "status_counts": status_counts,
                    "pending": load_pending(session._chat_db.kv_get),
                    "last_fired_at": session._chat_db.kv_get(
                        "promise_followthrough.last_fired_at"
                    ),
                    "settings": {
                        "interval_seconds": int(
                            getattr(
                                mem_settings,
                                "promise_followthrough_interval_seconds",
                                1800,
                            )
                        ),
                        "min_age_hours": float(
                            getattr(
                                mem_settings,
                                "promise_followthrough_min_age_hours",
                                4.0,
                            )
                        ),
                        "cooldown_hours": float(
                            getattr(
                                mem_settings,
                                "promise_followthrough_cooldown_hours",
                                6.0,
                            )
                        ),
                        "drop_after_days": float(
                            getattr(
                                mem_settings,
                                "promise_followthrough_drop_after_days",
                                14.0,
                            )
                        ),
                        "fulfil_min_overlap": int(
                            getattr(
                                mem_settings,
                                "promise_fulfil_min_overlap",
                                3,
                            )
                        ),
                    },
                    "open_assistant_promises": open_assistant[:10],
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_promise_followthrough_state raised: {exc}"

    @mcp.tool()
    def force_promise_followthrough() -> str:
        """K43 — bypass the age/cooldown gates and arm the cue now.

        Calls ``PromiseFollowthroughWorker.force_arm()``: picks the
        oldest active (open or surfaced) assistant-side promise, stamps
        it ``surfaced``, and writes the one-shot pending cue into
        kv_meta. The next turn's provider renders the "close the loop"
        line and clears the slot.

        Repro: send a message that makes Aiko say "I'll look into X"
        (or insert a ``kind=promise`` memory whose content starts with
        "Aiko promised: ...") -> call this tool -> confirm ``pending``
        in ``get_promise_followthrough_state`` -> ``send_message(
        skip_tts=true)`` -> check ``tail_logs(module_contains=
        "promise")`` for ``promise-followthrough fire:`` and the
        Heads-up line in ``get_last_response_detail.system_prompt``.
        """
        try:
            worker = getattr(session, "_promise_followthrough_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "worker not registered (no MemoryStore?)"},
                    indent=2,
                )
            payload = worker.force_arm()
            if payload is None:
                return json.dumps(
                    {
                        "armed": False,
                        "note": (
                            "no active assistant-side promise found; make "
                            "Aiko promise something first or insert a "
                            "kind=promise memory starting with "
                            "'Aiko promised: ...'"
                        ),
                    },
                    indent=2,
                )
            return json.dumps({"armed": True, "pending": payload}, indent=2)
        except Exception as exc:
            return f"force_promise_followthrough raised: {exc}"

    @mcp.tool()
    def get_topic_graph() -> str:
        """K9 — dump the memory topic-cluster graph ("what Aiko sees").

        Returns the same JSON snapshot that backs ``GET /api/topic-graph``
        and the Memory-tab browser panel:

        - ``enabled``: whether the TopicGraph wired up (needs a loaded
          MemoryStore + embedder + ``agent.topic_graph_enabled``).
        - ``total_memories`` / ``clustered_memories`` / ``total_clusters``:
          the density readout.
        - ``similarity`` / ``min_cluster_size`` / ``filter_threshold``:
          the live clustering knobs.
        - ``clusters``: sorted by size desc, each with ``summary`` /
          ``size`` / ``kind_counts`` / ``members`` (id, trimmed content,
          kind, salience, tier).

        The graph rebuilds lazily; the ``topic_graph rebuilt:`` DEBUG
        line is grep-able via ``tail_logs(module_contains="topic_graph")``.
        """
        try:
            return json.dumps(session.topic_graph_snapshot(), indent=2)
        except Exception as exc:
            return f"get_topic_graph raised: {exc}"

    @mcp.tool()
    def force_topic_graph_rebuild() -> str:
        """K9 — force a full batch re-cluster and return the fresh snapshot.

        Handy after hand-inserting memories during debugging. In the
        persisted/incremental mode (schema v20) this runs the same
        ``TopicGraphRebuildWorker`` path — a full mutual-k-NN refit over
        the whole mirror (ANN-backed at scale) that re-derives centroids
        + memberships and re-persists. In the legacy in-memory mode it
        just drops the cache so the next read rebuilds.
        """
        try:
            graph = getattr(session, "_topic_graph", None)
            if graph is None:
                return json.dumps(
                    {"error": "topic graph not registered (disabled?)"},
                    indent=2,
                )
            graph.invalidate()
            if getattr(graph, "persistent", False):
                graph.rebuild()
            return json.dumps(session.topic_graph_snapshot(), indent=2)
        except Exception as exc:
            return f"force_topic_graph_rebuild raised: {exc}"

    @mcp.tool()
    def force_concept_synthesis() -> str:
        """L2 — force one concept-synthesis run and return its stats.

        Forces a full pass (``force=True``): bypasses both the worker's
        ~30-min interval AND the incremental dirty-tracking, so it
        proposes even when nothing drifted (e.g. right after concepts were
        manually deleted, when the source-memory signatures are
        unchanged). Still bounded per run (``_max_clusters_per_run`` /
        ``_max_aiko_memories``), and existing concepts are passed to the
        proposer so it reinforces rather than duplicates. Requires
        ``agent.concepts_enabled`` + ``agent.concept_synthesis_enabled``.
        Pair with ``get_concepts_state`` to see what it proposed.
        """
        try:
            worker = getattr(session, "_concept_synthesis_worker", None)
            if worker is None:
                return json.dumps(
                    {"error": "concept synthesis worker not registered "
                     "(concepts_enabled / concept_synthesis_enabled off?)"},
                    indent=2,
                )
            return json.dumps(worker.run(force=True), indent=2)
        except Exception as exc:
            return f"force_concept_synthesis raised: {exc}"

    @mcp.tool()
    def get_concepts_state() -> str:
        """L1/L2 — dump the current concept layer for inspection.

        Lists every concept in the in-process ``ConceptStore`` mirror with
        its ``label`` / ``subject`` / ``kind`` / ``status`` / ``confidence``
        / ``importance`` / ``evidence_count`` and its evidence edges
        (``src_type:src_id``), grouped by status. This is the pre-UI debug
        read for eyeballing what the proposer (L2) has generated.

        ``importance`` is the L32 second axis: how much the belief matters,
        as opposed to how likely it is to be true. Reading the two together
        is the point -- a high importance next to a low confidence is a
        belief carried on faith, which is what confidence-only ranking used
        to bury.
        """
        try:
            store = getattr(session, "_concept_store", None)
            if store is None:
                return json.dumps(
                    {"error": "concept store not registered "
                     "(concepts_enabled off?)"},
                    indent=2,
                )
            concepts = store.all()
            importance = session.concept_importance_context(concepts)
            by_status: dict[str, list[dict[str, Any]]] = {}
            for c in concepts:
                evidence = [
                    f"{e.src_type}:{e.src_id}"
                    for e in store.evidence_of(c.concept_id)
                ]
                row = {
                    "id": c.concept_id,
                    "label": c.label,
                    "subject": c.subject,
                    "kind": c.kind,
                    "evidence_model": c.evidence_model,
                    "confidence": round(float(c.confidence), 3),
                    "evidence_count": c.evidence_count,
                    "evidence": evidence,
                    "rationale": c.rationale,
                }
                if importance is not None:
                    row["importance"] = round(
                        importance.for_concept(c), 3
                    )
                by_status.setdefault(c.status, []).append(row)
            return json.dumps(
                {"total": len(concepts), "by_status": by_status}, indent=2
            )
        except Exception as exc:
            return f"get_concepts_state raised: {exc}"

    @mcp.tool()
    def get_concept_graph(
        limit: int = 40,
        offset: int = 0,
        status: str = "",
        subject: str = "",
    ) -> str:
        """L26 — dump the live concept graph ("how Aiko is thinking").

        Richer than ``get_concepts_state``: the same JSON that backs
        ``GET /api/concepts`` and the Memory-tab Concepts panel — every
        concept with ``status`` / ``confidence`` / ``plasticity`` /
        ``importance`` / ``rationale`` / timestamps and its resolved
        evidence edges (with memory / cluster labels), plus
        ``counts.by_status`` and ``counts.by_subject``. The L32
        ``importance`` triple (``importance`` and the
        ``importance_prior`` / ``importance_charge`` it is built from)
        says how much each belief *matters*, separately from how likely
        it is to be true. ``enabled=False`` when the concept layer
        is off (``agent.concepts_enabled``). Pair with
        ``get_concept_transitions`` for what changed recently and
        ``get_last_concept_trace`` for what actually entered the last
        prompt.

        Paged, strongest-belief first, because evidence is resolved to
        full untruncated source text — the whole graph is megabytes and
        would swamp a context window. ``matched`` tells you how many rows
        the filter selects; walk them with ``offset``, or narrow with
        ``status`` / ``subject`` instead of paging blindly.
        """
        try:
            return json.dumps(
                session.concepts_snapshot(
                    limit=max(1, min(int(limit), 200)),
                    offset=max(0, int(offset)),
                    status=(status or "").strip() or None,
                    subject=(subject or "").strip() or None,
                ),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_concept_graph raised: {exc}"

    @mcp.tool()
    def get_concept_quality() -> str:
        """L22 — is the concept *layer* healthy? (the quality scoreboard)

        Where ``get_concept_graph`` dumps what Aiko believes, this
        aggregates it into the numbers that say whether the layer is
        working. Sections:

        - ``flow`` — production rate, ``promotion_rate_pct`` (what share
          of proposals become beliefs) and ``demotion_events``. A high
          promotion rate next to zero demotions means the graph only
          grows.
        - ``confidence`` / ``evidence`` — distributions, plus actives
          sitting *below* the promotion bar (``active_below_bar``,
          ``active_zero_source``) and the L22 spurious signals
          ``single_cluster_active`` (evidence all from one topic) and
          ``weak_memory_active`` (supporting memories are low-confidence).
        - ``importance`` (L32) — the second strength axis: the overall
          and per-kind distribution, ``affect_lifted_pct`` (how much of
          the axis is more than the bare kind prior), and
          ``attention_gap_sample`` — active beliefs whose importance
          outruns their confidence, i.e. what matters more than it is
          established. That list is the one to read.
        - ``duplicates`` — paraphrase twins in the cosine band just under
          the creation-time dedupe bar, i.e. the ones that slipped past it.
        - ``register`` — per ``kind/subject`` label-template concentration
          (``frame_pct`` / ``jargon_pct`` / ``top_lead_ngram``). Read per
          kind, never pooled: a high shared opening is correct for
          ``value`` and pathological for ``identity``.
        - ``pruning`` — ``unreinforced_since_promotion`` and how many
          engaged days (~conversation hours) decay would need to demote
          them at the current half-life.

        Read-only and advisory — nothing here changes a concept.
        ``enabled=False`` when the layer is off.
        """
        try:
            return json.dumps(
                session.concept_quality(), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_concept_quality raised: {exc}"

    @mcp.tool()
    def get_concept_transitions(limit: int = 50) -> str:
        """L26 — recent concept lifecycle transitions ("what changed").

        Filters the concept discovery timeline (L3 ``ConceptEventStore``)
        to lifecycle events — ``promoted`` / ``dormant`` / ``retired`` /
        ``revived`` — newest first, dropping the ``discovered`` births
        that ``get_concept_graph`` already covers. Each entry: ``id``,
        ``concept_id``, ``event_type``, ``label``, ``confidence``,
        ``reason``, ``created_at``. ``enabled=False`` when the concept
        layer is off.
        """
        try:
            n = max(1, int(limit))
            lifecycle = ("promoted", "dormant", "retired", "revived")
            collected: dict[int, dict[str, Any]] = {}
            for event_type in lifecycle:
                snap = session.concept_timeline(limit=n, event_type=event_type)
                if not snap.get("enabled", False):
                    return json.dumps(snap, indent=2, default=str)
                for event in snap.get("events", []):
                    collected[int(event["id"])] = event
            events = sorted(
                collected.values(),
                key=lambda e: int(e["id"]),
                reverse=True,
            )[:n]
            return json.dumps(
                {"enabled": True, "total": len(events), "events": events},
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_concept_transitions raised: {exc}"

    @mcp.tool()
    def get_concept_learning(
        limit: int = 40,
        subject: str | None = None,
        shape: str | None = None,
        concept_id: int | None = None,
        min_salience: float | None = None,
    ) -> str:
        """L17c — how Aiko's understanding has *changed*, and why.

        The causal record, not the raw log. ``get_concept_transitions``
        shows lifecycle moves; this shows only the movement the L17b
        classifier judged to be real learning, with the "because" that
        goes with it. Shapes:

        - ``succession`` — an old belief faded while a semantically-near
          new one rose on overlapping evidence. This is the main one: a
          proposal at or above the dedupe cosine folds into the existing
          concept, so a belief that sharpens over time shows up as two
          rows rather than one edited row.
        - ``relabel`` — the same belief, rewritten in place by the L17
          drift worker because a later proposal said it better.
        - ``emergence`` / ``loss`` / ``revival`` — a belief was promoted,
          lost its support, or came back.

        Each entry carries ``old_label`` / ``new_label`` / ``because`` /
        ``resolution``, the ``salience`` it scored, and the evidence
        labels captured at detection time (which stay readable even after
        the memories behind them are pruned). ``concept_id`` matches
        either endpoint. Pair with ``get_concept_provenance`` for one
        belief's whole arc.
        """
        try:
            return json.dumps(
                session.concept_learning_events(
                    limit=max(1, int(limit)),
                    subject=subject,
                    shape=shape,
                    concept_id=concept_id,
                    min_salience=min_salience,
                ),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_concept_learning raised: {exc}"

    @mcp.tool()
    def get_concept_provenance(concept_id: int) -> str:
        """L17e — the history-of-thought drill-down for ONE belief.

        Answers "how did she come to think this", which is a different
        question from ``get_last_concept_trace``'s "why is this in the
        prompt right now". Returns the learning events on either
        endpoint, the raw lifecycle trajectory (both ends of it, so a
        wall of confidence samples can't hide recent moves), every
        wording the belief has ever worn, and the alias chain.

        Works on merged-away and deleted concepts: the alias map resolves
        the id to whatever is still live, and the label snapshots in the
        learning events stay readable even when nothing is left in
        ``concepts`` to point at.
        """
        try:
            return json.dumps(
                session.concept_provenance(int(concept_id)),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_concept_provenance raised: {exc}"

    @mcp.tool()
    def get_concept_drift_state() -> str:
        """L17 — drift-worker state: watermark, backlog, pending reflections.

        ``watermark`` is the newest ``concept_events`` id already
        accounted for and ``latest_event_id`` the newest that exists, so
        the gap is the worker's backlog. ``pending`` is the bounded
        snapshot the rare T6 learning reflection reads on the turn path.
        ``sweep_cursor`` / ``sweep_done`` track the one-time cold-start
        backfill that walks the concept id space so history predating the
        worker still gets classified; until it is done, expect the
        learning feed to keep growing without new events arriving.
        """
        try:
            return json.dumps(
                session.concept_drift_state(), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_concept_drift_state raised: {exc}"

    @mcp.tool()
    def force_concept_drift() -> str:
        """L17 — run one drift pass now instead of waiting for the cadence.

        Applies any staged relabels (subject to the materiality,
        previously-held-wording, cooldown and identity-cosine gates, plus
        the adjudication budget) and records salient changes as learning
        events. Returns the per-pass stats. Idempotent: re-running over
        already-classified history is absorbed by the fingerprint index.
        """
        try:
            return json.dumps(
                session.run_concept_drift(), indent=2, default=str,
            )
        except Exception as exc:
            return f"force_concept_drift raised: {exc}"

    @mcp.tool()
    def get_self_history(subject: str = "aiko", eras: int = 8) -> str:
        """L19 — Aiko's history as eras of change, oldest first.

        The exact payload the ``recall_self_history`` tool hands the model,
        so you can check what she *would* say about her past before she says
        it. Each era lists beliefs classified as ``flipped`` (replaced by a
        better reading, with ``prior_label``), ``faded``, ``revived``,
        ``born``, or ``settled`` (held all along), each carrying its
        ``concept_id`` and ``learning_event_ids``.

        ``thin_record: true`` is the important field: it means the trail is
        too sparse to narrate and she is required to say she has no record.
        A store full of ``settled`` beliefs still reads as thin, because
        having beliefs is not the same as having changed.

        ``subject`` is ``aiko`` for her own history or ``user`` for how her
        read on them has evolved.
        """
        try:
            return json.dumps(
                session.self_history(subject=subject, max_eras=eras),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_self_history raised: {exc}"

    @mcp.tool()
    def get_hypotheses(
        subject: str = "",
        kind: str = "",
        origin: str = "",
        limit: int = 25,
    ) -> str:
        """L30 — everything Aiko is currently unsure about, least settled first.

        The exact payload the ``recall_hypotheses`` tool hands the model.
        Two origins share the shape and are told apart by ``origin``:
        ``invented`` rows are speculation from the ``hypotheses`` table
        with a ``credence`` and a ``hypothesis_id``; ``grounded`` rows are
        candidate concepts with a ``confidence`` and a ``concept_id`` and
        have no row of their own — they are derived on read.

        Read this together with ``get_hypothesis_state``: an empty list
        with a healthy proposer means the shelf is genuinely bare, while
        an empty list plus a full ``live`` count means everything on the
        shelf is either linked or already settled.

        ``origin`` filters to one half; ``subject`` is ``user`` / ``aiko``
        / ``relationship`` / ``world``.
        """
        try:
            return json.dumps(
                session.open_hypotheses(
                    subject=(subject or "").strip() or None,
                    kind=(kind or "").strip() or None,
                    origin=(origin or "").strip() or None,
                    limit=limit,
                ),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_hypotheses raised: {exc}"

    @mcp.tool()
    def get_hypothesis_state() -> str:
        """L30 Phase B — shelf stock against the caps that govern it.

        ``live`` versus ``max_open`` says whether the proposer has room to
        invent at all, ``by_status`` tells the three exits apart
        (``graduated`` became its own concept, ``merged`` folded into an
        existing one, ``refuted`` was denied), and ``linked`` counts rows
        already answered by a concept — those are live but will never
        surface, which is the usual explanation for a quiet lane on a full
        shelf.
        """
        try:
            return json.dumps(
                session.hypothesis_state(), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_hypothesis_state raised: {exc}"

    @mcp.tool()
    def force_hypothesis_invention() -> str:
        """L30 Phase B — invent one batch of hypotheses now.

        Bypasses the idle window but not the ``hypothesis_max_open`` cap
        or either novelty floor, so the interesting outcomes are the
        rejections: ``rejected_dup`` means it re-proposed something
        already on the shelf, ``rejected_known`` means it proposed
        something she already believes as a concept — a high count there
        means the invention prompt is paraphrasing the profile rather
        than speculating past it.
        """
        try:
            return json.dumps(
                session.run_hypothesis_proposer_now(), indent=2, default=str,
            )
        except Exception as exc:
            return f"force_hypothesis_invention raised: {exc}"

    @mcp.tool()
    def force_hypothesis_ask() -> str:
        """L30b — stock the ask shelf now and return what it queued.

        Publishes ``concept_hypothesis`` cues for the most unsettled
        testable rows from both pools (``questions`` = grounded
        candidates, ``invented`` = made-up ones). Queuing is not asking:
        the cue still waits for the provider's topic match or a typed
        gap, so follow this with ``get_cue_pool_state`` to watch it move
        and ``send_message`` on a topically-related line to trigger it.
        """
        try:
            return json.dumps(
                session.run_hypothesis_ask_worker_now(), indent=2, default=str,
            )
        except Exception as exc:
            return f"force_hypothesis_ask raised: {exc}"

    @mcp.tool()
    def get_evolution_diary(limit: int = 20) -> str:
        """L17f — Aiko's diary of how her own understanding has changed.

        The human-legible face of ``concept_learning_events``: one short
        first-person paragraph per period, composed only from the ``because``
        clauses the L17b classifier wrote. Each entry carries the
        ``learning_event_ids`` / ``concept_ids`` it summarises, so feed
        those to ``get_concept_provenance`` to check a line against its
        evidence.

        This is the fastest read on whether the whole concept pipeline is
        healthy. Entries that describe real, specific change mean
        evidence -> concept -> drift -> why is working; entries that read
        as vague or invented mean something upstream is wrong. Gaps are
        expected and meaningful — a period with nothing salient writes
        nothing rather than padding.

        Distinct from ``get_diary_worker_state``'s H9 diary, which is
        subjective inner-life journalling rather than a change log.
        """
        try:
            return json.dumps(
                session.evolution_diary(limit=limit), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_evolution_diary raised: {exc}"

    @mcp.tool()
    def get_evolution_diary_state() -> str:
        """L17f — diary-worker gates: why the next entry has or hasn't fired.

        ``blocker`` is the first reason a run would write nothing
        (``disabled`` / ``no_llm`` / ``cooldown`` / ``nothing_to_report``),
        ``pending`` how many salient changes are waiting above
        ``watermark``, and ``min_events`` the floor they must clear.
        ``pending`` below ``min_events`` is the normal resting state: those
        changes are held, not dropped, until enough accumulate.
        """
        try:
            return json.dumps(
                session.evolution_diary_state(), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_evolution_diary_state raised: {exc}"

    @mcp.tool()
    def force_evolution_diary() -> str:
        """L17f — compose one diary entry now, bypassing only the cooldown.

        The salient-event floor still applies, so a period with nothing in
        it returns ``reason: nothing_to_report`` rather than inventing an
        entry. A ``reason: empty`` means the model was given real changes
        and judged they did not add up to anything worth saying — also a
        legitimate outcome, and it spends the cooldown.
        """
        try:
            return json.dumps(
                session.run_evolution_diary(), indent=2, default=str,
            )
        except Exception as exc:
            return f"force_evolution_diary raised: {exc}"

    @mcp.tool()
    def get_last_concept_trace() -> str:
        """L26 — which concepts entered the LAST turn's prompt.

        Reads the per-turn trace stamped onto ``_last_metrics`` from
        ``PromptTelemetry`` (so it reflects the slice-cached block text,
        not a fresh recompute):

        - ``concepts`` (L5 ``concept_block``): ``surfaced`` list of
          ``{concept_id, label, confidence, plasticity, kind, subject,
          hedge}`` — or a ``reason`` when empty (``disabled`` /
          ``immature`` / ``no_eligible`` / ``aggressive`` / ...). Join
          ``concept_id`` to ``get_concept_graph`` for the evidence.
          Each entry also carries the **L35 surface reason**:
          ``surface_reason`` (a stable token — ``core_belief``,
          ``topic_match``, ``unresolved_contradiction``, ``association``,
          …) plus a readable ``surface_reason_label``. That is the one
          field that answers "why is this concept here?"; ``score`` under
          it holds the full signal breakdown the reason was picked from.
        - ``coactivation`` (L4 ``coactivation_block``): the chosen
          ``mode`` (reps / labels / strength / bucket) + ``quiet``
          cluster, or a ``reason``.

        Both carry ``slice_cache_event`` + ``aggressive`` so you can tell
        cached vs freshly built vs dropped under context pressure.
        """
        try:
            metrics = session.get_last_metrics()
            return json.dumps(
                {
                    "mode": metrics.get("mode"),
                    "concepts": metrics.get("concepts_surfaced", {}),
                    "coactivation": metrics.get("coactivation_surfaced", {}),
                },
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_last_concept_trace raised: {exc}"

    @mcp.tool()
    def rename_topic_cluster(cluster_id: int, label: str) -> str:
        """F10l — override a cluster's label (sticky across batch refits).

        Mirrors ``PATCH /api/topic-graph/clusters/{id}``. Sets the live
        label and writes a ``user_pinned`` entry into the F10a label cache
        keyed by the cluster's representative id, so the
        ``ClusterLabelWorker`` re-applies it for free after a refit and
        never regenerates over it. Returns ``{cluster_id, summary}`` or an
        error when the cluster can't be found (persistent mode only).
        """
        try:
            result = session.rename_topic_cluster(int(cluster_id), label)
            if result is None:
                return json.dumps(
                    {"error": "cluster not found (or topic graph off)"},
                    indent=2,
                )
            return json.dumps(result, indent=2)
        except Exception as exc:
            return f"rename_topic_cluster raised: {exc}"

    @mcp.tool()
    def pin_topic_cluster(cluster_id: int, pinned: bool = True) -> str:
        """F10l — pin / unpin every member of a cluster in one action.

        Mirrors ``POST /api/topic-graph/clusters/{id}/pin``. Pinned rows
        are immune to decay / prune and get a small RAG boost. Returns
        ``{cluster_id, pinned, affected}``.
        """
        try:
            result = session.set_topic_cluster_pinned(
                int(cluster_id), bool(pinned)
            )
            if result is None:
                return json.dumps(
                    {"error": "cluster not found (or topic graph off)"},
                    indent=2,
                )
            return json.dumps(result, indent=2)
        except Exception as exc:
            return f"pin_topic_cluster raised: {exc}"

    @mcp.tool()
    def forget_topic_cluster(cluster_id: int) -> str:
        """F10l — bulk-archive a topic (skips pinned members).

        Mirrors ``POST /api/topic-graph/clusters/{id}/forget``. Demotes
        every non-pinned member to ``tier=archive`` (reversible from the
        Memory list). Returns ``{cluster_id, archived, skipped_pinned}``.
        """
        try:
            result = session.forget_topic_cluster(int(cluster_id))
            if result is None:
                return json.dumps(
                    {"error": "cluster not found (or topic graph off)"},
                    indent=2,
                )
            return json.dumps(result, indent=2)
        except Exception as exc:
            return f"forget_topic_cluster raised: {exc}"

    @mcp.tool()
    def get_topic_graph_persistence_state() -> str:
        """K9 v20 — inspect the persisted/incremental topic-graph state.

        Returns whether the graph is in persistent mode, the live cluster
        count, how many incrementally-added memories are pending the next
        batch refit (``pending_unclustered``), the SQLite-persisted row
        counts (``persisted_clusters`` / ``persisted_assignments``), and
        whether an ANN index is being used for the batch path. First stop
        for "is the graph actually persisting / why hasn't it refit yet?".
        """
        try:
            graph = getattr(session, "_topic_graph", None)
            if graph is None:
                return json.dumps(
                    {"error": "topic graph not registered (disabled?)"},
                    indent=2,
                )
            out: dict = {
                "persistent": bool(getattr(graph, "persistent", False)),
                "warm": bool(getattr(graph, "_warm", False)),
                "live_clusters": len(getattr(graph, "_live", {}) or {}),
                "live_assignments": len(getattr(graph, "_assignment", {}) or {}),
                "pending_unclustered": int(getattr(graph, "_pending_unclustered", 0)),
                "assign_threshold": float(getattr(graph, "_assign_threshold", 0.0)),
                "last_k": int(getattr(graph, "_last_k", 0)),
                "rag_store_wired": getattr(graph, "_rag_store", None) is not None,
            }
            store = getattr(graph, "_cluster_store", None)
            if store is not None:
                try:
                    rows, assignments = store.load_all()
                    out["persisted_clusters"] = len(rows)
                    out["persisted_assignments"] = len(assignments)
                except Exception as exc:
                    out["persisted_load_error"] = str(exc)
            return json.dumps(out, indent=2)
        except Exception as exc:
            return f"get_topic_graph_persistence_state raised: {exc}"

    @mcp.tool()
    def get_memory_consolidation_state() -> str:
        """K35 — dump the memory-consolidation worker state.

        Returns a JSON dict with the master switch, whether the worker
        wired up (needs MemoryStore + embedder + idle scheduler), the
        cadence + threshold + cap knobs, and the current merge-LLM
        rate-limiter budget (``hour_used`` / ``day_used`` vs caps).
        """
        try:
            worker = getattr(session, "_memory_consolidation_worker", None)
            limiter = getattr(
                session, "_memory_consolidation_rate_limiter", None
            )
            mem = session._memory_settings
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "memory_consolidation_enabled",
                            True,
                        )
                    ),
                    "worker_registered": worker is not None,
                    "interval_seconds": int(
                        getattr(mem, "consolidation_interval_seconds", 21600)
                    ),
                    "lookback_days": int(
                        getattr(mem, "consolidation_lookback_days", 30)
                    ),
                    "similarity_threshold": float(
                        getattr(
                            mem, "consolidation_similarity_threshold", 0.90
                        )
                    ),
                    "max_corpus": int(
                        getattr(mem, "consolidation_max_corpus", 1000)
                    ),
                    "max_clusters_per_run": int(
                        getattr(
                            mem, "consolidation_max_clusters_per_run", 20
                        )
                    ),
                    "min_cluster_size": int(
                        getattr(mem, "consolidation_min_cluster_size", 2)
                    ),
                    "rate_limiter": (
                        limiter.snapshot() if limiter is not None else None
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_memory_consolidation_state raised: {exc}"

    @mcp.tool()
    def force_memory_consolidation() -> str:
        """K35 — run the consolidation worker once, right now.

        Bypasses the idle scheduler's quiet-window + interval gates by
        calling ``run()`` directly. The per-run cluster cap + the merge
        LLM rate limiter still apply (so a forced run won't fuse the
        whole store at once). Returns the worker's run summary
        (``corpus_size`` / ``clusters`` / ``merged`` / ``absorbed`` /
        ``llm_used``). Repro: insert a few near-identical scratchpad
        memories, call this, then confirm one survives as ``long_term``
        with ``metadata.source_ids`` and the rest are ``archive`` with
        ``metadata.consolidated_into``.
        """
        try:
            worker = getattr(session, "_memory_consolidation_worker", None)
            if worker is None:
                return json.dumps(
                    {
                        "error": (
                            "worker not registered (no MemoryStore / "
                            "embedder / disabled?)"
                        ),
                    },
                    indent=2,
                )
            result = worker.run()
            return json.dumps({"ran": True, "result": result}, indent=2)
        except Exception as exc:
            return f"force_memory_consolidation raised: {exc}"

    @mcp.tool()
    def get_confidence_decay_state(limit: int = 20) -> str:
        """K25 — preview which memory rows would currently render
        with the ``(distant)`` suffix.

        Returns a JSON dict with:

        - ``enabled``: master switch state from :class:`AgentSettings`.
        - ``settings``: the three numeric knobs (``horizon_days``,
          ``floor``, ``distant_threshold``) so user.json overrides
          are visible immediately.
        - ``rows``: top-``limit`` memory rows (most recently used
          first) with ``id``, ``kind``, ``stored_confidence``,
          ``age_days``, ``effective_confidence``, ``pinned``, and
          predicate flags ``distant`` / ``uncertain`` so you can
          eyeball which rows would gain which suffix.

        Pinned rows are included with ``distant=False`` (bypassed)
        so you can confirm pinning is working as intended. This tool
        is the tuning loop for K25: tweak ``user.json``, restart,
        call this, see what would surface differently.
        """
        store = getattr(session, "_memory_store", None)
        if store is None:
            return json.dumps({"enabled": False, "error": "no memory_store"})
        try:
            from datetime import datetime

            from app.core.rag.rag_retriever import (
                _compute_effective_confidence,
                _is_distant_memory,
            )
        except Exception as exc:
            return f"get_confidence_decay_state import failed: {exc}"
        try:
            agent = session._settings.agent
            mem_settings = session._settings.memory
            enabled = bool(
                getattr(agent, "confidence_time_decay_enabled", True),
            )
            horizon_days = max(
                1,
                int(
                    getattr(
                        mem_settings, "confidence_decay_horizon_days", 365,
                    )
                ),
            )
            floor = max(
                0.0,
                min(
                    1.0,
                    float(
                        getattr(
                            mem_settings, "confidence_decay_floor", 0.3,
                        )
                    ),
                ),
            )
            threshold = max(
                0.0,
                min(
                    1.0,
                    float(
                        getattr(
                            mem_settings,
                            "confidence_decay_distant_threshold",
                            0.5,
                        )
                    ),
                ),
            )
            mirror = getattr(store, "_mirror", None)
            rows_iter = list(mirror.values()) if mirror is not None else []
            # Sort most-recently-used first so the preview shows
            # actively-retrieved rows -- the ones that actually
            # surface in real turns.
            rows_iter.sort(
                key=lambda m: (m.last_used_at or m.created_at or ""),
                reverse=True,
            )
            now = timephrase.utcnow()
            rows: list[dict[str, Any]] = []
            cap = max(1, int(limit))
            for mem in rows_iter[:cap]:
                stored = float(getattr(mem, "confidence", 0.0) or 0.0)
                pinned = bool(getattr(mem, "pinned", False))
                created_at = getattr(mem, "created_at", None)
                age_days: float | None = None
                if created_at:
                    try:
                        created = datetime.fromisoformat(
                            str(created_at).replace("Z", "+00:00")
                        )
                        age_days = max(
                            0.0,
                            (now - created).total_seconds() / 86400.0,
                        )
                    except Exception:
                        age_days = None
                effective = (
                    _compute_effective_confidence(
                        stored,
                        age_days=age_days,
                        horizon_days=horizon_days,
                        floor=floor,
                    )
                    if age_days is not None
                    else stored
                )
                distant = _is_distant_memory(
                    stored_confidence=stored,
                    created_at=created_at,
                    now=now,
                    horizon_days=horizon_days,
                    floor=floor,
                    threshold=threshold,
                    pinned=pinned,
                )
                rows.append(
                    {
                        "id": int(mem.id),
                        "kind": mem.kind,
                        "tier": getattr(mem, "tier", "long_term"),
                        "pinned": pinned,
                        "stored_confidence": round(stored, 4),
                        "age_days": (
                            round(age_days, 2) if age_days is not None else None
                        ),
                        "effective_confidence": round(float(effective), 4),
                        "distant": bool(distant and enabled),
                        "uncertain": stored < 0.5,
                        "content_preview": (mem.content or "")[:80],
                    }
                )
            return json.dumps(
                {
                    "enabled": enabled,
                    "settings": {
                        "horizon_days": horizon_days,
                        "floor": floor,
                        "distant_threshold": threshold,
                    },
                    "rows": rows,
                    "total_rows": len(rows_iter),
                    "shown": len(rows),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_confidence_decay_state raised: {exc}"

    @mcp.tool()
    def force_seed_onboarding_goal() -> str:
        """K1 follow-up — re-seed the curated "get to know" goal.

        Bypasses the ``goals.onboarding_goal_seeded`` kv_meta gate
        and re-runs the seed. Useful for end-to-end testing the
        prompt placement + reflection cadence without nuking
        ``data/chat_sessions.db``. Cosine dedupe in
        :class:`MemoryStore` may collapse the second insert into
        the existing row (returns ``None``); the kv_meta flag
        stays set in that case.

        Returns JSON with the seeded memory id + summary preview,
        or an explanatory message if the seed was a no-op.
        """
        try:
            mem = session._seed_onboarding_goal_if_first_time(force=True)
        except Exception as exc:
            return f"force_seed_onboarding_goal raised: {exc}"
        if mem is None:
            return json.dumps(
                {
                    "fired": False,
                    "reason": (
                        "add_goal returned None — likely cosine dedupe "
                        "against an existing goal, or no_embedder. The "
                        "kv_meta flag is set anyway to prevent retries."
                    ),
                },
            )
        return json.dumps(
            {
                "fired": True,
                "memory_id": int(getattr(mem, "id", -1) or -1),
                "pinned": bool(getattr(mem, "pinned", False)),
                "source": (getattr(mem, "metadata", {}) or {}).get(
                    "source",
                ),
                "summary_preview": str(
                    getattr(mem, "content", "")
                )[:200],
            },
            indent=2,
        )

    # ── PR 2: LLM provider catalogue debug tools ─────────────────────

    @mcp.tool()
    def list_llm_providers() -> str:
        """Snapshot the LLM provider catalogue with credentials masked.

        Each entry shows ``id`` (used by routes), ``kind``,
        ``base_url``, and a boolean ``has_api_key``. Use alongside
        ``list_llm_routes`` to debug "why is Aiko using the wrong
        model" / "did my credentials make it to the cache".
        """
        try:
            return json.dumps(session.list_providers(), indent=2, default=str)
        except Exception as exc:
            return f"Error listing providers: {exc}"

    @mcp.tool()
    def list_llm_routes() -> str:
        """Snapshot the role -> provider routing table.

        Returns ``{role: {provider_id, model, context_window, max_tokens, temperature}}``
        for every active role (``main_chat`` + ``worker_default``,
        plus any future ``heavy_workers`` etc.).
        """
        try:
            return json.dumps(session.list_routes(), indent=2, default=str)
        except Exception as exc:
            return f"Error listing routes: {exc}"

    @mcp.tool()
    def set_llm_route(
        role: str,
        provider_id: str,
        model: str,
        context_window: int = 0,
        max_tokens: int = 0,
        reasoning_effort: str = "",
    ) -> str:
        """Retarget a role to a different provider / model.

        ``role`` is typically ``main_chat`` (rebuilds the chat client
        immediately) or ``worker_default`` (recorded; restart picks it
        up). ``context_window`` and ``max_tokens`` of ``0`` mean
        "leave unchanged on the route" — the resolved budget then
        falls back to the client's lookup or the existing value.

        ``reasoning_effort`` (OpenAI GPT-5 / o-series only) is sent
        verbatim when non-empty — e.g. ``low`` / ``none`` / ``xhigh``
        for gpt-5.4-mini which rejects the default ``minimal``. Use
        ``omit`` to send no reasoning param at all; leave empty to keep
        the route's current value.

        Useful for quickly flipping the chat path to a different
        cloud provider during testing without going through the
        Settings drawer.
        """
        draft: dict[str, Any] = {
            "provider_id": provider_id,
            "model": model,
        }
        if context_window > 0:
            draft["context_window"] = int(context_window)
        if max_tokens > 0:
            draft["max_tokens"] = int(max_tokens)
        if (reasoning_effort or "").strip():
            draft["reasoning_effort"] = reasoning_effort.strip().lower()
        try:
            updated = session.update_route(role, draft)
        except KeyError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error setting route: {exc}"
        return json.dumps(updated, indent=2, default=str)

    @mcp.tool()
    def get_client_cache_stats() -> str:
        """Diagnostic snapshot of the shared LLM client cache.

        Shows how many distinct underlying clients are alive and which
        provider ids share each. Useful to verify "two routes pointing
        at the same OpenAI key share one client" after a route swap.
        """
        try:
            return json.dumps(session.client_cache_stats(), indent=2, default=str)
        except Exception as exc:
            return f"Error reading client cache stats: {exc}"

    @mcp.tool()
    def get_worker_llm_gate_stats() -> str:
        """Diagnostic snapshot of the worker-LLM priority gate.

        Shows the single fair semaphore in front of the shared local
        worker model: how many calls are in flight, how many are queued
        per tier (conversation / maintenance / task), and cumulative
        grant counts + wait-time stats per tier. First stop when a
        background task or workflow seems to be starving the per-turn
        conversation workers (or vice-versa). Returns ``{enabled:false}``
        when the gate is disabled via ``agent.worker_llm_gate_enabled``.
        """
        try:
            gate = getattr(session, "_worker_llm_gate", None)
            if gate is None:
                return json.dumps({"enabled": False}, indent=2)
            payload = {"enabled": True, **gate.stats()}
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"Error reading worker LLM gate stats: {exc}"

    # ── Resources ────────────────────────────────────────────────────


