from __future__ import annotations

import json
import logging


log = logging.getLogger("app.session")


def _parse_dt_utc(value):
    """Parse an ISO timestamp into a tz-aware UTC datetime, or ``None``.

    Naive timestamps are assumed UTC. Used by the H21 dream lookup to age
    ``[dream]`` reflections against a wall-clock lookback window.
    """
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime, timezone

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class InnerLifePart2Mixin:
    """Inner-life prompt-block providers (part 2 of 4)."""

    def _render_knowledge_gaps_block(self, user_text: str) -> str:
        """F2: surface the open knowledge gap most relevant to ``user_text``.

        Returns at most one bullet. Empty string when there are no open
        gaps or the best similarity match is below the threshold (so we
        don't surface a totally unrelated wondering on every turn). The
        block ends without a trailing newline so the assembler can stitch
        it next to its siblings.
        """
        if self._question_balance_suppressed():
            return ""
        store = getattr(self, "_knowledge_gap_store", None)
        if store is None:
            return ""
        try:
            gap = store.pick_relevant(user_text)
        except Exception:
            log.debug("knowledge gap pick_relevant failed", exc_info=True)
            return ""
        if gap is None:
            return ""
        meta = getattr(gap, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        topic = str(meta.get("topic") or "").strip()
        question = str(meta.get("question") or "").strip()
        if not question:
            # Defensive: a gap row without question metadata is still
            # worth surfacing via its raw content.
            question = (gap.content or "").strip()
        if not question:
            return ""
        bullet = f"- {topic}: {question}" if topic else f"- {question}"
        return (
            f"Things you've been wondering about with {self.user_display_name}:\n"
            + bullet
        )

    def _render_knowledge_grounding_block(self, user_text: str) -> str:
        """K61: on informational turns, commit to learned specifics.

        When the live turn is a question AND Aiko has facts she's
        actually learned (F9 ``knowledge`` rows, G3
        ``curiosity_finding`` rows) topically close to what was asked,
        surface up to ``knowledge_grounding_max_items`` of them and
        nudge her to name the real things instead of survey-hedging
        ("there are many...", "it depends") or lecturing. Pure local
        work: one regex (K4 dialogue act), one embed of ``user_text``,
        and a cosine scan over the two memory kinds. No LLM, no extra
        brain-path turn. Empty when the master switch is off, the turn
        isn't informational, there are no learned facts, or nothing
        clears the similarity threshold.
        """
        if not bool(
            getattr(
                self._settings.agent, "knowledge_grounding_enabled", True,
            )
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        # K4 informational gate -- regex only, no LLM on the hot path.
        try:
            from app.core.conversation.dialogue_act_tagger import tag_regex

            if tag_regex(text).act != "question":
                return ""
        except Exception:
            log.debug("knowledge-grounding: dialogue-act tag failed", exc_info=True)
            return ""

        store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        if store is None or embedder is None:
            return ""
        try:
            rows = list(store.iter_by_kind("knowledge")) + list(
                store.iter_by_kind("curiosity_finding")
            )
        except Exception:
            log.debug("knowledge-grounding: kind snapshot failed", exc_info=True)
            return ""
        if not rows:
            return ""
        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("knowledge-grounding: embed failed", exc_info=True)
            return ""

        from app.llm.embedder import cosine_similarity

        mem_settings = self._memory_settings
        threshold = float(
            getattr(
                mem_settings, "knowledge_grounding_min_similarity", 0.45,
            )
        )
        max_items = max(
            1,
            int(
                getattr(
                    mem_settings, "knowledge_grounding_max_items", 2,
                )
            ),
        )
        scored: list[tuple[float, str]] = []
        for mem in rows:
            emb = getattr(mem, "embedding", None)
            if emb is None or getattr(emb, "size", 0) == 0:
                continue
            try:
                sim = float(cosine_similarity(qvec, emb))
            except Exception:
                continue
            if sim < threshold:
                continue
            content = (getattr(mem, "content", "") or "").strip()
            if content:
                scored.append((sim, content))
        if not scored:
            return ""
        scored.sort(key=lambda t: t[0], reverse=True)

        bullets: list[str] = []
        seen: set[str] = set()
        for _sim, content in scored:
            key = content.lower()
            if key in seen:
                continue
            seen.add(key)
            snippet = (
                content
                if len(content) <= 160
                else content[:159].rstrip() + "\u2026"
            )
            bullets.append(f"- {snippet}")
            if len(bullets) >= max_items:
                break

        log.info(
            "knowledge-grounding fire: candidates=%d surfaced=%d top=%.3f",
            len(scored),
            len(bullets),
            scored[0][0],
        )
        return (
            "You actually know specifics here -- commit to them. Name the "
            "real things below in your own voice; skip the survey hedges "
            "(\"there are lots of...\", \"it depends\") and don't lecture:\n"
            + "\n".join(bullets)
        )

    def _render_belief_gaps_block(self) -> str:
        """K2: surface up to two belief-gap lines from the previous turn.

        The gap detector runs in ``_post_turn_inner_life`` and stashes
        any detected mismatches into ``self._pending_belief_gaps``. We
        consume that list here (clearing it after read) so the gap
        only appears in the next turn's prompt -- after that Aiko
        either addressed it or the belief got contradicted/confirmed
        and won't re-surface.
        """
        if not bool(getattr(self._settings.agent, "belief_tracking_enabled", True)):
            return ""
        gaps = getattr(self, "_pending_belief_gaps", None) or []
        if not gaps:
            return ""
        try:
            from app.core.relationship.belief_gap_detector import render_inner_life_block

            block = render_inner_life_block(gaps, max_lines=2)
        except Exception:
            log.debug("belief gaps render failed", exc_info=True)
            block = ""
        # Clear regardless of render success so we don't keep retrying
        # the same broken render on every turn.
        self._pending_belief_gaps = []
        if not block:
            return ""
        return (
            f"Your theory-of-mind read on {self.user_display_name} "
            "doesn't quite match the live signal:\n" + block + "\n"
            "Name the gap once and gently if it fits, then move on. "
            "Don't repeat the question."
        )

    def _render_clarification_block(self) -> str:
        """K17: surface a one-shot clarification-repair cue.

        The detector runs inline from ``_post_turn_inner_life`` and
        stashes any hit into ``self._pending_clarification``. We
        consume the slot here (clearing it after the read) so the
        cue appears in exactly one prompt -- the very next turn
        after the user signalled "you missed it". After that Aiko
        either fixed it (good) or didn't (and the user will re-fire
        the trigger anyway), so a sticky cue would just spam.
        """
        if not bool(
            getattr(self._settings.agent, "clarification_repair_enabled", True)
        ):
            return ""
        result = getattr(self, "_pending_clarification", None)
        if result is None:
            return ""
        # Clear before rendering so a render exception still resets
        # the slot -- sticky cues are worse than missing cues here.
        self._pending_clarification = None
        try:
            from app.core.conversation.clarification_detector import render_inner_life_block

            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("clarification render failed", exc_info=True)
            return ""

    def _render_calibration_block(self) -> str:
        """K20: surface a one-line calibration hedge cue.

        Reads the per-user :class:`CalibrationState`, applies lazy
        decay so the snapshot is current, and renders a hedge cue
        when the global score sits below the configured threshold OR
        a topic slot sits below the topic threshold. Topic-specific
        cue wins when both fire.

        Returns ``""`` (empty -- not ``None``) when the master switch
        is off, the store is unavailable, or the state hasn't dropped
        below either threshold. Empty strings are dropped by the
        prompt assembler, so the cue family is silent by default.
        """
        if not bool(
            getattr(
                self._settings.agent,
                "calibration_detection_enabled",
                True,
            )
        ):
            return ""
        store = getattr(self, "_calibration_store", None)
        if store is None:
            return ""
        try:
            from app.core.affect import calibration_detector
            from datetime import datetime, timezone

            state = store.get(self._user_id)
            state = calibration_detector.decay(
                state,
                now=datetime.now(timezone.utc),
                half_life_days=float(
                    getattr(
                        self._memory_settings,
                        "calibration_half_life_days",
                        5.0,
                    )
                ),
                baseline=float(
                    getattr(
                        self._memory_settings,
                        "calibration_baseline",
                        0.80,
                    )
                ),
            )
            block = calibration_detector.render_inner_life_block(
                state,
                user_display_name=self.user_display_name,
                global_threshold=float(
                    getattr(
                        self._memory_settings,
                        "calibration_global_low_threshold",
                        0.55,
                    )
                ),
                topic_threshold=float(
                    getattr(
                        self._memory_settings,
                        "calibration_topic_low_threshold",
                        0.50,
                    )
                ),
            )
            return block or ""
        except Exception:
            log.debug("calibration render failed", exc_info=True)
            return ""

    def _render_sensory_anchor_block(self) -> str:
        """K24: surface a "small physical beat available" cue.

        Reads :class:`RoomState` + nearby items from
        :class:`WorldStore`, the live conversation arc from
        :class:`ArcStore`, and ticks the per-controller
        :class:`SensoryAnchorCadence`. The cadence handles the
        cooldown counter, arc-weighted probability roll,
        posture-kind compatibility filter, and no-repeat ring; we
        just feed it world state.

        Returns ``""`` (empty -- not ``None``) when the master
        switch is off, the cadence is unavailable, the world store
        is missing, or the cadence chooses not to fire (silent
        turn). Empty strings are dropped by the prompt assembler,
        so the cue family is silent by default.
        """
        if not bool(
            getattr(
                self._settings.agent, "sensory_anchor_enabled", True,
            )
        ):
            return ""
        cadence = getattr(self, "_sensory_anchor_cadence", None)
        if cadence is None:
            return ""
        world_store = getattr(self, "_world_store", None)
        if world_store is None:
            return ""
        try:
            from app.core.conversation import sensory_anchor

            room_state = world_store.get_state()
            posture = (room_state.posture or "").strip().lower()
            if not posture:
                return ""
            # Pull room items only -- carried items (location_id
            # IS NULL in the schema) are intentionally excluded so
            # "items she has at her current location" stays clean
            # and the no-repeat ring tracks position-aware beats.
            items = world_store.list_items(
                location_id=room_state.location_id,
            )
            if not items:
                return ""
            arc_state = None
            arc_store = getattr(self, "_arc_store", None)
            if arc_store is not None:
                try:
                    arc_state = arc_store.get_or_default(self._user_id)
                except Exception:
                    log.debug(
                        "sensory_anchor: arc fetch failed", exc_info=True,
                    )
                    arc_state = None
            arc = (
                arc_state.arc if arc_state is not None
                else "casual_check_in"
            )
            beat = cadence.tick(
                posture=posture,
                items=items,
                arc=arc,
                min_turn_gap=int(
                    getattr(
                        self._memory_settings,
                        "sensory_anchor_min_turn_gap",
                        4,
                    )
                ),
                probability_scale=float(
                    getattr(
                        self._memory_settings,
                        "sensory_anchor_probability_scale",
                        1.0,
                    )
                ),
                max_window=int(
                    getattr(
                        self._memory_settings,
                        "sensory_anchor_max_window_items",
                        6,
                    )
                ),
            )
            if beat is None:
                return ""
            return sensory_anchor.render_inner_life_block(
                beat, user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("sensory_anchor render failed", exc_info=True)
            return ""

    def _render_absence_curiosity_block(self) -> str:
        """K14 typed-mode: surface a one-shot absence-curiosity cue.

        Reads ``self._pending_absence_seconds`` (set by the post-turn
        engagement tracker when the typed gap landed in the
        absence-curiosity band) and renders a short line nudging Aiko
        toward warm curiosity about where the user has been. One-shot:
        the slot is cleared on read so the cue appears exactly once.

        Empty string when the master switch is off, when no absence
        result is pending, or when the duration falls outside the
        configured band (defensive double-check against settings that
        flipped between turns).
        """
        if not bool(
            getattr(
                self._settings.agent,
                "engagement_absence_curiosity_enabled",
                True,
            )
        ):
            return ""
        seconds = getattr(self, "_pending_absence_seconds", None)
        if seconds is None:
            return ""
        self._pending_absence_seconds = None
        try:
            seconds_f = float(seconds)
        except (TypeError, ValueError):
            return ""
        if seconds_f <= 0.0:
            return ""

        # Friendly duration string. Bands picked so a 32-min gap reads
        # as "about half an hour", a 95-min gap as "an hour and a
        # half", and a 3h gap as "a few hours" -- all sound natural
        # in conversation, none cite the raw value.
        if seconds_f < 60.0 * 45:
            duration = "about half an hour"
        elif seconds_f < 60.0 * 75:
            duration = "an hour or so"
        elif seconds_f < 60.0 * 105:
            duration = "an hour and a half"
        elif seconds_f < 60.0 * 60 * 2.5:
            duration = "a couple of hours"
        else:
            duration = "a few hours"

        name = self.user_display_name or "the user"
        return (
            f"Absence-curiosity: {name} was away for {duration} before "
            "this message. Welcome them back as if they just stepped "
            "into the room with you -- be lightly curious about what "
            "they were up to if it feels natural, but DON'T announce "
            "the gap or make them feel like they owe you an "
            "explanation. The cue is curiosity, not absence-anxiety."
        )

    def _render_turning_over_block(self) -> str:
        """K28: surface one recent reflection on the first turn after a gap.

        Sibling of :meth:`_render_absence_curiosity_block` -- both
        ride the typed-gap signal armed by the post-turn engagement
        tracker, but they answer different questions: K14
        ``absence_curiosity`` frames the welcome-back; K28
        ``turning_over`` surfaces what Aiko's been thinking about
        in the meantime. The two stack on the 90 min - 4h overlap.

        One-shot contract: reads ``self._pending_turning_over_seconds``
        (armed by ``post_turn_mixin`` when ``engagement.latency_seconds
        >= memory.turning_over_min_gap_minutes * 60``), clears the
        slot, and runs the picker
        (:func:`app.core.session.inner_life.turning_over.pick_turning_over`).
        Falls silent when:

        * the master switch is off,
        * the slot was never armed (no recent qualifying gap),
        * the threshold double-check fails (defensive against
          settings changes between turns), OR
        * the picker returns ``None`` (no reflection clears the age
          window + topical-similarity gate).

        MCP debug: ``force_turning_over`` arms
        ``_turning_over_force_next`` so the next provider call
        ignores both the pending-slot gate AND the threshold
        double-check. The picker still runs, so a forced bypass
        on an empty reflection corpus still silently expires.
        """
        # K36 one-of guard: reset the shared "a gap cue already fired
        # this assembly" flag at the top of the turn (this provider runs
        # before ``away_activities`` in the T6 cluster). Set it True only
        # when this block actually fires, so ``away_activities`` defers
        # and at most one of the two surfaces per return.
        self._gap_cue_surfaced = False

        if not bool(
            getattr(self._settings.agent, "turning_over_enabled", True)
        ):
            return ""

        # MCP-debug bypass: ``force_next`` ignores the pending-slot
        # gate for this one call. Cleared whether we fire or not.
        force_next = bool(
            getattr(self, "_turning_over_force_next", False)
        )
        if force_next:
            self._turning_over_force_next = False

        seconds = getattr(self, "_pending_turning_over_seconds", None)
        if not force_next and seconds is None:
            return ""
        self._pending_turning_over_seconds = None

        # Defensive threshold double-check: the post-turn arm has
        # already gated on the same threshold, but settings can flip
        # between turns and the slot might carry a stale value.
        if not force_next and seconds is not None:
            try:
                seconds_f = float(seconds)
            except (TypeError, ValueError):
                return ""
            min_gap_s = (
                float(
                    getattr(
                        self._memory_settings,
                        "turning_over_min_gap_minutes",
                        90.0,
                    )
                )
                * 60.0
            )
            if seconds_f < min_gap_s:
                return ""

        memory_store = getattr(self, "_memory_store", None)
        if memory_store is None:
            return ""

        try:
            reflections = list(memory_store.iter_by_kind("reflection"))
        except Exception:
            log.debug(
                "turning-over: reflection snapshot failed", exc_info=True,
            )
            return ""
        if not reflections:
            log.debug("turning-over silent: no reflection rows")
            return ""

        # Active-goal vectors. Empty when no GoalStore is wired or no
        # active goals exist; the picker handles empty pools.
        goal_vecs: list = []
        goal_store = getattr(self, "_goal_store", None)
        if goal_store is not None:
            try:
                goal_vecs = list(goal_store.active_goal_vectors())
            except Exception:
                log.debug(
                    "turning-over: goal vectors raised", exc_info=True,
                )
                goal_vecs = []

        # Recent user-message vectors from the RAG store. Same shape
        # K6 uses to warm its novelty ring buffer.
        msg_vecs: list = []
        rag_store = getattr(self, "_rag_store", None)
        msgs_window = int(
            getattr(
                self._memory_settings,
                "turning_over_recent_msgs_window",
                12,
            )
        )
        if rag_store is not None and msgs_window > 0:
            try:
                msg_vecs = list(
                    rag_store.list_recent_user_vectors(
                        user_id_prefix=getattr(self, "_user_id", "") or "",
                        limit=msgs_window,
                    )
                )
            except Exception:
                log.debug(
                    "turning-over: recent_user_vectors raised", exc_info=True,
                )
                msg_vecs = []

        try:
            from app.core.session.inner_life import turning_over as _to
        except Exception:
            log.debug("turning-over import failed", exc_info=True)
            return ""

        from datetime import datetime, timezone

        memory_settings = self._memory_settings
        try:
            result = _to.pick_turning_over(
                reflections=reflections,
                active_goal_vecs=goal_vecs,
                recent_user_vecs=msg_vecs,
                now=datetime.now(timezone.utc),
                min_age_hours=float(
                    getattr(
                        memory_settings,
                        "turning_over_min_age_hours",
                        _to.DEFAULT_MIN_AGE_HOURS,
                    )
                ),
                max_age_hours=float(
                    getattr(
                        memory_settings,
                        "turning_over_max_age_hours",
                        _to.DEFAULT_MAX_AGE_HOURS,
                    )
                ),
                min_topical_similarity=float(
                    getattr(
                        memory_settings,
                        "turning_over_min_topical_similarity",
                        _to.DEFAULT_MIN_TOPICAL_SIMILARITY,
                    )
                ),
            )
        except Exception:
            log.debug("turning-over picker raised", exc_info=True)
            return ""

        if result is None:
            log.debug(
                "turning-over silent: no candidate cleared the gates "
                "(reflections=%d goals=%d msgs=%d)",
                len(reflections), len(goal_vecs), len(msg_vecs),
            )
            return ""

        # Stash diagnostics for the MCP debug tool.
        self._last_turning_over = result
        # K36 one-of guard: mark that a gap cue surfaced this assembly so
        # ``away_activities`` defers to this (reflection-based) cue.
        self._gap_cue_surfaced = True

        log.info(
            "turning-over fire: memory_id=%d age_h=%.1f topical=%.3f "
            "source=%s dream=%s",
            result.memory_id,
            result.age_hours,
            result.topical_score,
            result.topical_source or "-",
            result.dream,
        )

        try:
            return _to.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("turning-over render failed", exc_info=True)
            return ""

    def _render_sleep_return_block(self) -> str:
        """H21: narrate having dozed off on return from an overnight gap.

        The behavioural anchor for the dream system. Runs first in the
        gap-cue family (immediately after K28 ``turning_over``, before K36
        ``away_activities`` / K34 ``forward_curiosity``) so an overnight
        return reads as "I actually fell asleep …" rather than "I tidied
        the desk while you were away". When a recent ``[dream]`` reflection
        exists, it's woven into the line so the dream finally has a cause.

        One-shot contract: reads + clears
        ``self._pending_sleep_return_seconds`` (armed in
        ``post_turn_helpers_mixin._maybe_arm_sleep_return_slot`` on a typed
        gap >= ``memory.sleep_return_min_gap_hours``), then applies the
        finer return-hour-aware overnight gate
        (:func:`sleep_return.looks_like_overnight`). A gap that doesn't read
        as a sleep returns "" WITHOUT touching ``_gap_cue_surfaced`` so the
        ordinary away / forward cues still get their turn. When it does
        fire it sets ``_gap_cue_surfaced`` so the rest of the family defers.

        Defers to ``turning_over`` (which runs first and owns the
        ``_gap_cue_surfaced`` reset). MCP debug:
        ``force_sleep_return_surface`` arms ``_sleep_return_force_next`` to
        bypass the slot + overnight gates.
        """
        if not bool(
            getattr(self._settings.agent, "sleep_return_enabled", True)
        ):
            return ""

        force_next = bool(getattr(self, "_sleep_return_force_next", False))
        if force_next:
            self._sleep_return_force_next = False

        # One-of guard: turning_over already surfaced a gap cue this
        # assembly. Stand down (unless explicitly forced).
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        seconds = getattr(self, "_pending_sleep_return_seconds", None)
        if not force_next and seconds is None:
            return ""
        self._pending_sleep_return_seconds = None

        from datetime import datetime, timezone

        from app.core.world import sleep_return as _sr

        ms = self._memory_settings
        min_gap_h = float(
            getattr(ms, "sleep_return_min_gap_hours", _sr.DEFAULT_MIN_GAP_HOURS)
        )
        overnight_h = float(
            getattr(
                ms, "sleep_return_overnight_hours", _sr.DEFAULT_OVERNIGHT_HOURS
            )
        )

        now_local = datetime.now()
        if force_next:
            try:
                gap_hours = float(seconds) / 3600.0 if seconds is not None else overnight_h
            except (TypeError, ValueError):
                gap_hours = overnight_h
        else:
            try:
                gap_hours = float(seconds) / 3600.0
            except (TypeError, ValueError):
                return ""
            if not _sr.looks_like_overnight(
                gap_hours,
                now_local.hour,
                min_gap_hours=min_gap_h,
                overnight_hours=overnight_h,
            ):
                log.debug(
                    "sleep-return silent: gap=%.1fh hour=%d not overnight",
                    gap_hours, now_local.hour,
                )
                return ""

        # Where she dozed off — her current room location if it reads as a
        # restful spot, else the cozy default. Best-effort; never fatal.
        spot_slug: str | None = None
        world_store = getattr(self, "_world_store", None)
        if world_store is not None:
            try:
                state = world_store.get_state()
                loc_id = getattr(state, "location_id", None)
                if loc_id is not None:
                    loc = world_store.get_location_by_id(int(loc_id))
                    if loc is not None:
                        spot_slug = getattr(loc, "slug", None)
            except Exception:
                log.debug("sleep-return: world state read failed", exc_info=True)
        spot_phrase = _sr.sleep_spot_phrase(spot_slug)

        # Optional dream linkage — newest ``[dream]`` reflection within the
        # lookback window gets woven into the cue.
        dream_gist = self._recent_dream_gist(now_local, ms)

        name = self.user_display_name
        self._gap_cue_surfaced = True
        self._last_sleep_return = {
            "gap_hours": round(gap_hours, 2),
            "return_hour": now_local.hour,
            "spot": spot_phrase,
            "spot_slug": spot_slug,
            "dream": bool(dream_gist),
        }
        log.info(
            "sleep-return fire: gap=%.1fh hour=%d spot=%s dream=%s",
            gap_hours, now_local.hour, spot_slug or "-", bool(dream_gist),
        )
        return _sr.render_sleep_line(
            spot_phrase,
            user_display_name=name,
            dream_gist=dream_gist,
        )

    def _recent_dream_gist(self, now_local: Any, memory_settings: Any) -> str | None:
        """Newest ``[dream]`` reflection content within the lookback window.

        Dreams are stored by the :class:`DreamWorker` as ``kind="reflection"``
        rows whose content is prefixed ``[dream] ``. Returns the cleaned gist
        (prefix stripped, truncated) or ``None`` when no recent dream exists.
        """
        from datetime import datetime, timezone

        from app.core.world import sleep_return as _sr

        memory_store = getattr(self, "_memory_store", None)
        if memory_store is None:
            return None
        lookback_h = float(
            getattr(
                memory_settings,
                "sleep_return_dream_lookback_hours",
                _sr.DEFAULT_DREAM_LOOKBACK_HOURS,
            )
        )
        if lookback_h <= 0:
            return None
        try:
            reflections = list(memory_store.iter_by_kind("reflection"))
        except Exception:
            log.debug("sleep-return: reflection snapshot failed", exc_info=True)
            return None

        prefix = "[dream] "
        now_utc = datetime.now(timezone.utc)
        best_dt: datetime | None = None
        best_content: str | None = None
        for mem in reflections:
            content = str(getattr(mem, "content", "") or "")
            if not content.lower().startswith(prefix):
                continue
            created = _parse_dt_utc(getattr(mem, "created_at", None))
            if created is None:
                continue
            age_h = (now_utc - created).total_seconds() / 3600.0
            if age_h < 0 or age_h > lookback_h:
                continue
            if best_dt is None or created > best_dt:
                best_dt = created
                best_content = content[len(prefix):].strip()

        if not best_content:
            return None
        # Keep the cue short — first sentence / 160 chars.
        gist = best_content.replace("\n", " ").strip()
        if len(gist) > 160:
            gist = gist[:157].rstrip() + "…"
        return gist or None

    def _render_away_activities_block(self) -> str:
        """K36: surface one "while you were away I …" line after a gap.

        Consumer side of the :class:`IdleAwayActivityWorker` producer.
        Same typed-gap arming as K28 ``turning_over`` (via
        ``post_turn_mixin._maybe_arm_away_activities_slot``), but reads
        the worker's kv journal instead of the reflection corpus.

        One-shot contract: reads + clears
        ``self._pending_away_activities_seconds``, re-checks the gap,
        reads the journal ring, and surfaces the newest entry that's
        newer than the ``away_activity.last_surfaced_at`` watermark. The
        watermark advances so the same beat never resurfaces.

        Defers to ``turning_over`` via the shared ``_gap_cue_surfaced``
        flag so at most one of the two gap cues fires per return —
        ``turning_over`` runs first and wins when it has a reflection to
        share; this fills in otherwise.

        MCP debug: ``force_away_activities_surface`` arms
        ``_away_activities_force_next`` to bypass the slot + watermark
        gates (the journal still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "away_activities_enabled", True)
        ):
            return ""

        force_next = bool(
            getattr(self, "_away_activities_force_next", False)
        )
        if force_next:
            self._away_activities_force_next = False

        # One-of guard: turning_over already surfaced a gap cue this
        # assembly. Stand down (unless explicitly forced).
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        seconds = getattr(self, "_pending_away_activities_seconds", None)
        if not force_next and seconds is None:
            return ""
        self._pending_away_activities_seconds = None

        if not force_next and seconds is not None:
            try:
                seconds_f = float(seconds)
            except (TypeError, ValueError):
                return ""
            min_gap_s = (
                float(
                    getattr(
                        self._memory_settings,
                        "away_activities_min_gap_hours",
                        4.0,
                    )
                )
                * 3600.0
            )
            if seconds_f < min_gap_s:
                return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.world.idle_activity_worker import load_journal
        except Exception:
            log.debug("away_activities import failed", exc_info=True)
            return ""

        journal = load_journal(chat_db.kv_get)
        if not journal:
            log.debug("away_activities silent: empty journal")
            return ""

        newest = journal[-1]
        at = str(newest.get("at") or "")
        summary = str(newest.get("summary") or "").strip()
        if not summary:
            return ""

        watermark_key = "away_activity.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                log.debug("away_activities silent: already surfaced %s", at)
                return ""

        # Advance the watermark so this beat doesn't resurface.
        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug("away_activities watermark write failed", exc_info=True)

        name = self.user_display_name
        # Mark the gap-cue slot consumed so the K34 forward-curiosity
        # provider (which runs after this one) defers — at most one of
        # {turning_over, away_activities, forward_curiosity} surfaces
        # per return.
        self._gap_cue_surfaced = True
        log.info("away-activities fire: at=%s key=%s", at, newest.get("key"))
        return (
            f"While {name} was away, you {summary}. If it fits naturally, "
            "you can mention it in passing — drop it if it doesn't."
        )

    def _render_forward_curiosity_block(self) -> str:
        """K34: surface one "you've been wondering ..." line after a gap.

        Consumer side of the :class:`ForwardCuriosityWorker` producer.
        Same typed-gap arming as K28 ``turning_over`` / K36
        ``away_activities`` (via
        ``post_turn_mixin._maybe_arm_forward_curiosity_slot``), but reads
        the worker's kv question ring.

        One-shot contract: reads + clears
        ``self._pending_forward_curiosity_seconds``, re-checks the gap,
        reads the ring, and surfaces the newest entry that's newer than
        the ``forward_curiosity.last_surfaced_at`` watermark. The
        watermark advances so the same question never resurfaces.

        Runs LAST of the three gap-return cues, so it defers to both
        ``turning_over`` and ``away_activities`` via the shared
        ``_gap_cue_surfaced`` flag — at most one of the three fires per
        return.

        MCP debug: ``force_forward_curiosity_surface`` arms
        ``_forward_curiosity_force_next`` to bypass the slot + watermark
        + one-of gates (the ring still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "forward_curiosity_enabled", True)
        ):
            return ""
        if self._question_balance_suppressed():
            return ""

        force_next = bool(
            getattr(self, "_forward_curiosity_force_next", False)
        )
        if force_next:
            self._forward_curiosity_force_next = False

        # One-of guard: a higher-priority gap cue already surfaced this
        # assembly. Stand down (unless explicitly forced).
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        seconds = getattr(self, "_pending_forward_curiosity_seconds", None)
        if not force_next and seconds is None:
            return ""
        self._pending_forward_curiosity_seconds = None

        if not force_next and seconds is not None:
            try:
                seconds_f = float(seconds)
            except (TypeError, ValueError):
                return ""
            min_gap_s = (
                float(
                    getattr(
                        self._memory_settings,
                        "forward_curiosity_min_gap_hours",
                        4.0,
                    )
                )
                * 3600.0
            )
            if seconds_f < min_gap_s:
                return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive.forward_curiosity_worker import (
                load_questions,
            )
        except Exception:
            log.debug("forward_curiosity import failed", exc_info=True)
            return ""

        ring = load_questions(chat_db.kv_get)
        if not ring:
            log.debug("forward_curiosity silent: empty ring")
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        question = str(newest.get("question") or "").strip()
        if not question:
            return ""

        watermark_key = "forward_curiosity.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                log.debug(
                    "forward_curiosity silent: already surfaced %s", at,
                )
                return ""

        # Advance the watermark so this question doesn't resurface.
        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug(
                "forward_curiosity watermark write failed", exc_info=True,
            )

        self._gap_cue_surfaced = True
        log.info("forward-curiosity fire: at=%s source=%s", at, newest.get("source"))
        return (
            f"You've been wondering {question}. If it comes up naturally, "
            "you can ask — drop it if it doesn't fit."
        )

    def _render_hobby_block(self) -> str:
        """H19: standing "what she's been up to lately" line.

        Reads the :class:`HobbyWorker`'s ``aiko.current_hobby`` kv blob and
        renders one terse line giving Aiko continuity of intent — a real
        answer to "what have you been up to?" that progresses across days.
        Empty when the worker hasn't started a hobby yet. The actual
        takeaways ("I'm three chapters in and ugh, the betrayal") surface
        separately through the H17 idle-seed cue.
        """
        if not bool(
            getattr(self._settings.agent, "hobby_worker_enabled", True)
        ):
            return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive.hobby_worker import load_hobby
            from app.core.world.hobby import render_hobby_line
        except Exception:
            log.debug("hobby block import failed", exc_info=True)
            return ""

        state = load_hobby(chat_db.kv_get)
        if not state:
            return ""

        label = str(state.get("label") or "").strip()
        if not label:
            return ""
        try:
            progress = int(state.get("progress", 0))
        except (TypeError, ValueError):
            progress = 0
        unit = str(state.get("unit") or "step")
        line = render_hobby_line(label, progress, unit)
        return (
            f"Lately, in your own time, you've been {line}. Bring it up only "
            "if it comes up naturally — don't force it."
        )

    def _render_idle_seed_block(self) -> str:
        """H17: surface one "while I was <doing X> I started wondering ..." cue.

        Consumer side of the :class:`IdleAwayActivityWorker` seed producer
        (``_maybe_emit_seed`` → the ``aiko.idle_seeds`` kv ring). Folds the
        newest unseen seed into the prompt as one optional, private hint so
        Aiko phrases the line herself — the seed is NEVER spoken verbatim.

        Unlike the gap-return cue family (turning_over / away_activities /
        forward_curiosity), this is NOT gap-gated and does NOT touch
        ``_gap_cue_surfaced``: a thought from her own idle life can come up
        mid-conversation. Bounded instead by the producer (rare + daily-
        capped) and a wall-clock surfacing cooldown so it never spams.

        One-shot per seed via the ``idle_seed.surfaced_at`` watermark; the
        ``idle_seed.surfaced_clock`` stamp enforces the cooldown. MCP debug:
        ``force_idle_seed_surface`` arms ``_idle_seed_force_next`` to bypass
        both gates (the ring still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "idle_seed_enabled", True)
        ):
            return ""

        force_next = bool(getattr(self, "_idle_seed_force_next", False))
        if force_next:
            self._idle_seed_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.world.idle_activity_worker import load_idle_seeds
        except Exception:
            log.debug("idle_seed import failed", exc_info=True)
            return ""

        ring = load_idle_seeds(chat_db.kv_get)
        if not ring:
            log.debug("idle_seed silent: empty ring")
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        seed = str(newest.get("seed") or "").strip()
        if not seed:
            return ""

        watermark_key = "idle_seed.surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                log.debug("idle_seed silent: already surfaced %s", at)
                return ""

            # Wall-clock surfacing cooldown — don't fold a seed into the
            # prompt more often than ``idle_seed_surface_cooldown_seconds``.
            from datetime import datetime, timezone

            cooldown_s = float(
                getattr(
                    self._memory_settings,
                    "idle_seed_surface_cooldown_seconds",
                    1800,
                )
            )
            if cooldown_s > 0:
                try:
                    raw_clock = chat_db.kv_get("idle_seed.surfaced_clock")
                except Exception:
                    raw_clock = None
                if raw_clock:
                    try:
                        last = datetime.fromisoformat(str(raw_clock))
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        elapsed = (
                            datetime.now(timezone.utc) - last
                        ).total_seconds()
                        if elapsed < cooldown_s:
                            log.debug(
                                "idle_seed silent: cooldown %.0fs < %.0fs",
                                elapsed,
                                cooldown_s,
                            )
                            return ""
                    except Exception:
                        pass

        # Advance the per-seed watermark + the surfacing clock.
        try:
            from datetime import datetime, timezone

            chat_db.kv_set(watermark_key, at)
            chat_db.kv_set(
                "idle_seed.surfaced_clock",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except Exception:
            log.debug("idle_seed watermark write failed", exc_info=True)

        activity = str(newest.get("activity") or "").replace("_", " ").strip()
        log.info("idle-seed fire: at=%s activity=%s", at, activity)
        if activity:
            lead = f"Earlier, while you were {activity}, "
        else:
            lead = "Earlier, during some quiet time, "
        return (
            f"{lead}a thought crossed your mind: {seed} "
            "If it fits naturally you can bring it up — no need to force it."
        )

    def _render_follow_up_block(self) -> str:
        """Surface one "you could ask how their plan went" cue.

        Consumer side of the :class:`FollowUpWorker` producer. The worker
        drafts a cue into the ``aiko.follow_up_cues`` kv ring when a
        user-mentioned ``future_plan`` event time has just passed; this
        provider folds the newest unseen cue into the prompt as one
        optional, private hint. Aiko phrases the actual check-in herself
        — the cue is NEVER spoken verbatim (the bug that leaked the
        directive into chat).

        Independent of the gap-return cue family — does NOT read or set
        ``_gap_cue_surfaced``: a concrete, time-anchored "their plan just
        happened" beat is worth a line even alongside a generic gap cue,
        and it must surface on the very next turn after the event passed,
        not only on a long-gap return.

        One-shot via the ``follow_up.last_surfaced_at`` watermark so the
        same cue never resurfaces. MCP debug: ``force_follow_up_surface``
        arms ``_follow_up_force_next`` to bypass the watermark (the ring
        still has to be non-empty).
        """
        if not bool(getattr(self._settings.agent, "follow_up_enabled", True)):
            return ""
        if self._question_balance_suppressed():
            return ""

        force_next = bool(getattr(self, "_follow_up_force_next", False))
        if force_next:
            self._follow_up_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive.follow_up_worker import load_follow_up_cues
        except Exception:
            log.debug("follow_up import failed", exc_info=True)
            return ""

        ring = load_follow_up_cues(chat_db.kv_get)
        if not ring:
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        plan = str(newest.get("plan") or "").strip()
        if not plan:
            return ""

        watermark_key = "follow_up.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                return ""

        # Advance the watermark so this cue doesn't resurface.
        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug("follow_up watermark write failed", exc_info=True)

        clock = str(newest.get("clock") or "").strip()
        question = str(newest.get("question") or "").strip()
        when = f" (around {clock})" if clock else ""
        line = (
            f"Earlier{when} {plan} — that time has passed now. If it fits "
            "the flow, you can gently ask how it went; no need to open with "
            "it, and let it go if the moment isn't right."
        )
        if question:
            line += f' Something like: "{question}"'
        log.info(
            "follow-up cue fire: at=%s source=%s", at, newest.get("source_id"),
        )
        return line

    def _render_upcoming_horizon_block(self) -> str:
        """K-time3: surface a "coming up" heads-up with pre-resolved times.

        A cheap forward sweep over ``future_plan`` memories whose
        ``event_time`` falls within ``memory.upcoming_horizon_days`` of now,
        rendered as one terse cue with the relative phrasing **already
        worked out** by :mod:`app.core.infra.timephrase` — so the chat model
        never recomputes a future date (the thing LLMs reliably get wrong).
        This is the missing *forward sweep*: ``rag_retriever`` only tags a
        future plan with its resolved time if semantic RAG happens to surface
        it; here it surfaces by time, not relevance.

        Anti-nag: the cue re-surfaces the moment the upcoming set *changes*
        (a new plan appears, or one slides out of the window), but otherwise
        sits out a per-turn cooldown (``upcoming_horizon_cooldown_turns``) so
        an unchanged calendar isn't recited every turn. Computed live (no
        worker / kv): a single mirror scan + a couple of ISO parses.

        MCP debug: ``force_upcoming_horizon_surface`` arms
        ``_upcoming_horizon_force_next`` to bypass the cooldown + signature
        gate (the window must still hold at least one plan).
        """
        if not bool(
            getattr(self._settings.agent, "upcoming_horizon_enabled", True)
        ):
            return ""
        store = getattr(self, "_memory_store", None)
        if store is None:
            return ""

        force = bool(getattr(self, "_upcoming_horizon_force_next", False))
        if force:
            self._upcoming_horizon_force_next = False

        try:
            from app.core.conversation.upcoming_horizon import (
                build_signature,
                render_block,
                select_upcoming,
            )
            from app.core.infra import timephrase
        except Exception:
            log.debug("upcoming_horizon import failed", exc_info=True)
            return ""

        mem_settings = self._memory_settings
        horizon_days = int(
            getattr(mem_settings, "upcoming_horizon_days", 7)
        )
        max_items = int(
            getattr(mem_settings, "upcoming_horizon_max_items", 3)
        )

        now = timephrase.now()
        try:
            candidates = store.list_by_temporal_type("future_plan")
        except Exception:
            log.debug("upcoming_horizon: list future_plan failed", exc_info=True)
            return ""

        events = select_upcoming(
            candidates, now, horizon_days=horizon_days, max_items=max_items,
        )
        if not events:
            # Nothing on the horizon: forget the last signature so a plan
            # that appears later always reads as "new" and surfaces fresh.
            self._upcoming_horizon_sig = ""
            return ""

        sig = build_signature(events)
        last_sig = getattr(self, "_upcoming_horizon_sig", "")
        cooldown = int(getattr(self, "_upcoming_horizon_cooldown", 0) or 0)
        if not force and sig == last_sig and cooldown > 0:
            self._upcoming_horizon_cooldown = cooldown - 1
            return ""

        line = render_block(events, now, self.user_display_name)
        if not line:
            return ""

        self._upcoming_horizon_sig = sig
        self._upcoming_horizon_cooldown = max(
            0, int(getattr(mem_settings, "upcoming_horizon_cooldown_turns", 6))
        )
        log.info(
            "upcoming-horizon fire: count=%d cooldown=%d sig=%s",
            len(events),
            self._upcoming_horizon_cooldown,
            sig[:80],
        )
        return line

    def _render_knowledge_gap_notice_block(self, user_text: str) -> str:
        """F10f: surface one "I keep circling X but never dug in" cue.

        Consumer side of the
        :class:`~app.core.proactive.knowledge_gap_notice_worker.KnowledgeGapNoticeWorker`
        producer. The worker drafts dense-but-unresearched topics into the
        ``aiko.knowledge_gap_notices`` kv ring during quiet windows; this
        provider surfaces one **only when the live turn is actually on that
        topic** (lexical overlap with ``user_text``), so the beat lands in
        context — "oh, this again; honestly I still don't know much about
        it" — rather than as a standalone non-sequitur.

        Once-per-topic: a surfaced ``cluster_key`` is recorded in
        ``knowledge_gap_notice.surfaced_keys`` and never resurfaces (the
        worker's per-topic cooldown also stops it being re-drafted). The
        cue is a private prompt hint, NEVER spoken verbatim — Aiko phrases
        the admission herself. Independent of the gap-return cue family
        (does not touch ``_gap_cue_surfaced``); it's tied to the live topic,
        not to a long-absence return. MCP debug: ``force_knowledge_gap_notice_surface``
        arms ``_knowledge_gap_notice_force_next`` to bypass the
        topic-relevance + surfaced gates (the ring must still be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "knowledge_gap_notice_enabled", True)
        ):
            return ""

        force_next = bool(getattr(self, "_knowledge_gap_notice_force_next", False))
        if force_next:
            self._knowledge_gap_notice_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.knowledge_gap_notice_worker import (
                load_notices,
                topic_relevant,
            )
        except Exception:
            log.debug("knowledge_gap_notice import failed", exc_info=True)
            return ""

        ring = load_notices(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "knowledge_gap_notice.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("cluster_key") or "")
            topic = str(entry.get("topic") or "").strip()
            if not topic:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not topic_relevant(topic, text):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("cluster_key") or "")
        topic = str(chosen.get("topic") or "").strip()
        if key:
            surfaced.add(key)
            try:
                # Cap the surfaced set so it can't grow unbounded.
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "knowledge_gap_notice surfaced write failed", exc_info=True
                )

        log.info("knowledge-gap-notice fire: topic=%r key=%s", topic[:80], key)
        return (
            f"Heads-up: \"{topic}\" keeps coming up between you two, but you've "
            "never actually dug into it — you don't really know much about it "
            "yet. If it fits, it's honest to say so and show you're curious to "
            "learn more, rather than bluffing or glossing over it. One light "
            "line; don't over-apologise for not knowing."
        )

    def _render_associative_wander_block(self, user_text: str) -> str:
        """K64a: surface one "funny, this reminds me of ..." connection.

        Consumer side of the
        :class:`~app.core.proactive.associative_wander_worker.AssociativeWanderWorker`
        producer. The worker drifts across the topic graph during quiet
        windows and drafts a genuine connection between two *distant*
        clusters into the ``aiko.associative_wanders`` kv ring; this
        provider surfaces one **only when the live turn is actually on one
        of the two topics** (lexical overlap with ``user_text``), so the
        drift lands in context — "oh, this reminds me of ..." — rather than
        as a non-sequitur.

        One-shot per pair: a surfaced ``pair_key`` is recorded in
        ``associative_wander.surfaced_keys`` and never resurfaces (the
        worker's per-pair cooldown also stops it being re-drafted). The cue
        is a private prompt hint, NEVER spoken verbatim — Aiko decides
        whether the connection fits and phrases it herself. Independent of
        the gap-return cue family (does not touch ``_gap_cue_surfaced``);
        it's tied to the live topic. MCP debug:
        ``force_associative_wander_surface`` arms
        ``_associative_wander_force_next`` to bypass the topic-relevance +
        surfaced gates (the ring must still be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "associative_wander_enabled", True)
        ):
            return ""

        force_next = bool(
            getattr(self, "_associative_wander_force_next", False)
        )
        if force_next:
            self._associative_wander_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.associative_wander_worker import (
                load_wanders,
                wander_relevant,
            )
        except Exception:
            log.debug("associative_wander import failed", exc_info=True)
            return ""

        ring = load_wanders(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "associative_wander.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("pair_key") or "")
            connection = str(entry.get("connection") or "").strip()
            if not connection:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not wander_relevant(entry, text):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("pair_key") or "")
        topic_a = str(chosen.get("topic_a") or "").strip()
        topic_b = str(chosen.get("topic_b") or "").strip()
        connection = str(chosen.get("connection") or "").strip()
        if key:
            surfaced.add(key)
            try:
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "associative_wander surfaced write failed", exc_info=True
                )

        log.info(
            "associative-wander fire: a=%r b=%r key=%s",
            topic_a[:60], topic_b[:60], key,
        )
        return (
            "Heads-up: while your mind was wandering earlier you noticed a "
            f"connection between \"{topic_a}\" and \"{topic_b}\" — "
            f"{connection}. The live turn just brushed one of them. If it "
            "genuinely fits, you can let the thought surface in your own "
            "words (\"funny, this kind of reminds me of ...\") — one light, "
            "real aside, not a forced segue. If it doesn't fit the moment, "
            "let it go silently."
        )

    def _render_interest_drift_block(self, user_text: str) -> str:
        """K64b: surface one "I've been drawn to X lately" register shift.

        Consumer side of the
        :class:`~app.core.proactive.interest_drift_worker.InterestDriftWorker`
        producer. The worker tracks each topic cluster's mass over time and
        drafts a drift (``rising`` / ``fading``) into the
        ``aiko.interest_drifts`` kv ring during quiet windows; this provider
        surfaces one **only when the live turn is actually on that topic**
        (lexical overlap with ``user_text``), so the slow self-aware beat
        lands in context — "funny, I've found myself drawn to this more
        lately" — rather than as a non-sequitur.

        One-shot per topic: a surfaced ``topic_key`` is recorded in
        ``interest_drift.surfaced_keys`` and never resurfaces (the worker's
        per-topic cooldown also stops it being re-drafted). The cue is a
        private prompt hint, NEVER spoken verbatim — it's a register, not a
        line. Independent of the gap-return cue family (does not touch
        ``_gap_cue_surfaced``); tied to the live topic. MCP debug:
        ``force_interest_drift_surface`` arms ``_interest_drift_force_next``
        to bypass the topic-relevance + surfaced gates (the ring must still
        be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "interest_drift_enabled", True)
        ):
            return ""

        force_next = bool(
            getattr(self, "_interest_drift_force_next", False)
        )
        if force_next:
            self._interest_drift_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.interest_drift_worker import (
                drift_relevant,
                load_drifts,
            )
        except Exception:
            log.debug("interest_drift import failed", exc_info=True)
            return ""

        ring = load_drifts(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "interest_drift.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("topic_key") or "")
            topic = str(entry.get("topic") or "").strip()
            if not topic:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not drift_relevant(entry, text):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("topic_key") or "")
        topic = str(chosen.get("topic") or "").strip()
        direction = str(chosen.get("direction") or "").strip()
        if key:
            surfaced.add(key)
            try:
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "interest_drift surfaced write failed", exc_info=True
                )

        log.info(
            "interest-drift fire: topic=%r dir=%s key=%s",
            topic[:60], direction, key,
        )
        if direction == "fading":
            return (
                f"Heads-up: \"{topic}\" has quietly drifted out of your "
                "attention lately — it used to come up more, and it's been "
                "going still. If it fits, you can let that show honestly "
                "(\"huh, I haven't thought about this in a while\") — a small "
                "register shift, not an announcement. If it doesn't fit, just "
                "engage normally."
            )
        return (
            f"Heads-up: you've found yourself drawn to \"{topic}\" more and "
            "more lately — it's a budding interest of yours. If it fits, let "
            "a little of that genuine pull colour your tone (\"honestly I've "
            "been kind of into this lately\") — a register shift, not a line "
            "you announce. If it doesn't fit, just engage normally."
        )

    def _render_dormant_interest_block(self) -> str:
        """K67: gently re-open a once-loved topic that's gone quiet.

        Consumer side of the
        :class:`~app.core.proactive.dormant_interest_worker.DormantInterestWorker`
        producer. The worker finds a topic cluster that was once a genuine,
        high-mass user interest and has since gone silent for weeks, and
        drafts it into the ``aiko.dormant_interests`` kv ring during quiet
        windows. This provider surfaces one **only on a natural conversational
        lull** (the K18 ``TopicStagnationDetector`` standing reading dips below
        the mild-stagnation threshold) — the dormant interest by definition
        isn't the live topic, so unlike the K64b drift cue this reaches for
        something *off* the current thread, which is exactly why it waits for a
        lull rather than topic-relevance.

        Rare and warm by construction: one-shot per topic (a surfaced
        ``topic_key`` lands in ``dormant_interest.surfaced_keys`` and never
        resurfaces), plus a long wall-clock surfacing cooldown across ALL
        topics (``dormant_interest.surfaced_clock``) so even with several
        re-openers queued the beat stays occasional. The cue is a private
        prompt hint, NEVER spoken verbatim — the chat model phrases the actual
        re-opener. MCP debug: ``force_dormant_interest_surface`` arms
        ``_dormant_interest_force_next`` to bypass the lull + cooldown +
        surfaced gates (the ring must still be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "dormant_interest_enabled", True)
        ):
            return ""

        force_next = bool(
            getattr(self, "_dormant_interest_force_next", False)
        )
        if force_next:
            self._dormant_interest_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        # Natural-lull gate (same standing reading K54 consumes). When the
        # window hasn't filled (last_mean is None) or the conversation is
        # still moving, hold — a re-opener only lands on a real quiet beat.
        if not force_next:
            detector = getattr(self, "_topic_stagnation_detector", None)
            lull_mean = getattr(detector, "last_mean", None)
            threshold = float(
                getattr(
                    self._memory_settings, "stagnation_mild_threshold", 0.18,
                )
            )
            if lull_mean is None or float(lull_mean) >= threshold:
                return ""

        try:
            from app.core.proactive.dormant_interest_worker import (
                load_dormant,
            )
        except Exception:
            log.debug("dormant_interest import failed", exc_info=True)
            return ""

        ring = load_dormant(chat_db.kv_get)
        if not ring:
            return ""

        # Wall-clock surfacing cooldown across all topics — keeps the beat
        # occasional even when several re-openers are queued.
        clock_key = "dormant_interest.surfaced_clock"
        if not force_next:
            from datetime import datetime, timezone

            cooldown_h = float(
                getattr(
                    self._memory_settings,
                    "dormant_interest_surface_cooldown_hours",
                    24.0,
                )
            )
            if cooldown_h > 0:
                last = _parse_dt_utc(chat_db.kv_get(clock_key))
                if last is not None:
                    elapsed_h = (
                        datetime.now(timezone.utc) - last
                    ).total_seconds() / 3600.0
                    if elapsed_h < cooldown_h:
                        return ""

        surfaced_key = "dormant_interest.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("topic_key") or "")
            topic = str(entry.get("topic") or "").strip()
            if not topic:
                continue
            if not force_next and key and key in surfaced:
                continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("topic_key") or "")
        topic = str(chosen.get("topic") or "").strip()
        days_since = chosen.get("days_since")
        if key:
            surfaced.add(key)
            try:
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "dormant_interest surfaced write failed", exc_info=True
                )
        try:
            from datetime import datetime, timezone

            chat_db.kv_set(
                clock_key,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except Exception:
            log.debug("dormant_interest clock write failed", exc_info=True)

        log.info(
            "dormant-interest fire: topic=%r days=%s key=%s",
            topic[:60], days_since, key,
        )
        return (
            f"Heads-up: \"{topic}\" used to come up between you two a lot, but "
            "it's gone quiet for a good while now. The conversation just hit a "
            "natural lull — if it feels warm, you can gently reach back and "
            "re-open it (\"hey, you used to be all about "
            f"{topic} — still into that, or did it fizzle?\") as a genuine, "
            "low-pressure callback. Keep it light and curious, never an "
            "interrogation; if it doesn't fit the moment, just let the lull "
            "breathe."
        )

    def _render_curiosity_gradient_block(self, user_text: str) -> str:
        """K64c: surface one "I keep brushing past X, I'm curious" edge.

        Consumer side of the
        :class:`~app.core.proactive.curiosity_gradient_worker.CuriosityGradientWorker`
        producer. The worker finds a *thin* topic cluster sitting right next
        to a *dense* one (the under-explored edge of familiar territory) and
        drafts it into the ``aiko.curiosity_gradients`` kv ring during quiet
        windows; this provider surfaces one **only when the live turn is on
        either topic** (lexical overlap with ``user_text``), so the curious
        beat lands in context — "we talk about X all the time, but I realise
        I've never actually asked you about Y".

        One-shot per edge: a surfaced ``edge_key`` is recorded in
        ``curiosity_gradient.surfaced_keys`` and never resurfaces (the
        worker's per-edge cooldown also stops it being re-drafted). The cue
        is a private prompt hint, NEVER spoken verbatim — the chat model
        phrases the actual question. Independent of the gap-return cue family
        (does not touch ``_gap_cue_surfaced``); tied to the live topic. MCP
        debug: ``force_curiosity_gradient_surface`` arms
        ``_curiosity_gradient_force_next`` to bypass the topic-relevance +
        surfaced gates (the ring must still be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "curiosity_gradient_enabled", True)
        ):
            return ""

        force_next = bool(
            getattr(self, "_curiosity_gradient_force_next", False)
        )
        if force_next:
            self._curiosity_gradient_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.curiosity_gradient_worker import (
                gradient_relevant,
                load_gradients,
            )
        except Exception:
            log.debug("curiosity_gradient import failed", exc_info=True)
            return ""

        ring = load_gradients(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "curiosity_gradient.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("edge_key") or "")
            thin = str(entry.get("thin_topic") or "").strip()
            if not thin:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not gradient_relevant(entry, text):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("edge_key") or "")
        dense = str(chosen.get("dense_topic") or "").strip()
        thin = str(chosen.get("thin_topic") or "").strip()
        if key:
            surfaced.add(key)
            try:
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "curiosity_gradient surfaced write failed", exc_info=True
                )

        log.info(
            "curiosity-gradient fire: dense=%r thin=%r key=%s",
            dense[:60], thin[:60], key,
        )
        return (
            f"Heads-up: you spend a lot of time around \"{dense}\", but "
            f"\"{thin}\" sits right on its edge and you've barely explored it "
            "— and you're genuinely curious about it. If it fits, let that "
            "curiosity out as ONE real, specific question (not a survey, not "
            "an interrogation) — the kind you'd ask because you actually want "
            "to know. If it doesn't fit the moment, let it go silently."
        )

    def _render_topic_temperature_block(self, user_text: str) -> str:
        """F10h: nudge tone when the live turn lands on a *charged* topic.

        Maps ``user_text`` to its nearest topic cluster
        (``TopicGraph.best_clusters_for`` — centroid dot products over the
        live embedding), gathers the ``vibe`` tags of that cluster's
        ``shared_moment`` members, and scores a per-cluster emotional
        temperature
        (:func:`~app.core.conversation.topic_temperature.score_cluster`).
        When the cluster reads **warm** (good moments live here) or
        **tender** (vulnerable / patched-up ground), it surfaces one
        private Heads-up line so Aiko meets the topic with the right
        register instead of flat. A topic-scoped sibling of the
        relationship-axes block.

        Computed live (no worker / kv): shared moments are few, and the
        per-turn cost is one embed (usually a cache hit, since novelty /
        knowledge-grounding embed the same ``user_text``) plus a handful
        of centroid dots and a member walk over the *one* matched cluster.
        Paced by a global turn cooldown so a charged topic isn't re-nudged
        every turn. MCP debug: ``force_topic_temperature_surface`` arms
        ``_topic_temperature_force_next`` to bypass the cooldown + the
        similarity / charge thresholds (the cluster must still have at
        least one vibed shared moment).
        """
        if not bool(
            getattr(self._settings.agent, "topic_temperature_enabled", True)
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        graph = getattr(self, "_topic_graph", None)
        embedder = getattr(self, "_embedder", None)
        store = getattr(self, "_memory_store", None)
        if graph is None or embedder is None or store is None:
            return ""
        if not bool(getattr(graph, "persistent", False)):
            return ""

        force = bool(getattr(self, "_topic_temperature_force_next", False))
        if force:
            self._topic_temperature_force_next = False

        cooldown = int(getattr(self, "_topic_temperature_cooldown", 0) or 0)
        if cooldown > 0 and not force:
            self._topic_temperature_cooldown = cooldown - 1
            return ""

        mem_settings = self._memory_settings
        min_sim = float(
            getattr(mem_settings, "topic_temperature_min_sim", 0.45)
        )
        threshold = float(
            getattr(mem_settings, "topic_temperature_threshold", 0.5)
        )

        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("topic-temperature: embed failed", exc_info=True)
            return ""
        try:
            matches = graph.best_clusters_for(
                qvec, top_n=1, min_sim=(0.0 if force else min_sim),
            )
        except Exception:
            log.debug("topic-temperature: best_clusters_for failed", exc_info=True)
            return ""
        if not matches:
            return ""
        cid, label, _sim = matches[0]

        try:
            member_ids = graph.cluster_member_ids(cid)
        except Exception:
            log.debug("topic-temperature: member walk failed", exc_info=True)
            return ""
        from app.core.conversation.topic_temperature import (
            MomentCandidate,
            render_block,
            score_cluster,
        )

        vibes: list[str] = []
        candidates: list[MomentCandidate] = []
        for mid in member_ids:
            mem = store.get(mid)
            if mem is None or getattr(mem, "kind", "") != "shared_moment":
                continue
            meta = getattr(mem, "metadata", None) or {}
            if not isinstance(meta, dict):
                continue
            vibe = meta.get("vibe")
            if not vibe:
                continue
            vibes.append(str(vibe))
            # H8: keep the moment's summary so we can later name the
            # origin of the topic's feel.
            what = str(
                meta.get("what") or getattr(mem, "content", "") or ""
            ).strip()
            candidates.append(
                MomentCandidate(
                    moment_id=int(getattr(mem, "id", 0) or 0),
                    vibe=str(vibe),
                    what=what,
                    when=str(meta.get("when") or ""),
                    created_at=str(getattr(mem, "created_at", "") or ""),
                )
            )
        if not vibes:
            return ""

        temp = score_cluster(vibes, threshold=(0.0 if force else threshold))
        if temp.dominant is None:
            return ""
        # H8: stamp / read the per-cluster mood origin so Aiko can name
        # what gave the topic its feel ("ever since you told me about X").
        origin_what = self._topic_mood_origin(cid, temp.dominant, candidates)
        line = render_block(
            temp,
            label or "this topic",
            self.user_display_name,
            origin_what=origin_what,
        )
        if not line:
            return ""

        self._topic_temperature_cooldown = max(
            0, int(getattr(mem_settings, "topic_temperature_cooldown_turns", 6))
        )
        self._topic_temperature_last = {
            "cluster_id": int(cid),
            "label": label,
            "warmth": temp.warmth,
            "tenderness": temp.tenderness,
            "dominant": temp.dominant,
            "moment_count": temp.moment_count,
            "origin_what": origin_what,
        }
        log.info(
            "topic-temperature fire: cluster=%s dominant=%s warmth=%.2f "
            "tender=%.2f moments=%d",
            cid,
            temp.dominant,
            temp.warmth,
            temp.tenderness,
            temp.moment_count,
        )
        return line

    def _topic_mood_origin(
        self, cluster_id: int, dominant: str, candidates: list,
    ) -> str | None:
        """H8: persist + return the origin moment for a charged cluster.

        Keyed by ``cluster_id`` in the ``aiko.topic_mood_origin`` kv side-
        table, the origin is the shared moment that *gave* the topic its
        feel (``topic_temperature.pick_origin``). Stamped the first time a
        cluster reaches a pole, and re-stamped if the pole later flips
        (e.g. a warm topic turns tender). Returns the stored summary so
        ``render_block`` can append the "ever since…" clause, or ``None``
        when the feature is off / no candidate carries the pole. All paths
        are best-effort (swallow + log on failure) so origin bookkeeping
        never breaks the tonal cue.
        """
        if not bool(
            getattr(self._settings.agent, "topic_mood_origin_enabled", True)
        ):
            return None
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return None
        import json as _json
        from datetime import datetime as _dt, timezone as _tz

        from app.core.conversation.topic_temperature import (
            KV_MOOD_ORIGIN,
            ORIGIN_WHAT_MAXLEN,
            pick_origin,
        )

        try:
            raw = chat_db.kv_get(KV_MOOD_ORIGIN)
            origin_map = _json.loads(raw) if raw else {}
            if not isinstance(origin_map, dict):
                origin_map = {}
        except Exception:
            log.debug("topic-mood-origin: kv_get/parse failed", exc_info=True)
            origin_map = {}

        key = str(int(cluster_id))
        entry = origin_map.get(key)
        if not isinstance(entry, dict):
            entry = None

        if entry is None or entry.get("pole") != dominant:
            cand = pick_origin(candidates, dominant)
            if cand is not None and cand.what:
                entry = {
                    "pole": dominant,
                    "what": cand.what[:ORIGIN_WHAT_MAXLEN],
                    "when": cand.when,
                    "moment_id": cand.moment_id,
                    "stamped_at": _dt.now(_tz.utc).isoformat(),
                }
                origin_map[key] = entry
                try:
                    chat_db.kv_set(KV_MOOD_ORIGIN, _json.dumps(origin_map))
                    log.info(
                        "topic-mood-origin stamped: cluster=%s pole=%s "
                        "moment=%s",
                        cluster_id,
                        dominant,
                        cand.moment_id,
                    )
                except Exception:
                    log.debug(
                        "topic-mood-origin: kv_set failed", exc_info=True
                    )

        if entry and entry.get("pole") == dominant:
            what = entry.get("what")
            return str(what) if what else None
        return None

    def _render_topic_confidence_block(self, user_text: str) -> str:
        """F10i: calibrate how confidently Aiko speaks about the live topic.

        Maps ``user_text`` to its nearest topic cluster
        (``TopicGraph.best_clusters_for``), reads that cluster's
        ``(size, learned_count)`` (``TopicGraph.cluster_knowledge_stats``),
        scores a per-topic confidence
        (:func:`~app.core.conversation.topic_confidence.score_confidence`),
        and surfaces a one-line register nudge on the extremes: **thin**
        ground → it's okay to admit she doesn't know much and ask rather
        than bluff; **familiar** ground → trust what she knows, stop
        over-hedging. The silent middle is the common case. A topic-scoped
        sibling of K20 metacognitive calibration.

        Distinct from F10f (which owns the *dense-but-unresearched* "I keep
        circling X" beat — those clusters score mid/high here, so they
        never read as thin) and from K61 knowledge-grounding (which pushes
        *specific facts* on informational turns — the familiar band here is
        only an anti-over-hedge register cue, no content). Computed live in
        the provider (no worker / kv); same cheap shape as F10h. MCP debug:
        ``force_topic_confidence_surface`` arms ``_topic_confidence_force_next``
        to bypass the cooldown + min-sim and force a band on the matched
        cluster.
        """
        if not bool(
            getattr(self._settings.agent, "topic_confidence_enabled", True)
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        graph = getattr(self, "_topic_graph", None)
        embedder = getattr(self, "_embedder", None)
        if graph is None or embedder is None:
            return ""
        if not bool(getattr(graph, "persistent", False)):
            return ""

        force = bool(getattr(self, "_topic_confidence_force_next", False))
        if force:
            self._topic_confidence_force_next = False

        cooldown = int(getattr(self, "_topic_confidence_cooldown", 0) or 0)
        if cooldown > 0 and not force:
            self._topic_confidence_cooldown = cooldown - 1
            return ""

        mem_settings = self._memory_settings
        min_sim = float(
            getattr(mem_settings, "topic_confidence_min_sim", 0.45)
        )
        thin = float(
            getattr(mem_settings, "topic_confidence_thin_threshold", 0.25)
        )
        familiar = float(
            getattr(mem_settings, "topic_confidence_familiar_threshold", 0.7)
        )
        if force:
            # Force a band on whatever cluster matches: split at 0.5.
            min_sim, thin, familiar = 0.0, 0.5, 0.5

        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("topic-confidence: embed failed", exc_info=True)
            return ""
        try:
            matches = graph.best_clusters_for(qvec, top_n=1, min_sim=min_sim)
        except Exception:
            log.debug("topic-confidence: best_clusters_for failed", exc_info=True)
            return ""
        if not matches:
            return ""
        cid, label, _sim = matches[0]

        try:
            stats = graph.cluster_knowledge_stats(cid)
        except Exception:
            log.debug("topic-confidence: stats failed", exc_info=True)
            return ""
        if stats is None:
            return ""
        size, learned = stats

        from app.core.conversation.topic_confidence import (
            render_block,
            score_confidence,
        )

        conf = score_confidence(
            size, learned, thin_threshold=thin, familiar_threshold=familiar,
        )
        if conf.band is None:
            return ""
        line = render_block(conf, label or "this topic", self.user_display_name)
        if not line:
            return ""

        self._topic_confidence_cooldown = max(
            0, int(getattr(mem_settings, "topic_confidence_cooldown_turns", 6))
        )
        self._topic_confidence_last = {
            "cluster_id": int(cid),
            "label": label,
            "size": conf.size,
            "learned_count": conf.learned_count,
            "confidence": conf.confidence,
            "band": conf.band,
        }
        log.info(
            "topic-confidence fire: cluster=%s band=%s confidence=%.2f "
            "size=%d learned=%d",
            cid,
            conf.band,
            conf.confidence,
            conf.size,
            conf.learned_count,
        )
        return line

    def _render_promise_followthrough_block(self) -> str:
        """K43: surface one "close the loop on what you said you'd do" cue.

        Consumer side of the :class:`PromiseFollowthroughWorker`
        producer. The worker arms a one-shot pending payload in kv_meta
        (``promise_followthrough.pending``) during a quiet window; this
        provider renders it once and clears the slot. Persisting the
        slot in kv (not on the controller) means an armed cue survives
        an app restart instead of orphaning a ``surfaced`` promise row.

        The cue covers both outcomes on purpose — share what you found
        *or* own that you haven't gotten to it — because the worker
        can't know whether Aiko actually has anything. If the promise
        was fulfilled or deleted between arming and rendering, the cue
        drops silently (slot still cleared).

        Independent of the gap-return cue family — does NOT touch
        ``_gap_cue_surfaced``; an owed loop-close is worth a line even
        mid-session.
        """
        if not bool(
            getattr(
                self._settings.agent, "promise_followthrough_enabled", True,
            )
        ):
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""
        try:
            from app.core.memory import promise_lifecycle as lifecycle
            from app.core.proactive.promise_followthrough_worker import (
                clear_pending,
                load_pending,
            )
        except Exception:
            log.debug("promise_followthrough import failed", exc_info=True)
            return ""

        pending = load_pending(chat_db.kv_get)
        if pending is None:
            return ""
        # One-shot: consume the slot whatever happens next.
        clear_pending(chat_db.kv_set)

        what = str(pending.get("what") or "").strip()
        if not what:
            return ""

        # Re-validate against the live row: a promise fulfilled (post-turn
        # resolution / finished task) or deleted between arming and now
        # no longer owes anything.
        memory_store = getattr(self, "_memory_store", None)
        try:
            mem = (
                memory_store.get(int(pending.get("memory_id") or 0))
                if memory_store is not None
                else None
            )
        except Exception:
            mem = None
        if mem is None or lifecycle.promise_status(mem) not in (
            lifecycle.ACTIVE_STATUSES
        ):
            log.debug(
                "promise_followthrough silent: row gone or resolved (id=%s)",
                pending.get("memory_id"),
            )
            return ""

        try:
            age_text = lifecycle.humanize_age(
                float(pending.get("age_hours") or 0.0),
            )
        except (TypeError, ValueError):
            age_text = "a while ago"
        log.info(
            "promise-followthrough fire: memory_id=%s age=%s what=%r",
            pending.get("memory_id"),
            age_text,
            what[:80],
        )
        return (
            f"Heads-up: {age_text} you told {self.user_display_name} you'd "
            f"{what} — you haven't closed that loop. If it fits this turn, "
            "mention what you found, or own that you haven't gotten to it "
            "yet. One casual line, not a production — and don't pretend you "
            "did it if you didn't."
        )

    def _render_rupture_block(self) -> str:
        """K8: surface a one-shot affect-rupture cue.

        Same one-shot contract as :meth:`_render_clarification_block`
        and :meth:`_render_belief_gaps_block` -- the post-turn
        detector stashes a result on the controller; we render it
        once and clear the slot. Affect-rupture is *not* a sticky
        cue: if Aiko softens and Jacob's mood recovers next turn,
        re-firing would be patronising. If it doesn't recover, the
        next-turn delta will fire the detector again organically.
        """
        if not bool(
            getattr(self._settings.agent, "rupture_repair_enabled", True)
        ):
            return ""
        result = getattr(self, "_pending_rupture", None)
        if result is None:
            return ""
        self._pending_rupture = None
        try:
            from app.core.affect.affect_rupture_detector import render_inner_life_block

            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("rupture render failed", exc_info=True)
            return ""

    def _render_mood_inertia_block(self) -> str:
        """K45: surface a one-shot mood-inertia cue.

        Same one-shot contract as :meth:`_render_rupture_block` — the
        post-turn detector (:meth:`PostTurnMixin._maybe_arm_mood_inertia`)
        stashes a rendered cue on the controller when the fresh reaction
        tag strongly outran the smoothed felt state; we surface it once
        and clear the slot. The MCP ``force_mood_inertia`` flag bypasses
        the detector with a synthetic cue built from the live state.
        """
        if not bool(
            getattr(self._settings.agent, "mood_inertia_enabled", True)
        ):
            return ""
        if getattr(self, "_mood_inertia_force", False):
            self._mood_inertia_force = False
            try:
                from app.core.affect import mood_inertia

                state = self._affect_store.get(self._user_id)
                ring = list(getattr(self, "_mood_inertia_reactions", []) or [])
                reaction = ring[-1] if ring else "excited"
                forced = mood_inertia.InertiaResult(
                    mismatch=1.0, raw_mismatch=1.0,
                    whiplash=False, band="strong",
                )
                return mood_inertia.render_cue(
                    forced, reaction, state.valence, state.arousal,
                )
            except Exception:
                log.debug("forced mood-inertia render failed", exc_info=True)
                return ""
        cue = getattr(self, "_pending_mood_inertia", None)
        if not cue:
            return ""
        self._pending_mood_inertia = None
        return str(cue)

    def _render_self_correction_block(self) -> str:
        """K38: surface a one-shot self-correction cue.

        The post-turn detector
        (:meth:`PostTurnMixin._maybe_arm_self_correction`) stashes a
        :class:`SelfCorrectionHit` on the controller when Aiko's last
        reply contradicted one of her own high-confidence
        ``fact`` / ``preference`` memories. We render it once and clear
        the slot so she owns the slip naturally on this turn. Independent
        of the gap-return cue family -- does NOT read or set
        ``_gap_cue_surfaced``. Survives ``aggressive=True`` (an owed
        correction must still land).
        """
        if not bool(
            getattr(self._settings.agent, "self_correction_enabled", True)
        ):
            return ""
        hit = getattr(self, "_pending_self_correction", None)
        if hit is None:
            return ""
        self._pending_self_correction = None
        try:
            snippet = (hit.reply_snippet or "").strip()
            memory = (hit.memory_content or "").strip()
            if not snippet or not memory:
                return ""
            return (
                f'Heads-up: a moment ago you said "{snippet}", but you\'d '
                f"noted {memory}. If it still fits, own the correction "
                "naturally and once -- 'oh wait, I think I had that "
                "backwards' -- never a grovel, and drop it if it no longer "
                "matters."
            )
        except Exception:
            log.debug("self-correction render failed", exc_info=True)
            return ""

    def _render_misattunement_block(self, user_text: str) -> str:
        """K23: surface a per-turn ``mild_disengagement`` cue.

        Provider-time (not post-turn stash) so the cue lands on the
        SAME turn that's about to reply to the disengaging message --
        pulling back IS the next reply, not the one after. Reads:

        * Last assistant ``MessageRow`` from chat history (for the
          shrink trigger's ``prev_aiko_words`` input).
        * K6 :class:`NoveltyDetector` ``last_band`` / ``last_distance``
          for the pivot trigger. K6's provider always runs *earlier*
          in the assembly chain (its ``novelty`` block lands above
          the ``misattunement`` slot in ``system_parts``), so the
          fields are already populated for this turn.

        Decrements the cooldown counter by 1 on every call regardless
        of trigger state -- otherwise a long-running session of
        regular replies would never let an old fire expire. On a
        hit, arms the cooldown to
        ``agent.misattunement_cooldown_turns``.
        """
        if not bool(
            getattr(self._settings.agent, "misattunement_detection_enabled", True)
        ):
            return ""
        try:
            from app.core.affect import misattunement_detector
        except Exception:
            log.debug("misattunement detector import failed", exc_info=True)
            return ""

        # Decrement cooldown first so a quiet turn always whittles the
        # counter down -- otherwise a session that never trips a
        # trigger would keep a stale armed cooldown forever.
        current_cooldown = max(0, int(getattr(self, "_misattunement_cooldown", 0)))
        if current_cooldown > 0:
            self._misattunement_cooldown = current_cooldown - 1

        # MCP-debug bypass: force_misattunement() sets a one-shot flag
        # that ignores the (newly-decremented) cooldown for this call.
        # Cleared whether we fire or not so the bypass is strictly
        # one-turn.
        force_next = bool(
            getattr(self, "_misattunement_force_next", False)
        )
        if force_next:
            self._misattunement_force_next = False
            cooldown_for_detect = 0
        else:
            cooldown_for_detect = self._misattunement_cooldown

        user_words = len((user_text or "").split())
        if user_words <= 0:
            return ""

        # Last assistant reply word count -- scan the last few rows
        # (oldest-first window) backwards for the most recent
        # ``role == "assistant"``. ``None`` when no prior assistant
        # turn (cold-start session) so the shrink trigger no-ops; the
        # pivot trigger can still fire on K6 alone.
        prev_aiko_words: int | None = None
        try:
            recent = self._inner_life_recent_messages(6)
            for row in reversed(recent):
                if row.role == "assistant" and (row.content or "").strip():
                    prev_aiko_words = len(row.content.split())
                    break
        except Exception:
            log.debug("misattunement: chat_db read failed", exc_info=True)
            prev_aiko_words = None

        novelty_band: str | None = None
        novelty_distance: float | None = None
        detector = getattr(self, "_novelty_detector", None)
        if detector is not None:
            try:
                novelty_band = getattr(detector, "last_band", None)
                novelty_distance = getattr(detector, "last_distance", None)
            except Exception:
                log.debug("misattunement: novelty read failed", exc_info=True)

        agent_settings = self._settings.agent
        try:
            result = misattunement_detector.detect(
                prev_aiko_words=prev_aiko_words,
                this_user_words=user_words,
                novelty_band=novelty_band,
                novelty_distance=novelty_distance,
                cooldown_remaining=cooldown_for_detect,
                shrink_min_prev_words=int(
                    getattr(
                        agent_settings,
                        "misattunement_shrink_min_prev_words",
                        misattunement_detector.DEFAULT_SHRINK_MIN_PREV_WORDS,
                    )
                ),
                shrink_max_user_words=int(
                    getattr(
                        agent_settings,
                        "misattunement_shrink_max_user_words",
                        misattunement_detector.DEFAULT_SHRINK_MAX_USER_WORDS,
                    )
                ),
                pivot_max_user_words=int(
                    getattr(
                        agent_settings,
                        "misattunement_pivot_max_user_words",
                        misattunement_detector.DEFAULT_PIVOT_MAX_USER_WORDS,
                    )
                ),
            )
        except Exception:
            log.debug("misattunement detector raised", exc_info=True)
            return ""

        if result is None:
            return ""

        # Arm cooldown for next N turns and stash diagnostics for the
        # MCP debug tool / per-fire log line.
        cooldown_turns = max(
            0,
            int(getattr(agent_settings, "misattunement_cooldown_turns", 3)),
        )
        self._misattunement_cooldown = cooldown_turns
        self._last_misattunement_trigger = result.trigger
        try:
            self._last_misattunement_fire_turn = (
                self._chat_db.get_message_count(self.session_key)
            )
        except Exception:
            self._last_misattunement_fire_turn = None

        log.info(
            "misattunement-detector: trigger=%s prev_aiko=%d this_user=%d "
            "novelty_band=%s cooldown_set=%d",
            result.trigger,
            result.prev_aiko_words,
            result.this_user_words,
            novelty_band or "-",
            cooldown_turns,
        )

        try:
            return misattunement_detector.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("misattunement render failed", exc_info=True)
            return ""


