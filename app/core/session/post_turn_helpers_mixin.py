"""Post-turn helper methods feeding ``_post_turn_inner_life``.

Split out of :mod:`app.core.session.post_turn_mixin` to keep both files
under the size budget. State ownership stays in SessionController.

NB: patch ``app.core.session.post_turn_helpers_mixin.<symbol>`` for any
symbol looked up by these methods.
"""
from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("app.session")

# J6: kv_meta watermark so one extended rough patch doesn't spawn several
# repair moments in quick succession.
_KV_CONFLICT_REPAIR_AT = "conflict_repair.last_recorded_at"


class PostTurnHelpersMixin:
    """Slot-arming, promise/tease/emotion, curiosity + knowledge-gap
    resolution, revival detection, and the per-turn affect/balance updates
    that ``_post_turn_inner_life`` orchestrates."""

    def _maybe_arm_turning_over_slot(self, engagement: Any) -> None:
        """K28: stash ``latency_seconds`` on ``_pending_turning_over_seconds``
        when the turn qualifies.

        Gates (all must pass):

        * Master switch ``agent.turning_over_enabled`` is on.
        * Engagement mode is ``"typed"`` (voice turns never arm K28
          — same gating as K14).
        * ``engagement.latency_seconds`` is a positive number (cold-
          start engagements report ``None``).
        * Latency clears ``memory.turning_over_min_gap_minutes * 60``
          (defensive floor on the parser clamp).

        On a passing turn, sets ``self._pending_turning_over_seconds``
        to the latency value; the next prompt assembly's provider
        reads + clears the slot and runs the picker. The slot is NOT
        cleared here on a failing gate — that preserves any value
        stashed by a previous turn (i.e. an unconsumed cue waiting
        for the next prompt).
        """
        if engagement is None:
            return
        if not bool(
            getattr(self._settings.agent, "turning_over_enabled", True)
        ):
            return
        mode = getattr(engagement, "mode", None)
        if mode != "typed":
            return
        latency = getattr(engagement, "latency_seconds", None)
        if latency is None:
            return
        try:
            latency_f = float(latency)
        except (TypeError, ValueError):
            return
        if latency_f <= 0.0:
            return
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
        if latency_f >= min_gap_s:
            self._pending_turning_over_seconds = latency_f

    def _maybe_arm_away_activities_slot(self, engagement: Any) -> None:
        """K36: stash ``latency_seconds`` on
        ``_pending_away_activities_seconds`` when the turn follows a long
        typed gap.

        Mirror of :meth:`_maybe_arm_turning_over_slot` with its own
        master switch (``agent.away_activities_enabled``) and threshold
        (``memory.away_activities_min_gap_hours``, default 4h — longer
        than K28's 90 min). Voice turns never arm K36. The provider
        (:meth:`InnerLifeProvidersMixin._render_away_activities_block`)
        reads + clears the slot and defers to ``turning_over`` so at
        most one gap cue surfaces per return.
        """
        if engagement is None:
            return
        if not bool(
            getattr(self._settings.agent, "away_activities_enabled", True)
        ):
            return
        mode = getattr(engagement, "mode", None)
        if mode != "typed":
            return
        latency = getattr(engagement, "latency_seconds", None)
        if latency is None:
            return
        try:
            latency_f = float(latency)
        except (TypeError, ValueError):
            return
        if latency_f <= 0.0:
            return
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
        if latency_f >= min_gap_s:
            self._pending_away_activities_seconds = latency_f

    def _maybe_arm_sleep_return_slot(self, engagement: Any) -> None:
        """H21: stash ``latency_seconds`` on
        ``_pending_sleep_return_seconds`` when the turn follows a long
        typed gap that might have spanned an overnight sleep.

        Mirror of :meth:`_maybe_arm_away_activities_slot` with its own
        master switch (``agent.sleep_return_enabled``) and threshold
        (``memory.sleep_return_min_gap_hours``, default 5h — longer than
        the ordinary away cue so a long afternoon out never arms it). The
        provider (:meth:`InnerLifeProvidersMixin._render_sleep_return_block`)
        applies the finer overnight gate (return-hour aware) and defers to
        ``turning_over`` so at most one gap cue surfaces per return. Voice
        turns never arm H21.
        """
        if engagement is None:
            return
        if not bool(
            getattr(self._settings.agent, "sleep_return_enabled", True)
        ):
            return
        mode = getattr(engagement, "mode", None)
        if mode != "typed":
            return
        latency = getattr(engagement, "latency_seconds", None)
        if latency is None:
            return
        try:
            latency_f = float(latency)
        except (TypeError, ValueError):
            return
        if latency_f <= 0.0:
            return
        min_gap_s = (
            float(
                getattr(
                    self._memory_settings,
                    "sleep_return_min_gap_hours",
                    5.0,
                )
            )
            * 3600.0
        )
        if latency_f >= min_gap_s:
            self._pending_sleep_return_seconds = latency_f

    def _maybe_arm_forward_curiosity_slot(self, engagement: Any) -> None:
        """K34: stash ``latency_seconds`` on
        ``_pending_forward_curiosity_seconds`` when the turn follows a
        long typed gap.

        Mirror of :meth:`_maybe_arm_away_activities_slot` with its own
        master switch (``agent.forward_curiosity_enabled``) and threshold
        (``memory.forward_curiosity_min_gap_hours``, default 4h). Voice
        turns never arm K34. The provider
        (:meth:`InnerLifeProvidersMixin._render_forward_curiosity_block`)
        reads + clears the slot and defers to ``turning_over`` /
        ``away_activities`` so at most one gap cue surfaces per return.
        """
        if engagement is None:
            return
        if not bool(
            getattr(self._settings.agent, "forward_curiosity_enabled", True)
        ):
            return
        mode = getattr(engagement, "mode", None)
        if mode != "typed":
            return
        latency = getattr(engagement, "latency_seconds", None)
        if latency is None:
            return
        try:
            latency_f = float(latency)
        except (TypeError, ValueError):
            return
        if latency_f <= 0.0:
            return
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
        if latency_f >= min_gap_s:
            self._pending_forward_curiosity_seconds = latency_f

    def _maybe_resolve_promises(self, text: str, *, source: str = "reply") -> int:
        """K43: mark assistant promises this text plausibly delivered on.

        Lexical only (content-word overlap via
        :func:`promise_lifecycle.find_fulfilled`) — when Aiko's reply
        (or a finished background task, via the task-orchestration
        mixin) covers the body of an ``open`` / ``surfaced``
        assistant-side promise, the row flips to ``fulfilled`` and
        :meth:`note_promise_kept` fires so the relationship axes /
        moment detector see the kept-promise signal. Returns the number
        of promises resolved. Best-effort everywhere.
        """
        if not bool(
            getattr(
                self._settings.agent, "promise_followthrough_enabled", True,
            )
        ):
            return 0
        memory_store = getattr(self, "_memory_store", None)
        if memory_store is None:
            return 0
        body = (text or "").strip()
        if not body:
            return 0
        try:
            from datetime import datetime, timezone

            from app.core.memory import promise_lifecycle as lifecycle

            promises = memory_store.iter_by_kind("promise")
            if not promises:
                return 0
            fulfilled = lifecycle.find_fulfilled(
                promises,
                body,
                min_overlap=int(
                    getattr(
                        self._memory_settings,
                        "promise_fulfil_min_overlap",
                        3,
                    )
                ),
            )
            if not fulfilled:
                return 0
            now_iso = datetime.now(timezone.utc).isoformat()
            resolved = 0
            for mem in fulfilled:
                try:
                    memory_store.update(
                        mem.id,
                        metadata={
                            "promise_status": lifecycle.STATUS_FULFILLED,
                            "promise_resolved_at": now_iso,
                        },
                        metadata_merge=True,
                    )
                except Exception:
                    log.debug(
                        "promise fulfil update failed for id=%s",
                        mem.id,
                        exc_info=True,
                    )
                    continue
                resolved += 1
                log.info(
                    "promise fulfilled: memory_id=%s source=%s what=%r",
                    mem.id,
                    source,
                    lifecycle.promise_what(mem)[:80],
                )
            if resolved:
                self.note_promise_kept()
            return resolved
        except Exception:
            log.debug("promise resolution failed", exc_info=True)
            return 0

    # ── K57 directed emotion episodes ───────────────────────────────

    def _queue_emotion_trigger(
        self,
        *,
        emotion: str,
        cause: str,
        intensity: float,
        source: str,
    ) -> None:
        """K57: stage one episode trigger for the post-turn drain.

        Producers across the mixins call this (kept-promise hook, the
        lonely arm, K32 reaction warmth, the K55 pivot). Cheap and
        never raises — a lost trigger is a lost tint, not an error.
        """
        if not bool(
            getattr(self._settings.agent, "emotion_episodes_enabled", True)
        ):
            return
        try:
            queue = getattr(self, "_pending_emotion_triggers", None)
            if queue is None:
                queue = []
                self._pending_emotion_triggers = queue
            if len(queue) < 10:
                queue.append({
                    "emotion": str(emotion),
                    "cause": str(cause),
                    "intensity": float(intensity),
                    "source": str(source),
                })
        except Exception:
            log.debug("emotion trigger queue failed", exc_info=True)

    def _maybe_queue_lonely_episode(self, engagement: "Any") -> None:
        """K57: closeness-scaled loneliness from a long typed gap.

        Reads the raw ``latency_seconds`` (NOT the K14
        ``absence_seconds``, which is band-capped at ~4h and ``None``
        for the long gaps loneliness actually needs). Below the
        scaled threshold the pure helper returns 0.0 and nothing is
        queued — most gaps are just life.
        """
        try:
            latency = getattr(engagement, "latency_seconds", None)
            if latency is None or float(latency) <= 0.0:
                return
            from app.core.affect import emotion_episodes as _ee

            closeness = None
            axes_store = getattr(self, "_relationship_axes_store", None)
            if axes_store is not None:
                try:
                    closeness = float(
                        axes_store.get(self._user_id).closeness
                    )
                except Exception:
                    closeness = None
            gap_hours = float(latency) / 3600.0
            intensity = _ee.lonely_intensity(
                gap_hours,
                closeness,
                base_threshold_hours=float(
                    getattr(
                        self._settings.agent,
                        "emotion_lonely_threshold_hours",
                        5.0,
                    )
                ),
            )
            if intensity <= 0.0:
                return
            if gap_hours >= 36.0:
                duration = "a couple of days"
            elif gap_hours >= 20.0:
                duration = "about a day"
            elif gap_hours >= 9.0:
                duration = "most of the day"
            else:
                duration = "a good few hours"
            self._queue_emotion_trigger(
                emotion=_ee.EMOTION_LONELY,
                cause=f"they were gone {duration} and you noticed",
                intensity=intensity,
                source="absence",
            )
        except Exception:
            log.debug("lonely episode arm failed", exc_info=True)

    def _bank_tease_debt(
        self,
        *,
        what: str,
        context: str,
        source: str,
    ) -> bool:
        """K59: bank one mock-grudge into the kv-backed tease ledger.

        Called from the K29 opinion-injection fire site and the K57
        drain's light-offence lane. Best-effort; returns whether a
        row was actually added (dedupe / blank input refuse).
        """
        if not bool(
            getattr(self._settings.agent, "tease_economy_enabled", True)
        ):
            return False
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return False
        try:
            from datetime import datetime, timezone

            from app.core.relationship import tease_ledger as _tl

            now = datetime.now(timezone.utc)
            state = _tl.expire(
                _tl.deserialize(chat_db.kv_get(_tl.KV_TEASE_LEDGER)),
                now,
                expiry_days=float(
                    getattr(self._settings.agent, "tease_expiry_days", 14.0)
                ),
            )
            state, added = _tl.bank(
                state,
                what=what,
                context=context,
                source=source,
                now=now,
                cap=max(
                    1, int(getattr(self._settings.agent, "tease_cap", 5)),
                ),
            )
            chat_db.kv_set(_tl.KV_TEASE_LEDGER, _tl.serialize(state))
            if added:
                log.info(
                    "tease banked: source=%s what=%s",
                    source, what[:80],
                )
            return added
        except Exception:
            log.debug("tease bank failed", exc_info=True)
            return False

    def _settle_tease_debts(self, assistant_text: str) -> None:
        """K59: post-turn collection check on the offered ledger row.

        If the reply's content words overlap the row the provider
        offered this turn, the debt is deleted — repaid is done
        forever. A miss just clears the offered stamp so the row can
        come around again after the cooldown.
        """
        if not bool(
            getattr(self._settings.agent, "tease_economy_enabled", True)
        ):
            return
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return
        try:
            from app.core.relationship import tease_ledger as _tl

            state = _tl.deserialize(chat_db.kv_get(_tl.KV_TEASE_LEDGER))
            if not any(d.offered_at for d in state.debts):
                return
            state, settled = _tl.settle_if_collected(
                state, assistant_text,
            )
            chat_db.kv_set(_tl.KV_TEASE_LEDGER, _tl.serialize(state))
            if settled is not None:
                log.info(
                    "tease collected: what=%s source=%s",
                    settled.what[:80], settled.source,
                )
        except Exception:
            log.debug("tease settle failed", exc_info=True)

    def _drain_emotion_triggers(self) -> None:
        """K57: apply staged triggers to the kv-backed episode store.

        Single consumer. Applies decay first (so merges see current
        intensities), adds each trigger through the pure
        ``add_episode`` (warm_glow counter-events resolve inside),
        persists, then nudges the scalar affect layer with the small
        per-emotion impulses so the two systems agree.

        K59 lane-picker: a *light* miffed trigger (intensity below
        0.35) is comedy, not drama — it banks into the tease ledger
        instead of spawning a real episode, so a brushed-off thread
        becomes a callback bit rather than a sulk.
        """
        queue = getattr(self, "_pending_emotion_triggers", None)
        self._pending_emotion_triggers = []
        if not queue:
            return
        if not bool(
            getattr(self._settings.agent, "emotion_episodes_enabled", True)
        ):
            return
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return
        from datetime import datetime, timezone

        from app.core.affect import emotion_episodes as _ee

        if bool(
            getattr(self._settings.agent, "tease_economy_enabled", True)
        ):
            routed: list[dict] = []
            for trig in queue:
                if (
                    trig["emotion"] == _ee.EMOTION_MIFFED
                    and float(trig["intensity"]) < 0.35
                ):
                    self._bank_tease_debt(
                        what=trig["cause"],
                        context="",
                        source="light_offence",
                    )
                else:
                    routed.append(trig)
            queue = routed
            if not queue:
                return

        now = datetime.now(timezone.utc)
        state = _ee.apply_decay(
            _ee.deserialize(chat_db.kv_get(_ee.KV_EMOTION_EPISODES)), now,
        )
        cap = max(
            1, int(getattr(self._settings.agent, "emotion_episode_cap", 3)),
        )
        applied: list[dict] = []
        for trig in queue:
            before = state
            state = _ee.add_episode(
                state,
                emotion=trig["emotion"],
                cause=trig["cause"],
                intensity=trig["intensity"],
                source=trig["source"],
                now=now,
                cap=cap,
            )
            if state is not before:
                applied.append(trig)
                log.info(
                    "emotion-episode trigger: emotion=%s intensity=%.2f "
                    "source=%s cause=%s",
                    trig["emotion"], trig["intensity"],
                    trig["source"], trig["cause"][:80],
                )
        chat_db.kv_set(_ee.KV_EMOTION_EPISODES, _ee.serialize(state))

        # Feed the scalar affect layer one small clamped impulse per
        # applied trigger so the valence/arousal pair doesn't
        # contradict the episode the prompt is about to render.
        if applied:
            try:
                store = getattr(self, "_affect_store", None)
                if store is not None:
                    affect = store.get(self._user_id)
                    for trig in applied:
                        dv, da = _ee.AFFECT_IMPULSES.get(
                            trig["emotion"], (0.0, 0.0),
                        )
                        scale = max(0.0, min(1.0, trig["intensity"]))
                        affect.valence = max(
                            -1.0, min(1.0, affect.valence + dv * scale),
                        )
                        affect.arousal = max(
                            0.0, min(1.0, affect.arousal + da * scale),
                        )
                    store.save(affect)
            except Exception:
                log.debug("emotion affect impulse failed", exc_info=True)

    def _maybe_arm_self_correction(self, assistant_text: str) -> None:
        """K38: catch when Aiko's just-finished reply contradicts one of
        her own high-confidence ``fact`` / ``preference`` memories and arm
        a one-shot self-correction cue for the next turn.

        Embedding-free: the detector
        (:func:`app.core.conversation.self_correction_detector.detect_self_correction`)
        runs a content-word overlap shortlist + the shared F5 contradiction
        heuristic. Gated by ``agent.self_correction_enabled`` and a
        per-fire cooldown (``memory.self_correction_cooldown_turns``) so a
        single slip doesn't nag every turn. The cooldown counter decrements
        on every post-turn call; the detector only runs when it reaches 0.
        Independent of the gap-return cue family -- does NOT touch
        ``_gap_cue_surfaced``.
        """
        if not bool(
            getattr(self._settings.agent, "self_correction_enabled", True)
        ):
            return
        if getattr(self, "_self_correction_cooldown_remaining", 0) > 0:
            self._self_correction_cooldown_remaining -= 1
            return
        memory_store = getattr(self, "_memory_store", None)
        if memory_store is None:
            return
        text = (assistant_text or "").strip()
        if not text:
            return
        try:
            from app.core.conversation import self_correction_detector

            memories = list(memory_store.iter_by_kind("fact"))
            memories.extend(memory_store.iter_by_kind("preference"))
            if not memories:
                return
            hit = self_correction_detector.detect_self_correction(
                text,
                memories,
                min_confidence=float(
                    getattr(
                        self._memory_settings,
                        "self_correction_min_confidence",
                        0.6,
                    )
                ),
                min_overlap=int(
                    getattr(
                        self._memory_settings,
                        "self_correction_min_overlap",
                        2,
                    )
                ),
                max_candidates=int(
                    getattr(
                        self._memory_settings,
                        "self_correction_max_candidates",
                        50,
                    )
                ),
            )
            if hit is not None:
                self._pending_self_correction = hit
                self._self_correction_cooldown_remaining = int(
                    getattr(
                        self._memory_settings,
                        "self_correction_cooldown_turns",
                        3,
                    )
                )
                log.info(
                    "self-correction fire: memory_id=%s label=%s overlap=%d "
                    "snippet=%r",
                    hit.memory_id,
                    hit.label,
                    hit.overlap,
                    hit.reply_snippet,
                )
        except Exception:
            log.debug("self-correction detector raised", exc_info=True)

    def _maybe_arm_mood_inertia(
        self,
        *,
        reaction: str,
        affect_before: Any,
    ) -> None:
        """K45: arm the one-shot mood-inertia cue when the fresh reaction
        tag strongly outruns the pre-impulse smoothed affect.

        ``affect_before`` is the PRE-turn :class:`AffectState` snapshot
        (what Aiko still actually feels); the fresh tag's own impulse
        must not shrink its own mismatch. The reaction ring feeds
        whiplash detection and always advances, even on gated turns, so
        a swing across a cooldown window is still seen.
        """
        from app.core.affect import mood_inertia

        ring = getattr(self, "_mood_inertia_reactions", None)
        if ring is not None and reaction:
            ring.append(reaction)
        if not bool(
            getattr(self._settings.agent, "mood_inertia_enabled", True)
        ):
            return
        if affect_before is None or not reaction:
            return
        if getattr(self, "_mood_inertia_cooldown_remaining", 0) > 0:
            self._mood_inertia_cooldown_remaining -= 1
            return
        result = mood_inertia.assess(
            reaction,
            float(getattr(affect_before, "valence", 0.0)),
            float(getattr(affect_before, "arousal", 0.4)),
            list(ring or []),
            strong_threshold=float(
                getattr(
                    self._memory_settings,
                    "mood_inertia_mismatch_threshold",
                    mood_inertia.DEFAULT_STRONG_THRESHOLD,
                )
            ),
        )
        self._mood_inertia_last = {
            "reaction": reaction,
            "mismatch": result.mismatch,
            "raw_mismatch": result.raw_mismatch,
            "whiplash": result.whiplash,
            "band": result.band,
            "valence_before": float(getattr(affect_before, "valence", 0.0)),
            "arousal_before": float(getattr(affect_before, "arousal", 0.4)),
        }
        if result.band != "strong":
            return
        cue = mood_inertia.render_cue(
            result,
            reaction,
            float(getattr(affect_before, "valence", 0.0)),
            float(getattr(affect_before, "arousal", 0.4)),
        )
        if not cue:
            return
        self._pending_mood_inertia = cue
        self._mood_inertia_cooldown_remaining = max(
            0,
            int(
                getattr(
                    self._memory_settings,
                    "mood_inertia_cooldown_turns",
                    3,
                )
            ),
        )
        log.info(
            "mood-inertia fire: mismatch=%.2f band=%s whiplash=%s "
            "reaction=%s",
            result.mismatch,
            result.band,
            result.whiplash,
            reaction,
        )

    def _resolve_curiosity_seeds(  # noqa: C901
        self,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """K9: stamp ``consumed_at`` on any seed the turn drifted onto.

        Embeds the combined ``user_text + assistant_text`` once and
        cosines it against every active seed's stored embedding. Any
        seed scoring above
        ``agent.curiosity_seed_resolve_threshold`` (default 0.50) is
        marked consumed and demoted to ``archive`` so it stops
        eating the inner-life slot and no longer surfaces as a
        proactive candidate.

        No-op when the worker is disabled, when no active seeds
        exist, or when the embedder isn't available -- stays cheap
        on the cold path.
        """
        if not bool(
            getattr(self._settings.agent, "curiosity_seed_enabled", True)
        ):
            return
        memory = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        if memory is None or embedder is None:
            return
        try:
            seeds = memory.iter_by_kind("curiosity_seed")
        except Exception:
            return
        if not seeds:
            return
        active = [
            seed for seed in seeds
            if not (seed.metadata or {}).get("consumed_at")
            and seed.tier != "archive"
            and seed.embedding is not None
            and seed.embedding.size > 0
        ]
        if not active:
            return
        combined = " ".join(
            part for part in (user_text or "", assistant_text or "")
            if part and part.strip()
        ).strip()
        if not combined or len(combined) < 4:
            return
        try:
            turn_vec = embedder.embed(combined)
        except Exception:
            log.debug(
                "curiosity_seed resolve: embed failed", exc_info=True,
            )
            return
        if turn_vec is None or turn_vec.size == 0:
            return
        threshold = float(
            getattr(
                self._settings.agent,
                "curiosity_seed_resolve_threshold",
                0.50,
            )
        )
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        for seed in active:
            try:
                sim = float((turn_vec * seed.embedding).sum())
            except Exception:
                continue
            if sim < threshold:
                continue
            try:
                memory.update(
                    seed.id,
                    metadata={
                        "consumed_at": now_iso,
                        "consumed_similarity": round(sim, 4),
                    },
                    metadata_merge=True,
                    tier="archive",
                )
            except Exception:
                log.debug(
                    "curiosity_seed mark consumed failed (id=%s)",
                    seed.id,
                    exc_info=True,
                )
                continue
            log.info(
                "curiosity_seed resolved: id=%s sim=%.2f topic=%r",
                seed.id,
                sim,
                ((seed.metadata or {}).get("topic")
                 or seed.content or "")[:80],
            )
            try:
                fresh = memory.get(seed.id)
            except Exception:
                fresh = None
            if fresh is not None and self._notify_memory_updated is not None:
                try:
                    self._notify_memory_updated(fresh.to_dict())
                except Exception:
                    log.debug(
                        "curiosity_seed notify_updated failed",
                        exc_info=True,
                    )

    # ── F2.1: post-turn user-answer gap resolver ─────────────────────

    def _resolve_knowledge_gaps(  # noqa: C901
        self,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """F2.1: stamp ``resolved_at`` on any open gap the turn answered.

        Mirrors :meth:`_resolve_curiosity_seeds` but for
        ``knowledge_gap`` rows. Embeds the combined ``user_text +
        assistant_text`` once and cosines it against every open gap's
        stored embedding. Any gap scoring above
        ``agent.gap_user_answer_resolve_threshold`` (default 0.50) is
        marked resolved with ``metadata.resolved_by="user_answer"``.

        Why pair this with the idle :class:`IdleGapResolver`:
          * **This path** catches the answer the moment the user
            speaks it — the gap closes within one turn of being asked.
          * **The worker path** mops up gaps whose answer arrives via
            the post-summary ``MemoryExtractor`` (which writes a
            fresh ``preference`` / ``fact`` row hours later).

        No-op when the gap store is missing, when no gaps are open,
        or when the embedder isn't available — stays cheap on the
        cold path.
        """
        gap_store = getattr(self, "_knowledge_gap_store", None)
        embedder = getattr(self, "_embedder", None)
        if gap_store is None or embedder is None:
            return
        try:
            open_gaps = gap_store.list_open()
        except Exception:
            return
        active = [
            gap for gap in open_gaps
            if gap.embedding is not None and gap.embedding.size > 0
        ]
        if not active:
            return
        combined = " ".join(
            part for part in (user_text or "", assistant_text or "")
            if part and part.strip()
        ).strip()
        if not combined or len(combined) < 4:
            return
        try:
            turn_vec = embedder.embed(combined)
        except Exception:
            log.debug(
                "knowledge_gap resolve: embed failed", exc_info=True,
            )
            return
        if turn_vec is None or turn_vec.size == 0:
            return
        threshold = float(
            getattr(
                self._settings.agent,
                "gap_user_answer_resolve_threshold",
                0.50,
            )
        )
        for gap in active:
            try:
                sim = float((turn_vec * gap.embedding).sum())
            except Exception:
                continue
            if sim < threshold:
                continue
            try:
                ok = gap_store.mark_resolved(
                    int(gap.id),
                    answer_memory_id=None,
                    resolved_by="user_answer",
                    similarity=sim,
                )
            except Exception:
                log.debug(
                    "knowledge_gap mark_resolved failed (id=%s)",
                    gap.id,
                    exc_info=True,
                )
                continue
            if not ok:
                continue
            log.info(
                "knowledge_gap resolved: id=%s sim=%.2f topic=%r gap=%r",
                gap.id,
                sim,
                ((gap.metadata or {}).get("topic")
                 or "")[:40],
                (gap.content or "")[:80],
            )
            try:
                fresh = self._memory_store.get(int(gap.id))
            except Exception:
                fresh = None
            if (
                fresh is not None
                and self._notify_memory_updated is not None
            ):
                try:
                    self._notify_memory_updated(fresh.to_dict())
                except Exception:
                    log.debug(
                        "knowledge_gap notify_updated failed",
                        exc_info=True,
                    )

    # ── Schema v8 revival detection (E2) ────────────────────────────

    # Tiny stopword list scoped to the revival overlap check. We only
    # need to suppress the most common "free" matches so a memory and
    # an assistant reply don't pass the >=3-word threshold purely on
    # filler. Not a full NLP pipeline -- the threshold itself does the
    # heavy lifting.
    _REVIVAL_STOPWORDS: frozenset[str] = frozenset({
        "the", "a", "an", "and", "or", "but", "if", "then", "so", "of",
        "in", "on", "at", "to", "for", "with", "by", "as", "is", "are",
        "was", "were", "be", "been", "being", "do", "does", "did", "have",
        "has", "had", "you", "your", "i", "me", "my", "we", "our", "us",
        "he", "she", "they", "them", "this", "that", "these", "those",
        "it", "its", "from", "about", "into", "than", "what", "when",
        "where", "who", "how", "why", "not", "no", "yes", "ok", "okay",
        "just", "really", "very", "much", "like", "would", "could",
        "should", "will", "can", "may", "might", "also", "too", "any",
        "all", "some", "more", "most", "less", "such", "there", "here",
        "now", "again", "still", "even", "only", "yet",
    })

    @classmethod
    def _revival_tokens(cls, text: str) -> set[str]:
        """Lowercase content-word set used by the keyword overlap check.

        Tokens shorter than 4 chars and items in :attr:`_REVIVAL_STOPWORDS`
        are dropped -- short / common words light up too many incidental
        overlaps to be useful as a revival signal.
        """
        if not text:
            return set()
        import re

        raw = re.findall(r"[A-Za-z][A-Za-z0-9'_-]+", str(text).lower())
        out: set[str] = set()
        for token in raw:
            token = token.strip("'-_")
            if len(token) < 4:
                continue
            if token in cls._REVIVAL_STOPWORDS:
                continue
            out.add(token)
        return out

    def _mark_revived_memories(self, *, assistant_text: str) -> None:
        """Reward memories Aiko actually cited in her reply with revival.

        Reads the most recent surfaced-IDs snapshot from the RAG
        retriever, runs the keyword-overlap check between the reply
        text and each surfaced memory's content, and calls
        :meth:`MemoryStore.mark_revived` on the qualifying ids. Skipped
        entirely when tiers are disabled or no memories surfaced.
        """
        if not assistant_text or not self._memory_settings.tiers_enabled:
            return
        store = self._memory_store
        if store is None:
            return
        retriever = getattr(self, "_rag_retriever", None)
        if retriever is None:
            return
        ids = getattr(retriever, "last_surfaced_memory_ids", None)
        if not ids:
            return
        threshold = max(1, int(self._memory_settings.revival_min_word_overlap))
        reply_tokens = self._revival_tokens(assistant_text)
        if len(reply_tokens) < threshold:
            return
        delta = float(self._memory_settings.revival_per_hit)
        if delta <= 0:
            return
        revived: list[int] = []
        for mem_id in ids:
            mem = store.get(int(mem_id))
            if mem is None:
                continue
            mem_tokens = self._revival_tokens(mem.content)
            if len(reply_tokens & mem_tokens) >= threshold:
                revived.append(int(mem_id))
        if revived:
            try:
                store.mark_revived(revived, delta=delta)
                log.info(
                    "revival: bumped %d memory revival_scores (delta=%.2f)",
                    len(revived), delta,
                )
            except Exception:
                log.debug("mark_revived failed", exc_info=True)

    def _estimate_user_affect_for_contagion(
        self, user_text: str | None, tone: Any,
    ) -> tuple[float, float] | None:
        """K37: build the user's estimated ``(valence, arousal)`` for the
        contagion pass from cheap per-turn signals.

        Reuses the perceived mood / energy from the
        :class:`UserStateEstimator` (pure, no DB write needed here),
        regex dialogue-act sentiment, and the confident vocal tone.
        Returns ``None`` when nothing is readable so the contagion pass
        stays silent.
        """
        from app.core.affect.affect_state import estimate_user_affect

        mood: str | None = None
        energy: str | None = None
        estimator = getattr(self, "_user_state_estimator", None)
        if estimator is not None and user_text:
            try:
                now = estimator.estimate(self._user_id, user_text=user_text)
                mood = now.perceived_mood
                energy = now.perceived_energy
            except Exception:
                log.debug("contagion user-state estimate failed", exc_info=True)

        dialogue_act: str | None = None
        if user_text:
            try:
                from app.core.conversation.dialogue_act_tagger import tag_regex

                res = tag_regex(user_text)
                dialogue_act = res.act if res is not None else None
            except Exception:
                log.debug("contagion dialogue-act tag failed", exc_info=True)

        try:
            return estimate_user_affect(
                mood=mood,
                energy=energy,
                dialogue_act=dialogue_act,
                tone=tone,
            )
        except Exception:
            log.debug("contagion estimate_user_affect failed", exc_info=True)
            return None

    def _update_question_balance(self, assistant_text: str) -> None:
        """K47: roll the question-turn ring and arm/decay the suppress gate.

        Order matters: append the new flag, consume one suppressed turn
        for the turn that just completed, THEN re-arm from the fresh
        ratio. Re-arming while the ratio stays high keeps the gate up
        until Aiko's mix of questions vs. shares actually rebalances; a
        gentle tail of up-to ``suppress_turns`` lets it release.
        """
        from app.core.conversation.question_balance import (
            is_question_turn,
            should_suppress,
        )

        agent = self._settings.agent
        ring = getattr(self, "_question_turn_flags", None)
        if ring is None:
            return
        ring.append(is_question_turn(assistant_text))

        remaining = int(getattr(self, "_question_balance_suppress_remaining", 0))
        if remaining > 0:
            remaining -= 1

        threshold = float(
            getattr(agent, "question_balance_ratio_threshold", 0.55)
        )
        window = max(2, int(getattr(agent, "question_balance_window", 10)))
        min_samples = max(4, window // 2)
        suppress_turns = max(
            0, int(getattr(agent, "question_balance_suppress_turns", 2))
        )
        if suppress_turns > 0 and should_suppress(
            ring, threshold=threshold, min_samples=min_samples,
        ):
            remaining = suppress_turns

        self._question_balance_suppress_remaining = remaining

    def _update_tease_rhythm(
        self,
        *,
        user_text: str | None,
        assistant_text: str,
        reaction: str | None,
        assistant_message_id: int | None,
    ) -> None:
        """K48: evaluate the prior tease's landing, classify the current
        reply, and arm an ease-off / green-light cue.

        Order: (1) read the verdict on the most recent tease using this
        turn's ``user_text`` + that message's persisted K32 reactions;
        (2) classify the current reply and roll the ring + remember its
        id if it was a tease; (3) decide + arm a one-shot cue (cooldown-
        gated). The cue surfaces on the *next* turn's prompt.
        """
        from app.core.conversation.tease_rhythm import (
            classify_tease,
            decide_cue,
            landed_verdict,
            trailing_tease_streak,
        )

        agent = self._settings.agent
        ring = getattr(self, "_tease_flags", None)
        if ring is None:
            return

        # (1) Verdict on the previous tease.
        prev_id = getattr(self, "_last_tease_message_id", None)
        verdict: bool | None = None
        if prev_id is not None:
            laughed = False
            try:
                reactions = self._load_message_reactions(int(prev_id))
                laughed = int((reactions or {}).get("laugh", 0)) > 0
            except Exception:
                log.debug("tease-rhythm reaction read failed", exc_info=True)
            verdict = landed_verdict(laughed=laughed, user_reply=user_text)

        # (2) Classify the current reply; roll the ring; track its id.
        is_tease = classify_tease(assistant_text, reaction)
        ring.append(is_tease)
        self._last_tease_message_id = (
            int(assistant_message_id)
            if (is_tease and assistant_message_id is not None)
            else None
        )

        # (3) Decide + arm (cooldown-gated).
        cooldown = int(getattr(self, "_tease_cue_cooldown", 0))
        if cooldown > 0:
            cooldown -= 1

        humor = 0.0
        try:
            store = getattr(self, "_relationship_axes_store", None)
            if store is not None:
                humor = float(store.get(self._user_id).humor)
        except Exception:
            log.debug("tease-rhythm humor read failed", exc_info=True)

        cue = decide_cue(
            last_landed=verdict,
            tease_streak=trailing_tease_streak(ring),
            humor=humor,
            consecutive_cap=max(
                1, int(getattr(agent, "tease_rhythm_consecutive_cap", 3))
            ),
            green_light_humor=float(
                getattr(agent, "tease_rhythm_green_light_humor", 0.2)
            ),
        )
        if cue is not None and cooldown == 0:
            self._pending_tease_cue = cue
            cooldown = max(
                0, int(getattr(agent, "tease_rhythm_cooldown_turns", 3))
            )
            log.info(
                "tease-rhythm cue armed: cue=%s last_landed=%s streak=%d "
                "humor=%.3f",
                cue, verdict, trailing_tease_streak(ring), humor,
            )
        self._tease_cue_cooldown = cooldown

    def _maybe_track_conflict_repair(
        self,
        *,
        rupture_result: Any,
        current_valence: float,
        user_text: str,
        user_message_id: int | None,
        assistant_message_id: int | None,
    ) -> None:
        """J6: arm a repair watch on rupture; record on recovery.

        * A fresh rupture this turn (re)arms the watch with the dip floor
          + recovery target + a topic hint, and never records on the same
          turn (recovery hasn't happened yet).
        * Otherwise, if a watch is active and the user's valence has
          recovered, write the repair shared moment and clear the watch.
        * If the watch window runs out without recovery, drop it silently
          (an unresolved rupture is not a repair).
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "conflict_repair_enabled", True)):
            self._repair_watch = None
            return

        from app.core.relationship import conflict_repair as _cr

        if rupture_result is not None:
            topic = _cr.clean_topic(user_text)
            existing = getattr(self, "_repair_watch", None)
            if not topic and existing is not None:
                topic = existing.topic
            self._repair_watch = _cr.RepairWatch(
                recovery_target=float(rupture_result.prior_valence),
                dip_floor=float(rupture_result.current_valence),
                topic=topic,
                turns_left=int(getattr(agent, "conflict_repair_watch_turns", 5)),
            )
            return

        watch = getattr(self, "_repair_watch", None)
        if watch is None:
            return

        if _cr.has_recovered(
            current_valence,
            watch,
            epsilon=float(
                getattr(agent, "conflict_repair_recovery_epsilon", 0.05)
            ),
            min_rise=float(
                getattr(agent, "conflict_repair_min_recovery_rise", 0.10)
            ),
        ):
            self._record_conflict_repair(
                watch,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
            )
            self._repair_watch = None
            return

        watch.turns_left -= 1
        if watch.turns_left <= 0:
            self._repair_watch = None

    def _record_conflict_repair(
        self,
        watch: Any,
        *,
        user_message_id: int | None,
        assistant_message_id: int | None,
    ) -> None:
        """J6: persist the repair as a ``repair``-vibe shared moment."""
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        chat_db = getattr(self, "_chat_db", None)
        cooldown_h = float(
            getattr(self._settings.agent, "conflict_repair_cooldown_hours", 12.0)
        )
        if chat_db is not None and cooldown_h > 0:
            try:
                last = chat_db.kv_get(_KV_CONFLICT_REPAIR_AT)
            except Exception:
                last = None
            if last:
                try:
                    last_ts = datetime.fromisoformat(
                        str(last).replace("Z", "+00:00")
                    )
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    if (now - last_ts).total_seconds() < cooldown_h * 3600.0:
                        return
                except Exception:
                    pass

        from app.core.relationship import conflict_repair as _cr

        summary = _cr.build_repair_summary(self.user_display_name, watch.topic)
        ids = [
            i for i in (user_message_id, assistant_message_id) if i is not None
        ]
        try:
            row = store.add(
                summary=summary,
                vibe="repair",
                source="repair",
                confidence=0.7,
                salience=0.7,
                source_message_ids=ids or None,
                source_session=getattr(self, "session_key", None),
            )
        except Exception:
            log.debug("conflict-repair moment write failed", exc_info=True)
            return
        if row is None:
            return
        if chat_db is not None:
            try:
                chat_db.kv_set(_KV_CONFLICT_REPAIR_AT, now.isoformat())
            except Exception:
                log.debug("conflict-repair watermark write failed", exc_info=True)
        log.info(
            "J6 conflict-repair recorded: moment_id=%s topic=%r",
            row.id, watch.topic,
        )
        try:
            self._notify_shared_moment_added(row)
        except Exception:
            log.debug("conflict-repair notify failed", exc_info=True)


