"""Post-turn helper methods feeding ``_post_turn_inner_life``.

Split out of :mod:`app.core.session.post_turn_mixin` to keep both files
under the size budget. State ownership stays in SessionController.

NB: patch ``app.core.session.post_turn_helpers_mixin.<symbol>`` for any
symbol looked up by these methods.
"""
from __future__ import annotations

import logging
from typing import Any
from app.core.infra import timephrase
from app.core.memory import echo_detector
from app.core.session.debug_overrides import DebugOverridesHostMixin


log = logging.getLogger("app.session")

# J6: kv_meta watermark so one extended rough patch doesn't spawn several
# repair moments in quick succession.
_KV_CONFLICT_REPAIR_AT = "conflict_repair.last_recorded_at"

# K80: kv_meta watermark so a genuinely funny stretch of conversation
# yields one blessed bit, not a run of them. Blessing everything is the
# fastest way to make the beat worthless.
_KV_INSIDE_JOKE_AT = "inside_joke_birth.last_recorded_at"


class PostTurnHelpersMixin(DebugOverridesHostMixin):
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
            now_iso = timephrase.utcnow().isoformat()
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

            from app.core.relationship import tease_ledger as _tl

            now = timephrase.utcnow()
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

    def _peak_emotion_intensity(self) -> float:
        """K68 helper: strongest live K57 emotion-episode intensity, decayed.

        Reads the standing episode store (this turn's freshly-queued
        triggers are drained later in the post-turn pipeline, so this
        reflects the carried emotional weight). Best-effort -> 0.0.
        """
        try:

            from app.core.affect import emotion_episodes as _ee

            if not bool(
                getattr(self._settings.agent, "emotion_episodes_enabled", True)
            ):
                return 0.0
            chat_db = getattr(self, "_chat_db", None)
            if chat_db is None:
                return 0.0
            now = timephrase.utcnow()
            state = _ee.apply_decay(
                _ee.deserialize(chat_db.kv_get(_ee.KV_EMOTION_EPISODES)), now,
            )
            ep = _ee.strongest(state)
            return float(ep.intensity) if ep is not None else 0.0
        except Exception:
            return 0.0

    def _read_encoding_affect(self) -> tuple[float, float]:
        """K76 flashbulb hook: live ``(arousal, episode_intensity)``.

        Called by ``MemoryStore.add`` at memory-write time (on whatever
        thread is writing) to boost a new row's salience by its emotional
        charge. Both reads are best-effort — a neutral fallback
        ``(0.4, 0.0)`` yields zero charge / zero boost (legacy behaviour).
        """
        arousal = 0.4
        try:
            arousal = float(self._affect_store.get(self._user_id).arousal)
        except Exception:
            arousal = 0.4
        return arousal, self._peak_emotion_intensity()

    def _read_full_affect(self) -> tuple[float, float]:
        """L13 hook: live Aiko ``(valence, arousal)``.

        Wired as the ``MemoryStore`` affect provider so ``self`` /
        ``reflection`` / ``diary`` writes stamp ``metadata.affect`` with the
        tone of the moment they were written (the self-narrative half of the
        aiko affective pass). Best-effort neutral fallback ``(0.0, 0.4)``.
        """
        try:
            st = self._affect_store.get(self._user_id)
            return float(st.valence), float(st.arousal)
        except Exception:
            return 0.0, 0.4

    def _sample_cluster_affect(
        self,
        *,
        user_text: str,
        user_affect: "tuple[float, float] | None",
        state: Any,
    ) -> None:
        """L13 per-cluster affect sampler (post-turn, cheap).

        Resolves the live turn's topic cluster and folds the affect signal
        into that cluster's rolling EWMA, once per subject:

        * **user** map — the K37 ``user_affect`` estimate (skipped when the
          turn carried no readable user-affect signal, i.e. ``None``).
        * **aiko** map — Aiko's post-turn ``AffectState`` ``(valence,
          arousal)`` (always available), so "topics that move her" accrue.

        Keyed by ``cluster_id`` (the cheap hot-path key K75 uses); the L2
        ``_run_affect_pass`` joins ``cluster_id -> representative_id`` at
        synthesis time. Fully best-effort and gated by the sampler flag.
        """
        agent = getattr(self._settings, "agent", None)
        if not bool(getattr(agent, "affect_sampler_enabled", True)):
            return
        graph = getattr(self, "_topic_graph", None)
        embedder = getattr(self, "_embedder", None)
        chat_db = getattr(self, "_chat_db", None)
        if (
            graph is None
            or embedder is None
            or chat_db is None
            or not bool(getattr(graph, "persistent", False))
        ):
            return
        text = (user_text or "").strip()
        if len(text) < 8:
            return


        from app.core.concepts import cluster_affect as _ca

        mem = getattr(self, "_memory_settings", None)
        min_sim = float(getattr(mem, "affect_sampler_min_sim", 0.4))
        top_n = int(getattr(mem, "affect_sampler_top_n", 1))
        lr = float(getattr(mem, "affect_sampler_learning_rate", 0.2))
        cap = int(getattr(mem, "cluster_affect_map_cap", 200))
        max_age = float(getattr(mem, "cluster_affect_max_age_days", 120.0))

        qvec = embedder.embed(text)
        matches = graph.best_clusters_for(qvec, top_n=top_n, min_sim=min_sim)
        if not matches:
            return
        now_iso = timephrase.utcnow().isoformat(timespec="seconds")

        # Aiko map — always (her scalar is always defined).
        try:
            a_val = float(getattr(state, "valence", 0.0))
            a_ar = float(getattr(state, "arousal", 0.4))
            key = _ca.KV_CLUSTER_AFFECT_AIKO
            amap = _ca.load_map(chat_db.kv_get, key)
            for cid, _label, _sim in matches:
                ck = str(int(cid))
                amap[ck] = _ca.update_state(
                    amap.get(ck), a_val, a_ar,
                    learning_rate=lr, now_iso=now_iso,
                )
            _ca.save_map(
                chat_db.kv_set, key, amap, cap=cap, max_age_days=max_age
            )
        except Exception:
            log.debug("cluster-affect aiko update failed", exc_info=True)

        # User map — only when the turn carried a readable user-affect signal.
        if user_affect is None:
            return
        try:
            u_val, u_ar = float(user_affect[0]), float(user_affect[1])
            key = _ca.KV_CLUSTER_AFFECT_USER
            umap = _ca.load_map(chat_db.kv_get, key)
            for cid, _label, _sim in matches:
                ck = str(int(cid))
                umap[ck] = _ca.update_state(
                    umap.get(ck), u_val, u_ar,
                    learning_rate=lr, now_iso=now_iso,
                )
            _ca.save_map(
                chat_db.kv_set, key, umap, cap=cap, max_age_days=max_age
            )
        except Exception:
            log.debug("cluster-affect user update failed", exc_info=True)

    def _apply_vitality_turn(self, raw_assistant_text: str) -> None:
        """K68: apply this turn's energy spend + interest boost, then broadcast.

        * **Spend** — long reply (effort) + standing K57 emotion intensity
          (an emotionally heavy stretch drains her).
        * **Boost (the liven-up)** — K14 ``engaged`` + her own
          ``AffectState.arousal`` + a K6 ``strong_novelty`` / ``mild_shift``
          topic. A sleepy Aiko can wake up over a genuinely engaging chat.

        Reads the kv state, applies the tiny wall-clock recovery toward
        the circadian baseline (≈0 inside a live turn), then the net
        delta, persists, and broadcasts the new energy so the avatar
        embodiment (gesture/breath amplitude) updates. Best-effort.
        """

        from app.core.affect import vitality as _vit
        from app.core.affect import vitality_rhythm as _vr

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return
        mem = self._memory_settings
        now = timephrase.now()
        baseline, _rhythm = _vr.current_baseline(
            chat_db,
            now,
            enabled=bool(
                getattr(self._settings.agent, "vitality_rhythm_enabled", True)
            ),
            exception_chance=float(
                getattr(mem, "vitality_rhythm_exception_chance", 0.3)
            ),
        )

        try:
            raw = chat_db.kv_get(_vit.KV_VITALITY)
        except Exception:
            raw = None
        state = _vit.deserialize(raw, baseline=baseline, now=now)
        state = _vit.step_recover(
            state, baseline, now,
            half_life_hours=float(
                getattr(mem, "vitality_recover_half_life_hours", 2.0)
            ),
        )

        cost = _vit.compute_turn_cost(
            reply_chars=len(raw_assistant_text or ""),
            emotion_intensity=self._peak_emotion_intensity(),
            chars_per_unit=float(
                getattr(mem, "vitality_cost_chars_per_unit", 1200.0)
            ),
            length_cost_unit=float(
                getattr(mem, "vitality_cost_length_unit", 0.04)
            ),
            emotion_cost_gain=float(
                getattr(mem, "vitality_cost_emotion_gain", 0.06)
            ),
            max_cost=float(getattr(mem, "vitality_cost_max", 0.12)),
        )

        arousal: float | None = None
        try:
            arousal = float(self._affect_store.get(self._user_id).arousal)
        except Exception:
            arousal = None
        novelty_band = getattr(
            getattr(self, "_novelty_detector", None), "last_band", None,
        )
        boost = _vit.compute_interest_boost(
            engagement_label=getattr(self, "_last_engagement_label", None),
            arousal=arousal,
            novelty_band=novelty_band,
            engaged_boost=float(getattr(mem, "vitality_boost_engaged", 0.05)),
            arousal_threshold=float(
                getattr(mem, "vitality_boost_arousal_threshold", 0.55)
            ),
            arousal_gain=float(
                getattr(mem, "vitality_boost_arousal_gain", 0.22)
            ),
            strong_novelty_boost=float(
                getattr(mem, "vitality_boost_strong_novelty", 0.04)
            ),
            mild_novelty_boost=float(
                getattr(mem, "vitality_boost_mild_novelty", 0.02)
            ),
            max_boost=float(getattr(mem, "vitality_boost_max", 0.15)),
        )

        new_energy = _vit.apply_turn(state.energy, cost=cost, boost=boost)
        new_state = _vit.VitalityState(
            energy=new_energy, last_update_at=now.isoformat(),
        )
        try:
            chat_db.kv_set(_vit.KV_VITALITY, _vit.serialize(new_state))
        except Exception:
            log.debug("vitality kv_set failed", exc_info=True)

        if cost > 0 or boost > 0 or abs(new_energy - state.energy) > 1e-9:
            log.info(
                "vitality turn: energy=%.3f -> %.3f cost=%.3f boost=%.3f "
                "baseline=%.3f label=%s arousal=%s novelty=%s",
                float(state.energy), float(new_energy), cost, boost,
                baseline,
                getattr(self, "_last_engagement_label", None),
                (f"{arousal:.2f}" if arousal is not None else "-"),
                novelty_band or "-",
            )

        notify = getattr(self, "_notify_vitality", None)
        if notify is not None:
            try:
                notify(new_energy)
            except Exception:
                log.debug("vitality notify raised", exc_info=True)

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

        now = timephrase.utcnow()
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

    def _combined_turn_vec(
        self, *, user_text: str, assistant_text: str,
    ) -> Any:
        """The embedding of ``user_text + assistant_text``, computed once.

        Three post-turn paths want this exact vector -- seed resolve, gap
        resolve, and the cue pool's turn-scoped matching -- and each used
        to ask the embedder for it separately. The embedder's LRU made
        that cheap rather than free; this makes it explicit, so adding a
        fourth consumer does not quietly add a fourth round-trip on a
        cache eviction.

        Memoised against the text pair rather than a turn counter,
        because post-turn has no turn id in scope and the pair is what
        identifies the vector anyway.
        """
        combined = " ".join(
            part for part in (user_text or "", assistant_text or "")
            if part and part.strip()
        ).strip()
        if not combined or len(combined) < 4:
            return None
        cached = getattr(self, "_turn_vec_cache", None)
        if cached is not None and cached[0] == combined:
            return cached[1]
        embedder = getattr(self, "_embedder", None)
        if embedder is None:
            return None
        try:
            vec = embedder.embed(combined)
        except Exception:
            log.debug("turn embed failed", exc_info=True)
            return None
        if vec is None or getattr(vec, "size", 0) == 0:
            return None
        self._turn_vec_cache = (combined, vec)
        return vec

    # ── F2.1: post-turn user-answer gap resolver ─────────────────────

    def _resolve_knowledge_gaps(  # noqa: C901
        self,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """F2.1: stamp ``resolved_at`` on any open gap the turn answered.

        Cosines the combined ``user_text + assistant_text`` vector
        (:meth:`_combined_turn_vec`, shared with the cue pool) against
        every open gap's stored embedding. Any gap scoring above
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
        turn_vec = self._combined_turn_vec(
            user_text=user_text, assistant_text=assistant_text,
        )
        if turn_vec is None:
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

    # ── Revival detection (schema v8 / E2, semantic half from F12) ───
    #
    # The tokeniser, stopword list and overlap test used to live here as
    # private helpers, and the L37 ledger grew a second copy of the same
    # logic. Both now defer to
    # :mod:`app.core.memory.echo_detector`, so "did the reply use this?"
    # has exactly one answer and the memory layer and the ledger cannot
    # drift apart on it.

    def _mark_revived_memories(self, *, assistant_text: str) -> None:
        """Reward memories Aiko actually used in her reply with revival.

        Reads the most recent surfaced-IDs snapshot from the RAG
        retriever, asks
        :func:`app.core.memory.echo_detector.detect` whether the reply
        used each surfaced memory, and calls
        :meth:`MemoryStore.mark_revived` on the qualifying ids. Skipped
        entirely when tiers are disabled or no memories surfaced.

        F12 split the bump in two, because the two kinds of evidence are
        not equally strong. A **lexical** hit means Aiko quoted or closely
        restated the memory and earns the full historical
        ``revival_per_hit``. A **semantic** hit means the reply was merely
        close in embedding space, which is weaker than it looks: the
        memory was surfaced *because* it was topically near this turn, so
        some of that similarity is the retrieval showing through rather
        than Aiko using anything. It earns the smaller
        ``semantic_revival_per_hit``, which sits below
        ``scratchpad_ttl_min_revival`` so a single on-topic coincidence
        cannot exempt a memory from cleanup.

        Two write batches rather than one, since ``mark_revived`` applies a
        single delta to a whole list.
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
        ms = self._memory_settings
        threshold = max(1, int(ms.revival_min_word_overlap))
        reply_tokens = echo_detector.tokens(assistant_text)
        delta = float(ms.revival_per_hit)
        semantic_delta = float(getattr(ms, "semantic_revival_per_hit", 0.0))
        min_cosine = self._semantic_echo_floor()
        if delta <= 0 and semantic_delta <= 0:
            return
        # The lexical test needs at least ``threshold`` content words to
        # have any chance; the semantic one does not care, so a curt reply
        # is no longer a blanket early return.
        if len(reply_tokens) < threshold and min_cosine is None:
            return
        reply_vec = getattr(self, "_last_assistant_vec", None)

        lexical: list[int] = []
        semantic: list[int] = []
        for mem_id in ids:
            mem = store.get(int(mem_id))
            if mem is None:
                continue
            verdict = echo_detector.detect(
                reply_tokens=reply_tokens,
                item_text=mem.content,
                min_overlap=threshold,
                reply_vec=reply_vec,
                item_vec=getattr(mem, "embedding", None),
                min_cosine=min_cosine,
            )
            if verdict.is_lexical:
                lexical.append(int(mem_id))
            elif verdict.echoed:
                semantic.append(int(mem_id))

        for batch, batch_delta, label in (
            (lexical, delta, "lexical"),
            (semantic, semantic_delta, "semantic"),
        ):
            if not batch or batch_delta <= 0:
                continue
            try:
                store.mark_revived(batch, delta=batch_delta)
                log.info(
                    "revival: bumped %d memory revival_scores "
                    "(kind=%s delta=%.2f)",
                    len(batch), label, batch_delta,
                )
            except Exception:
                log.debug("mark_revived failed", exc_info=True)

    def _semantic_echo_floor(self) -> float | None:
        """The cosine floor for a semantic echo, or ``None`` when off.

        ``None`` is what switches the semantic half off in
        :func:`echo_detector.detect` -- the caller enables the fallback by
        supplying a floor rather than by passing a flag, so there is no
        way to ask for semantic matching without saying how strict.
        """
        ms = self._memory_settings
        if not bool(getattr(ms, "semantic_revival_enabled", False)):
            return None
        floor = float(getattr(ms, "semantic_revival_min_cosine", 0.0))
        return floor if floor > 0.0 else None

    # ── L37: surfacing outcome ledger ────────────────────────────────

    def _surfacing_echo_marks(
        self, items: list, assistant_text: str,
    ) -> dict[tuple[str, int], echo_detector.EchoVerdict]:
        """Which surfaced items did Aiko's own reply actually reference?

        The same question :meth:`_mark_revived_memories` acts on, recorded
        per item instead, through the same
        :func:`app.core.memory.echo_detector.detect` so the ledger cannot
        disagree with the memory layer about what an echo is.

        Two per-kind differences, both deliberate:

        - **Threshold.** ``revival_min_word_overlap`` was tuned against
          multi-sentence memory content; applying it to a three-to-six
          word concept label would make the same nominal bar a far harsher
          test, so concepts get their own floor.
        - **Semantic fallback.** Only memories get one. Memory embeddings
          are on the store's in-process mirror, and a memory is a sentence
          or two, which is a fair comparison against a reply. A concept
          label is a handful of words whose embedding sits in a different
          part of the space than prose, so a cosine between the two would
          not mean what the memory cosine means.

        An item is omitted from the result -- leaving the columns NULL, and
        reading as *not computed* -- whenever its text could not be
        loaded, which must stay distinct from a computed "not echoed".
        Clusters are always omitted: their labels aren't reachable from
        here, and guessing would be worse than a NULL.
        """
        marks: dict[tuple[str, int], echo_detector.EchoVerdict] = {}
        if not assistant_text or not items:
            return marks
        # An empty content-word set here is a real observation (a curt
        # reply echoed nothing), not a failure to look, so it still records
        # a verdict for every item whose own text resolves.
        reply_tokens = echo_detector.tokens(assistant_text)
        ms = self._memory_settings
        mem_floor = max(1, int(getattr(ms, "revival_min_word_overlap", 3)))
        concept_floor = max(1, int(getattr(
            self._settings.agent, "surfacing_echo_min_overlap_concept", 1,
        )))
        min_cosine = self._semantic_echo_floor()
        reply_vec = getattr(self, "_last_assistant_vec", None)
        mem_store = getattr(self, "_memory_store", None)
        concept_store = getattr(self, "_concept_store", None)
        for item in items:
            kind = str(getattr(item, "item_kind", "") or "")
            item_id = int(getattr(item, "item_id", 0) or 0)
            if item_id <= 0:
                continue
            text = ""
            floor = mem_floor
            item_vec = None
            item_cosine_floor = None
            if kind == "memory" and mem_store is not None:
                try:
                    mem = mem_store.get(item_id)
                except Exception:
                    mem = None
                text = str(getattr(mem, "content", "") or "")
                item_vec = getattr(mem, "embedding", None)
                item_cosine_floor = min_cosine
            elif kind == "concept" and concept_store is not None:
                try:
                    concept = concept_store.get(item_id)
                except Exception:
                    concept = None
                text = str(getattr(concept, "label", "") or "")
                floor = concept_floor
            if not text:
                continue
            marks[(kind, item_id)] = echo_detector.detect(
                reply_tokens=reply_tokens,
                item_text=text,
                min_overlap=floor,
                reply_vec=reply_vec,
                item_vec=item_vec,
                min_cosine=item_cosine_floor,
            )
        return marks

    def _surfacing_carry_is_current(
        self, prev_id: int, user_message_id: int | None,
    ) -> bool:
        """Does the carried key really name the reply the label describes?

        ``_post_turn_inner_life`` can return early -- on empty user text,
        or if the affect updater raises -- which skips this hook without
        clearing the carry. The next turn to reach here would then settle
        a two-turns-old reply with a reaction to the one in between.

        K14 measures latency from the last assistant message before the
        current user message, so that reply *is* the subject of the label.
        If another assistant message sits between the carry and this
        user message, the carry is stale and settling it would attribute
        the reaction to the wrong reply -- worse than not settling, which
        just reads as "no evidence".

        Unverifiable (no ``user_message_id``, no database) counts as
        current: the carry is right on every path that isn't the rare
        skip, and refusing to settle by default would starve the ledger.
        """
        if user_message_id is None or prev_id <= 0:
            return True
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return True
        try:
            return not chat_db.has_assistant_message_between(
                self.session_key, prev_id, int(user_message_id),
            )
        except Exception:
            log.debug("surfacing carry check failed", exc_info=True)
            return True

    def _record_surfacing_outcomes(
        self,
        *,
        assistant_text: str,
        assistant_message_id: int | None,
        engagement_label: str | None,
        user_message_id: int | None = None,
    ) -> None:
        """Close the surfacing loop: settle turn N-1, record turn N.

        The two halves run on different clocks, which is the whole point
        of doing them together here. ``engagement_label`` was just derived
        from how long the user took to reply and how much they wrote, so
        it describes their reaction to the **previous** reply -- it settles
        the rows keyed to ``_prev_surfacing_message_id``, never this
        turn's. Whether Aiko echoed an item, by contrast, is knowable
        immediately and is stamped at insert time.

        The stash is consumed unconditionally so a turn that surfaced
        nothing cannot be credited with the previous turn's set, and
        ``engagement_label`` is passed in rather than read off the session
        so a turn whose engagement pass failed leaves the old rows
        unsettled instead of settling them with a stale label.
        """
        store = getattr(self, "_surfacing_outcome_store", None)
        items = list(getattr(self, "_last_surfaced_items", None) or [])
        self._last_surfaced_items = []
        if store is None:
            return

        prev_id = int(getattr(self, "_prev_surfacing_message_id", 0) or 0)
        if prev_id > 0 and engagement_label:
            if self._surfacing_carry_is_current(prev_id, user_message_id):
                settled = store.settle(prev_id, str(engagement_label))
                if settled:
                    log.info(
                        "surfacing ledger: settled %d row(s) for msg=%d as %s",
                        settled, prev_id, engagement_label,
                    )
            else:
                log.info(
                    "surfacing ledger: msg=%d left unsettled, a turn was "
                    "skipped and %s describes a later reply",
                    prev_id, engagement_label,
                )

        message_id = int(assistant_message_id or 0)
        if message_id <= 0 or not items:
            # Nothing keyed to this reply, so nothing for the next turn's
            # engagement to attribute. Dropping the carry here is what
            # stops a later turn settling a much older reply's rows.
            self._prev_surfacing_message_id = 0
            return
        echoes = {}
        try:
            echoes = self._surfacing_echo_marks(items, assistant_text)
        except Exception:
            log.debug("surfacing echo marks failed", exc_info=True)
        written = store.add_many(message_id, items, echoes=echoes)
        self._prev_surfacing_message_id = message_id if written else 0
        if written:
            log.info(
                "surfacing ledger: recorded %d item(s) for msg=%d "
                "(echoed=%d/%d semantic=%d)",
                written, message_id,
                sum(1 for v in echoes.values() if v.echoed), len(echoes),
                sum(1 for v in echoes.values()
                    if v.kind == echo_detector.ECHO_SEMANTIC),
            )

    def _snapshot_armed_cues(self) -> None:
        """G4: stash which cues have material waiting, pre-assembly.

        Called from the top of the turn rather than during assembly because
        the T6 providers consume the state this reads -- see
        :func:`app.core.proactive.cue_accounting.armed_cues`.
        """
        self._cue_armed_snapshot = set()
        self._cue_question_balance_snapshot = False
        self._last_cue_decisions = None
        if getattr(self, "_cue_decision_store", None) is None:
            return
        try:
            from app.core.proactive.cue_accounting import armed_cues

            self._cue_armed_snapshot = armed_cues(self)
        except Exception:
            log.debug("cue arming snapshot failed", exc_info=True)
            self._cue_armed_snapshot = set()
        # K47's countdown has to be read here too, not at attribution
        # time: ``_update_question_balance`` decrements it during post-turn
        # and runs BEFORE the cue recorder, so a turn suppressed with one
        # turn remaining would look unsuppressed by then and its declines
        # would be misattributed to the providers. Providers only read the
        # countdown during assembly, so this snapshot stays valid across it.
        try:
            self._cue_question_balance_snapshot = bool(
                self._question_balance_suppressed()
            )
        except Exception:
            log.debug("question-balance snapshot failed", exc_info=True)

    def _record_cue_decisions(
        self,
        *,
        assistant_message_id: int | None,
        telemetry: Any = None,
    ) -> None:
        """G4: record what happened to every cue that was armed this turn.

        Reads ``telemetry.block_chars`` for "did it render", which the
        assembler has been computing on every turn since P31a -- so no cue
        provider needs instrumenting to answer it. ``telemetry`` is passed
        in rather than read from ``get_last_system_prompt()`` because that
        snapshot is stamped AFTER post-turn runs, and would hand us the
        previous turn's block sizes: the reach ratio would be silently
        computed against the wrong assembly.

        Surfaced cues are additionally recorded in the L37 ledger, so a cue
        that does get through gets the same engagement settle as a concept
        or memory and the question "which cues actually land?" becomes a
        leaderboard read.
        """
        store = getattr(self, "_cue_decision_store", None)
        armed = set(getattr(self, "_cue_armed_snapshot", None) or set())
        suppressed = bool(
            getattr(self, "_cue_question_balance_snapshot", False)
        )
        self._cue_armed_snapshot = set()
        self._cue_question_balance_snapshot = False
        if store is None:
            return

        message_id = int(assistant_message_id or 0)
        if message_id <= 0:
            return
        block_chars = dict(getattr(telemetry, "block_chars", None) or {})
        if not block_chars:
            # No assembly recorded (banter / aborted turns take a path that
            # builds no prompt). Recording declines here would blame the
            # cues for a prompt that was never assembled.
            return

        try:
            from app.core.proactive.cue_accounting import (
                decisions_from_block_chars,
            )

            decisions = decisions_from_block_chars(
                armed,
                block_chars,
                question_balance_suppressed=suppressed,
            )
        except Exception:
            log.debug("cue decision derivation failed", exc_info=True)
            return

        self._last_cue_decisions = decisions
        self._queue_surfaced_cues_for_ledger(decisions)
        rows = decisions.rows()
        if not rows:
            return
        written = store.add_many(message_id, rows)
        if written:
            surfaced = sorted(decisions.surfaced)
            log.info(
                "cue accounting: msg=%d armed=%d surfaced=%s declined=%s",
                message_id, len(decisions.armed),
                ",".join(surfaced) or "-",
                ",".join(
                    f"{cue}={reason}"
                    for cue, reason in sorted(decisions.declined.items())
                ) or "-",
            )
    def _queue_surfaced_cues_for_ledger(self, decisions: Any) -> None:
        """Add surfaced cues to the L37 carry so they settle like any item.

        Appended to ``_last_surfaced_items`` rather than written directly,
        so cues go through the *same* insert as concepts and memories. A
        second ``add_many`` would have looked simpler and been wrong: the
        ledger drops its carry pointer when a turn surfaces nothing, so on a
        turn that produced a cue but no concepts the cue row would never
        have been settled -- unsettled forever, in the one column the
        feature exists to fill.

        Cues are name-keyed (``item_key``); there is no integer cue registry
        anywhere in the codebase. No echo verdict is attached: a cue is an
        instruction to Aiko ("you can ask how it went"), not a remembered
        item she might quote, so echo has no meaning and NULL reads
        correctly as "not applicable".
        """
        if getattr(self, "_surfacing_outcome_store", None) is None:
            return
        if not decisions.surfaced:
            return
        try:
            from app.core.memory.surfacing_outcome_store import (
                ITEM_KIND_CUE,
                SurfacedItem,
            )

            carry = list(getattr(self, "_last_surfaced_items", None) or [])
            carry.extend(
                SurfacedItem(
                    item_kind=ITEM_KIND_CUE,
                    item_id=0,
                    item_key=str(cue),
                    lane="cue",
                    surface_reason="cue",
                )
                for cue in sorted(decisions.surfaced)
            )
            self._last_surfaced_items = carry
        except Exception:
            log.debug("cue ledger queue failed", exc_info=True)

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

        remaining = int(
            self._debug_overrides.peek("question_balance_suppress_remaining", 0)
        )
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

        # Drop the entry at zero rather than re-arming with 0. The guard reads
        # `peek(..., 0) > 0`, so absent and zero mean the same thing to it --
        # but leaving a 0 behind would put a permanent entry in
        # `list_debug_overrides`, whose whole job is answering what is pending.
        if remaining > 0:
            self._debug_overrides.arm(
                "question_balance_suppress_remaining", remaining,
            )
        else:
            self._debug_overrides.disarm("question_balance_suppress_remaining")

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

        now = timephrase.utcnow()
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

    # ── K80: inside-joke birth ────────────────────────────────────────

    def _maybe_bless_inside_joke(
        self, *, user_text: str, user_message_id: int | None,
    ) -> None:
        """K80: notice the turn a throwaway line becomes a running bit.

        The user handing one of Aiko's own phrases back to her, laughing,
        is the moment a bit is born. The slow
        :class:`~app.core.memory.catchphrase_miner.CatchphraseMiner` only
        sees a phrase once it has *recurred* across a window; this catches
        the live beat, when noticing it out loud still means something.

        On a hit: arms the one-shot cue the next turn's provider renders,
        and persists the phrase (catchphrase + playful shared moment) so
        K22 and the running-jokes block carry it forward. Rate-limited by
        a wall-clock watermark -- rarity is the whole point.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "inside_joke_birth_enabled", True)):
            return
        origins = list(getattr(self, "_recent_assistant_turns", None) or ())
        if not origins:
            return

        from datetime import datetime, timezone

        now = timephrase.utcnow()
        chat_db = getattr(self, "_chat_db", None)
        cooldown_h = float(
            getattr(agent, "inside_joke_birth_cooldown_hours", 24.0)
        )
        if chat_db is not None and cooldown_h > 0:
            try:
                last = chat_db.kv_get(_KV_INSIDE_JOKE_AT)
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

        from app.core.memory import catchphrase_miner as _cm

        laughed_ids: set[int] = set()
        for mid, _text in origins:
            if mid is None:
                continue
            try:
                reactions = self._load_message_reactions(int(mid))
            except Exception:
                continue
            if int((reactions or {}).get("laugh", 0)) > 0:
                laughed_ids.add(int(mid))

        birth = _cm.detect_inside_joke_birth(
            user_text=user_text,
            origins=origins,
            laughed_ids=laughed_ids,
            known_phrases=self._known_catchphrases(),
            min_n=max(2, int(getattr(agent, "inside_joke_birth_min_words", 3))),
        )
        if birth is None:
            return

        self._pending_inside_joke = birth
        written = _cm.bless_inside_joke(
            birth,
            memory_store=getattr(self, "_memory_store", None),
            embedder=getattr(self, "_embedder", None),
            moments_store=getattr(self, "_shared_moments_store", None),
            session_key=getattr(self, "session_key", None),
            source_message_id=user_message_id,
        )
        if chat_db is not None:
            try:
                chat_db.kv_set(_KV_INSIDE_JOKE_AT, now.isoformat())
            except Exception:
                log.debug("inside-joke watermark write failed", exc_info=True)
        log.info(
            "K80 inside-joke born: phrase=%r lag=%d laughed=%s "
            "catchphrase_id=%s moment_id=%s",
            birth.phrase, birth.lag_turns, birth.laughed,
            written.get("catchphrase_id"), written.get("moment_id"),
        )

    def _known_catchphrases(self) -> list[str]:
        """Phrases already in the running-jokes registry (lowercased).

        This is K80's duplicate guard, so a missed row means re-blessing a
        bit that is already "theirs" — the least magical thing the feature
        could do. Before the ``kind`` filter it read the top 64 rows of
        *any* kind, so on a mature corpus it could easily return nothing
        while dozens of catchphrases existed.
        """
        store = getattr(self, "_memory_store", None)
        if store is None:
            return []
        try:
            top = store.iter_by_kind("catchphrase")
        except Exception:
            return []
        return [
            (m.content or "").strip().lower()
            for m in top
            if m.content
        ]


