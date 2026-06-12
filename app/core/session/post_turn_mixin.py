"""Post-turn inner-life mixin.

Extracted from :mod:`app.core.session.session_controller` to keep the
controller shell readable. Covers the cold-path work that runs
*after* every turn — schema-v8 memory revival, K9 curiosity-seed
resolution, and the big ``_post_turn_inner_life`` orchestrator that
fans out into all the other inner-life subsystems (mood updates,
relationship tracking, knowledge-gap mining, narrative weaver
nudges, etc.).

State ownership stays in ``SessionController.__init__``; this mixin
just reads ``self.*`` and drives ``self._scheduler.submit`` /
``self._memory_store`` writes. The methods only run after the turn
text is committed to the chat DB and surfaced to the user, so they
have no init-order risk.

NB: tests that previously patched
``app.core.session.session_controller.<symbol>`` for any of the moved methods
must patch ``app.core.session.post_turn_mixin.<symbol>`` instead. The
patch must target the module where the symbol is *looked up*.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any


log = logging.getLogger("app.session")


class PostTurnMixin:
    """``_resolve_curiosity_seeds``, revival detection, ``_post_turn_inner_life``."""

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

    def _post_turn_inner_life(
        self,
        *,
        user_text: str,
        reaction: str,
        assistant_text: str = "",
        raw_assistant_text: str = "",
        user_message_id: int | None = None,
        assistant_message_id: int | None = None,
    ) -> None:
        """Run all post-turn inner-life updates (cheap, no LLM).

        Currently:
          - AffectUpdater.apply_turn (POST-TURN)
          - mood_state WS broadcast
          - ReflectionWorker scheduling (Phase 2c) — submitted to the
            speaking window so the LLM call hides under TTS playback.

        More post-turn jobs (user-state estimator, promise regex, agenda
        regex) will hang off this method as the relevant phases land.
        """
        try:
            affect_before = self._affect_store.get(self._user_id)
        except Exception:
            log.debug("affect snapshot failed", exc_info=True)
            affect_before = None
        try:
            with self._vocal_tone_lock:
                tone = self._last_vocal_tone
            state = self._affect_updater.apply_turn(
                self._user_id,
                reaction=reaction,
                user_text=user_text,
                user_tone=tone,
            )
        except Exception:
            log.debug("affect updater failed", exc_info=True)
            return

        # K30 — feed the self-noticing flat-affect ring with one
        # ``(valence, arousal, reaction)`` triple per turn. ``state``
        # is the POST-turn scalar (matches what the user actually
        # walked away with). ``reaction`` is the assistant tag from
        # the just-finished turn (None when no ``[[reaction:...]]``
        # fired) and is what the detector uses to decide whether
        # Aiko "landed somewhere" this turn. Cheap append, no
        # detector call -- the detector runs at provider time.
        try:
            ring = getattr(self, "_self_noticing_affect_samples", None)
            if ring is not None:
                ring.append(
                    (
                        float(state.valence),
                        float(state.arousal),
                        (reaction or None) if reaction else None,
                    )
                )
        except Exception:
            log.debug("self-noticing affect-ring append failed", exc_info=True)

        # K8 — affect rupture-and-repair detection. The cheapest
        # possible cue: subtract two valence scalars and reaction-
        # filter. Runs immediately after the AffectUpdater so we
        # have both ``affect_before`` (pre-turn) and ``state``
        # (post-turn) in scope. One-shot slot on the controller is
        # consumed by the next turn's inner-life provider.
        if (
            affect_before is not None
            and bool(
                getattr(self._settings.agent, "rupture_repair_enabled", True)
            )
        ):
            try:
                from app.core.affect import affect_rupture_detector

                threshold = float(
                    getattr(
                        self._settings.agent,
                        "rupture_valence_drop_threshold",
                        0.12,
                    )
                )
                rupture_result = affect_rupture_detector.detect(
                    prior_valence=affect_before.valence,
                    current_valence=state.valence,
                    prior_reaction=reaction,
                    threshold=threshold,
                )
                if rupture_result is not None:
                    self._pending_rupture = rupture_result
                    log.info(
                        "K8 rupture: drop=%.3f prior_reaction=%r "
                        "(prior=%.3f -> current=%.3f)",
                        rupture_result.valence_drop,
                        rupture_result.prior_reaction,
                        rupture_result.prior_valence,
                        rupture_result.current_valence,
                    )
            except Exception:
                log.debug("rupture detector raised", exc_info=True)

        # K45 — mood inertia. Compare the fresh reaction tag's implied
        # affect target against the PRE-impulse smoothed state
        # (``affect_before``): the avatar already jumped to the tag,
        # but if the felt state is still far away, arm a one-shot cue
        # so the *words* carry the residue. Ring + cooldown live on
        # the controller; the assessment itself is pure.
        try:
            self._maybe_arm_mood_inertia(
                reaction=reaction, affect_before=affect_before,
            )
        except Exception:
            log.debug("mood-inertia arming failed", exc_info=True)

        # K38 — self-correction. Catch when this reply contradicted one
        # of Aiko's own high-confidence fact/preference memories so she
        # can own the slip on her next turn. One-shot slot + cooldown.
        try:
            self._maybe_arm_self_correction(assistant_text)
        except Exception:
            log.debug("self-correction arming failed", exc_info=True)

        # K43 — promise fulfilment: when this reply lexically covers the
        # body of an open assistant-side promise, flip it to fulfilled so
        # the follow-through worker stops owing it (and the kept-promise
        # signal reaches the axes / moment detector above next turn).
        try:
            self._maybe_resolve_promises(assistant_text, source="reply")
        except Exception:
            log.debug("promise resolution hook failed", exc_info=True)

        self._notify_mood_state({
            "label": state.mood_label,
            "intensity": float(state.mood_intensity),
            "valence": float(state.valence),
            "arousal": float(state.arousal),
            "circadian_period": self.current_circadian_period(),
            "resolved_outfit": self.resolve_auto_outfit(),
        })

        # Schema v8: bump revival_score on memories Aiko actually cited.
        # The RAG retriever stashed the surfaced IDs after its mark_used
        # pass; we compare the assistant reply's keyword set against each
        # memory's content and reward overlap above the configured floor.
        try:
            self._mark_revived_memories(assistant_text=assistant_text)
        except Exception:
            log.debug("memory revival mark failed", exc_info=True)

        # K9: auto-resolve any curiosity seed the conversation just
        # touched. One embed call + N dot products (N <= max_active);
        # cheap enough to land on the post-turn hot path.
        try:
            self._resolve_curiosity_seeds(
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception:
            log.debug("curiosity seed auto-resolve failed", exc_info=True)

        # F2.1: auto-resolve any open knowledge_gap the user just
        # answered. Same shape as the seed resolver above; reuses the
        # combined user+assistant embedding budget (one embed per turn
        # total whether seeds and gaps both run or only one does).
        try:
            self._resolve_knowledge_gaps(
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception:
            log.debug(
                "knowledge_gap auto-resolve failed", exc_info=True,
            )

        # K22 — callback / inside-joke detector. Post-turn cosine pass
        # between Aiko's reply and older eligible memories; hits stamp
        # ``metadata.callback_count`` + bump salience/revival_score so
        # the retriever's read-side bonus prefers memories Aiko has
        # actually managed to weave back in. Pure mechanics, no inner-
        # life cue — the reinforcement is invisible to the LLM by
        # design. Embeds assistant_text only (the user-said-this signal
        # is already covered by the revival path above; K22 measures
        # what *Aiko* successfully reached back to).
        if (
            bool(
                getattr(
                    self._settings.agent, "callback_detector_enabled", True,
                )
            )
            and assistant_text
            and len(assistant_text) >= 12
            and self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.conversation import callback_detector
                from datetime import datetime, timezone

                turn_vec = self._embedder.embed(assistant_text)
                # Stash for downstream consumers (K20 reads it as the
                # "claim Jacob is reacting to" centroid on the next
                # turn; carry-forward happens at the end of K20's
                # block below).
                self._last_assistant_vec = turn_vec

                # K30 — repeated-thought detection. Compare the
                # just-finished reply against the last-3 assistant
                # vectors (in-memory ring on the controller); if max
                # cosine >= ``self_noticing_repeated_cosine_threshold``,
                # arm the one-shot carry-forward flag so the NEXT
                # turn's provider surfaces a Heads-up. Reuses the
                # same ``turn_vec`` K22 already computed -- no extra
                # embed cost. Falls back silently when the
                # sub-detector is disabled OR the master switch is
                # off; the ring is still appended either way so
                # toggling the switch back on mid-session warms
                # cleanly.
                if bool(
                    getattr(
                        self._settings.agent, "self_noticing_enabled", True,
                    )
                ) and bool(
                    getattr(
                        self._settings.agent,
                        "self_noticing_repeated_thought_enabled",
                        True,
                    )
                ):
                    try:
                        from app.core.affect.self_pattern_detector import (
                            detect_repeated_thought,
                        )

                        prior = list(
                            getattr(
                                self,
                                "_self_noticing_aiko_vecs",
                                deque(),
                            )
                        )
                        result = detect_repeated_thought(
                            turn_vec,
                            prior,
                            threshold=float(
                                getattr(
                                    self._settings.agent,
                                    "self_noticing_repeated_cosine_threshold",
                                    0.85,
                                )
                            ),
                        )
                        self._repeated_thought_fired_last_turn = bool(
                            result.fired
                        )
                        self._repeated_thought_last_cosine = float(
                            result.max_cosine
                        )
                        self._repeated_thought_last_matched_index = int(
                            result.matched_index
                        )
                        if result.fired:
                            log.info(
                                "self-noticing repeated-thought: "
                                "cosine=%.3f matched_index=%d ring_size=%d",
                                result.max_cosine,
                                result.matched_index,
                                len(prior),
                            )
                    except Exception:
                        log.debug(
                            "self-noticing repeated-thought detect failed",
                            exc_info=True,
                        )
                try:
                    ring = getattr(self, "_self_noticing_aiko_vecs", None)
                    if ring is not None:
                        ring.append(turn_vec)
                except Exception:
                    log.debug(
                        "self-noticing vec-ring append failed", exc_info=True,
                    )
                now = datetime.now(timezone.utc)
                hits = callback_detector.detect(
                    assistant_vec=turn_vec,
                    memory_store=self._memory_store,
                    now=now,
                    threshold=float(
                        getattr(
                            self._memory_settings,
                            "callback_similarity_threshold",
                            0.55,
                        )
                    ),
                    age_floor_days=int(
                        getattr(
                            self._memory_settings,
                            "callback_age_floor_days",
                            3,
                        )
                    ),
                    cooldown_hours=int(
                        getattr(
                            self._memory_settings,
                            "callback_cooldown_hours",
                            24,
                        )
                    ),
                    top_k=int(
                        getattr(
                            self._memory_settings,
                            "callback_max_hits_per_turn",
                            3,
                        )
                    ),
                )
                if hits:
                    callback_detector.record(
                        memory_store=self._memory_store,
                        hits=hits,
                        salience_bump=float(
                            getattr(
                                self._memory_settings,
                                "callback_salience_bump",
                                0.05,
                            )
                        ),
                        revival_bump=float(
                            getattr(
                                self._memory_settings,
                                "callback_revival_bump",
                                0.10,
                            )
                        ),
                        now=now,
                        notify_memory_updated=self._notify_memory_updated,
                    )
            except Exception:
                log.debug("callback detector raised", exc_info=True)

        # K20 — metacognitive calibration. Post-turn pass that
        # classifies Jacob's last message into a calibration signal
        # (pushback_strong / pushback_mild / softening / affirmation)
        # and adjusts the per-user CalibrationState. The detector is
        # write-only here -- the inner-life provider on the *next*
        # turn reads the state and renders a one-line hedge cue when
        # warranted. Posture: verbal hedging only; no RAG retrieval
        # penalty (F3 owns that lane).
        #
        # The softening detector wants user_vec + the prior turn's
        # assistant_vec to gate on cosine + hedge-token AND. We
        # always embed user_text fresh here (~1-5ms warm) -- there's
        # no shared embed to steal because K6 runs at prompt-assembly
        # time, not post-turn. The prior_assistant_vec comes from
        # ``self._prior_assistant_vec`` (set at the bottom of this
        # block on the previous turn).
        calibration_store = getattr(self, "_calibration_store", None)
        if (
            bool(
                getattr(
                    self._settings.agent,
                    "calibration_detection_enabled",
                    True,
                )
            )
            and user_text
            and calibration_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.affect import calibration_detector
                from datetime import datetime, timezone

                prior_assistant_vec = getattr(
                    self, "_prior_assistant_vec", None,
                )
                # Embed user_text only when the softening detector
                # could fire (we have a prior assistant vec to
                # compare against); otherwise skip the embed to save
                # the round-trip -- pushback / affirmation regex
                # paths don't need user_vec.
                user_vec = None
                if prior_assistant_vec is not None:
                    try:
                        user_vec = self._embedder.embed(user_text)
                    except Exception:
                        log.debug(
                            "calibration: user_text embed failed",
                            exc_info=True,
                        )
                        user_vec = None

                signal = calibration_detector.detect(
                    user_text=user_text,
                    user_vec=user_vec,
                    prior_assistant_vec=prior_assistant_vec,
                    softening_cosine_threshold=float(
                        getattr(
                            self._memory_settings,
                            "calibration_softening_threshold",
                            0.70,
                        )
                    ),
                )
                if signal is not None:
                    now_cal = datetime.now(timezone.utc)
                    state = calibration_store.get(self._user_id)
                    # Decay before applying so the delta lands on a
                    # current snapshot rather than a stale one.
                    state = calibration_detector.decay(
                        state,
                        now=now_cal,
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
                    # Topic centroid = the *prior* assistant vec --
                    # that's the claim Jacob is doubting, not Aiko's
                    # response to the doubt.
                    state = calibration_detector.apply_signal(
                        state,
                        signal=signal,
                        assistant_vec=prior_assistant_vec,
                        now=now_cal,
                        topic_merge_threshold=float(
                            getattr(
                                self._memory_settings,
                                "calibration_topic_merge_threshold",
                                0.78,
                            )
                        ),
                        max_topic_slots=int(
                            getattr(
                                self._memory_settings,
                                "calibration_max_topic_slots",
                                8,
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
                    calibration_store.upsert(self._user_id, state)
            except Exception:
                log.debug("calibration detector raised", exc_info=True)

        # Carry the just-emitted assistant vec forward so the next
        # turn's K20 detector can read it as the "claim Jacob is
        # reacting to". Runs unconditionally so the carry-forward
        # works even when K20 itself is disabled mid-session; the
        # variable is just dead state in that case.
        last_assistant_vec = getattr(self, "_last_assistant_vec", None)
        if last_assistant_vec is not None:
            self._prior_assistant_vec = last_assistant_vec

        # Anti-rut layer: feed the AikoStylePatternTracker the
        # *stripped* spoken text (``assistant_text``, not
        # ``raw_assistant_text``) so we measure what the user heard,
        # not the raw model output with meta tags. Per-turn cost is a
        # deque append plus three short scans; the next turn's prompt
        # may carry an opener / question / length cue if a band trips.
        tracker = getattr(self, "_aiko_style_tracker", None)
        if tracker is not None and assistant_text:
            try:
                tracker.record_turn(assistant_text)
            except Exception:
                log.debug(
                    "aiko style tracker record_turn failed",
                    exc_info=True,
                )

        # K13 stylometric mirror: feed the user_text into the
        # analyzer and persist the updated window. Lazy cross-session
        # warmup happens on the very first record call, only if the
        # persisted blob didn't already populate the window (fresh
        # install). Per-turn cost is a few regex scans + a deque
        # append + a SQLite UPSERT of a small JSON blob.
        analyzer = getattr(self, "_style_signal_analyzer", None)
        if analyzer is not None and user_text:
            try:
                warmed = bool(getattr(self, "_style_signal_warmed", False))
                if not warmed and analyzer.window_size() == 0:
                    try:
                        recent = self._chat_db.get_messages(
                            self.session_key, limit=60,
                        )
                        history = [
                            (row.role, row.content) for row in recent
                        ]
                        analyzer.warm_from_history(history)
                    except Exception:
                        log.debug(
                            "style signal warm-from-history failed",
                            exc_info=True,
                        )
                    self._style_signal_warmed = True
                analyzer.record_user_turn(user_text)
                store = getattr(self, "_style_signal_store", None)
                if store is not None:
                    try:
                        store.upsert(self._user_id, analyzer.to_dict())
                    except Exception:
                        log.debug(
                            "style signal upsert failed", exc_info=True,
                        )
            except Exception:
                log.debug(
                    "style signal record_user_turn failed",
                    exc_info=True,
                )

        # Phase 2c: schedule a reflection during TTS playback.
        worker = getattr(self, "_reflection_worker", None)
        if worker is not None:
            session_key = self.session_key
            user_snapshot = (user_text or "")[:1500]
            assistant_snapshot = (assistant_text or "")[:1500]
            reaction_snapshot = reaction or "neutral"
            affect_after = state

            def _job(_stop_flag: Any) -> None:
                # Honor cooperative cancel before the LLM call too.
                if _stop_flag is not None and _stop_flag.is_set():
                    return
                try:
                    worker.maybe_run(
                        session_key=session_key,
                        user_text=user_snapshot,
                        assistant_text=assistant_snapshot,
                        reaction=reaction_snapshot,
                        affect_before=affect_before,
                        affect_after=affect_after,
                        on_memory_added=self._notify_memory_added,
                    )
                except Exception:
                    log.debug("reflection job raised", exc_info=True)

            try:
                from app.core.voice.speaking_window_scheduler import ScheduledJob

                self._scheduler.submit(ScheduledJob(
                    name="reflection",
                    priority=50,  # mid — reactive jobs (cancel) run sooner
                    estimated_seconds=4.0,
                    callable=_job,
                    dedupe_key="reflection",
                ))
            except Exception:
                log.debug("reflection job submit failed", exc_info=True)

        # Phase 2d: opportunistically schedule the daily self-image pulse.
        try:
            self._maybe_schedule_self_image_pulse()
        except Exception:
            log.debug("self-image schedule failed", exc_info=True)

        # Phase 3a: per-turn user-state heuristic (regex only, ~0.5ms).
        estimator = getattr(self, "_user_state_estimator", None)
        if estimator is not None:
            try:
                estimator.apply_turn(self._user_id, user_text=user_text)
            except Exception:
                log.debug("user-state estimator failed", exc_info=True)
        worker = getattr(self, "_user_profile_worker", None)
        if worker is not None:
            try:
                worker.notify_user_turn()
                self._maybe_schedule_user_profile_job()
            except Exception:
                log.debug("user-profile schedule failed", exc_info=True)

        # Phase 3b: bump turn counter + maybe surface a milestone callback.
        tracker = getattr(self, "_relationship_tracker", None)
        milestone: str | None = None
        if tracker is not None:
            try:
                _new_state, milestone = tracker.record_turn(self._user_id)
            except Exception:
                log.debug("relationship record_turn failed", exc_info=True)
                milestone = None
            if milestone:
                self._record_milestone_memory(milestone)
        self._last_turn_milestone = milestone

        # Phase 3c: post-turn promise regex (cheap) + maybe schedule LLM pass.
        extractor = getattr(self, "_promise_extractor", None)
        if extractor is not None:
            try:
                extractor.extract_post_turn(
                    user_text=user_text,
                    assistant_text=assistant_text,
                    session_key=self.session_key,
                )
                extractor.notify_user_turn()
                self._maybe_schedule_promise_llm_job()
            except Exception:
                log.debug("promise extraction failed", exc_info=True)

        # K4: per-turn dialogue-act tagger. Regex hot path is cheap
        # (microseconds) so we run it inline and write the result to
        # ``messages.dialogue_act`` immediately. Low-confidence results
        # (the fallback ``story`` bucket) get scheduled for an LLM
        # upgrade on the speaking-window scheduler. Subsequent
        # consumers (RAG retriever, ProactiveDirector) read the
        # column straight from ``messages``.
        tagger = getattr(self, "_dialogue_act_tagger", None)
        if tagger is not None:
            try:
                tagger.notify_user_turn()
                act_result = tagger.tag_user_turn(user_text)
                if user_message_id and act_result.act:
                    self._chat_db.update_message_dialogue_act(
                        int(user_message_id), act_result.act,
                    )
                if user_message_id and tagger.should_run_llm(
                    regex_result=act_result,
                ):
                    self._maybe_schedule_dialogue_act_llm_job(
                        message_id=int(user_message_id),
                        user_text=user_text,
                        regex_result=act_result,
                    )
            except Exception:
                log.debug("dialogue_act tagger failed", exc_info=True)

        # K17 — clarification-repair detector. Regex-only, runs inline
        # right after the dialogue_act tagger so its result lands in
        # the same "what was the shape of this turn" cluster. Stashes
        # a one-shot result on the controller; the next turn's
        # prompt assembler renders it via the inner-life provider.
        # Disabled-path: the detector returns ``None`` when the
        # setting is off, so the slot stays empty and the provider
        # short-circuits.
        if bool(
            getattr(self._settings.agent, "clarification_repair_enabled", True)
        ):
            try:
                from app.core.conversation import clarification_detector

                clarification_result = clarification_detector.detect(user_text)
                if clarification_result is not None:
                    self._pending_clarification = clarification_result
                    log.info(
                        "K17 clarification: band=%s evidence=%r",
                        clarification_result.band,
                        clarification_result.evidence,
                    )
            except Exception:
                log.debug("clarification detector raised", exc_info=True)

        # Phase 4a: inline [[agenda:...]] tags in raw assistant output.
        agenda_store = getattr(self, "_agenda_store", None)
        if agenda_store is not None and raw_assistant_text:
            try:
                from app.core.goals.agenda import extract_inline_tags

                for goal_text, importance in extract_inline_tags(raw_assistant_text):
                    agenda_store.add(
                        self._user_id,
                        goal=goal_text,
                        importance=importance,
                        source_session=self.session_key,
                    )
            except Exception:
                log.debug("agenda inline extraction failed", exc_info=True)
        agenda_worker = getattr(self, "_agenda_worker", None)
        if agenda_worker is not None:
            try:
                agenda_worker.notify_user_turn()
                self._maybe_schedule_agenda_groom_job()
            except Exception:
                log.debug("agenda groom schedule failed", exc_info=True)

        # Phase 4c: hot-path arc estimator on the user turn.
        estimator = getattr(self, "_arc_estimator", None)
        smoother = getattr(self, "_arc_smoother", None)
        arc_store = getattr(self, "_arc_store", None)
        try:
            current_turn = self._chat_db.get_message_count(self.session_key)
        except Exception:
            current_turn = 0
        if estimator is not None:
            try:
                estimator.apply_turn(
                    self._user_id,
                    user_text=user_text,
                    current_turn=current_turn,
                )
            except Exception:
                log.debug("arc estimator failed", exc_info=True)

        # H1: parse Aiko's ``[[arc:X]]`` self-tag from the raw reply and
        # write it to the store at confidence 0.85 (between regex and
        # smoother). Single-valued per turn -- if she emits more than
        # one, take the last and ignore the rest. The estimator above
        # ran first so the +0.1 confidence buffer protects this write
        # against an immediate same-turn regex bump.
        self_tagged_arc: str | None = None
        if (
            arc_store is not None
            and raw_assistant_text
        ):
            try:
                from app.core.conversation.conversation_arc import VALID_ARCS
                from app.core.services.response_text_service import (
                    parse_arc_tags,
                )

                tags = [t for t in parse_arc_tags(raw_assistant_text) if t in VALID_ARCS]
                if tags:
                    self_tagged_arc = tags[-1]
                    arc_store.set_from_self_tag(
                        self._user_id,
                        self_tagged_arc,
                        since_turn=current_turn,
                    )
                    log.info(
                        "H1 self-tag: aiko set arc=%r (confidence=0.85)",
                        self_tagged_arc,
                    )
            except Exception:
                log.debug("H1 arc self-tag dispatch failed", exc_info=True)

        # Stamp ``messages.arc`` on Aiko's row (preferring the self-tag,
        # falling back to the current store state) and on the user row
        # (always from the current store state) so the timeline filter
        # has full coverage.
        if arc_store is not None:
            try:
                state = arc_store.get(self._user_id)
                user_arc_value = state.arc if state is not None else None
                assistant_arc_value = self_tagged_arc or user_arc_value
                if user_arc_value and user_message_id:
                    self._chat_db.update_message_arc(
                        int(user_message_id), user_arc_value,
                    )
                if assistant_arc_value and assistant_message_id:
                    self._chat_db.update_message_arc(
                        int(assistant_message_id), assistant_arc_value,
                    )
            except Exception:
                log.debug("messages.arc stamp failed", exc_info=True)

        # K31 soft physicality: seal the per-turn gesture accumulator
        # onto the assistant message row so the chat bubble footer
        # badge survives a reload. The accumulator was populated by
        # ``avatar_mixin._emit_avatar_touch`` during the streaming
        # phase; here we flush it to ``messages.gestures``. The
        # helper is a no-op when the accumulator is empty.
        try:
            persist = getattr(self, "_persist_turn_gestures", None)
            if persist is not None and assistant_message_id:
                persist(int(assistant_message_id))
        except Exception:
            log.debug(
                "K31 _persist_turn_gestures failed", exc_info=True,
            )

        if smoother is not None:
            try:
                smoother.notify_user_turn()
                self._maybe_schedule_arc_smoother()
            except Exception:
                log.debug("arc smoother schedule failed", exc_info=True)

        # Phase 4c: notify narrative weaver and maybe enqueue.
        weaver = getattr(self, "_narrative_weaver", None)
        if weaver is not None:
            try:
                weaver.notify_user_turn()
                self._maybe_schedule_narrative_weaver()
            except Exception:
                log.debug("narrative weaver schedule failed", exc_info=True)

        # Phase 4b: opportunistic maintenance jobs (consolidator + pulse).
        try:
            self._maybe_schedule_consolidator()
        except Exception:
            log.debug("consolidator schedule failed", exc_info=True)
        try:
            self._maybe_schedule_relationship_pulse()
        except Exception:
            log.debug("relationship pulse schedule failed", exc_info=True)
        # Phase 2c (Aiko human-like upgrades): mine recurring phrases.
        try:
            self._maybe_schedule_catchphrase_miner()
        except Exception:
            log.debug("catchphrase miner schedule failed", exc_info=True)
        # Phase 4c: small follow-up question on shallow arcs.
        try:
            self._maybe_schedule_curiosity(
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception:
            log.debug("curiosity worker schedule failed", exc_info=True)

        # Schema v7: shared moments + relationship axes. Order matters —
        # extract inline tags first so the axes updater sees their vibes.
        moment_vibes_this_turn: list[str] = []
        moments_store = getattr(self, "_shared_moments_store", None)
        if (
            moments_store is not None
            and raw_assistant_text
            and bool(getattr(self._settings.agent, "shared_moments_enabled", True))
        ):
            try:
                from app.core.relationship.shared_moment_extractor import extract_inline_tags

                for candidate in extract_inline_tags(raw_assistant_text):
                    row = moments_store.add_from_candidate(
                        candidate,
                        source_session=self.session_key,
                    )
                    if row is not None:
                        moment_vibes_this_turn.append(row.vibe)
                        detector = getattr(self, "_moment_detector", None)
                        if detector is not None:
                            try:
                                detector.note_tag_persisted()
                            except Exception:
                                pass
                        self._notify_shared_moment_added(row)
            except Exception:
                log.debug("shared-moment inline extraction failed", exc_info=True)
        self._last_turn_moment_vibes = moment_vibes_this_turn

        # F2: inline [[gap:topic:question]] tags. Same shape as the
        # moments extraction above — pure regex over the raw assistant
        # text, ``prune_overflow`` keeps the cap honoured.
        gap_store = getattr(self, "_knowledge_gap_store", None)
        if gap_store is not None and raw_assistant_text:
            try:
                from app.core.memory.knowledge_gap_extractor import (
                    extract_inline_tags as _extract_gaps,
                )

                for candidate in _extract_gaps(raw_assistant_text):
                    gap = gap_store.add_gap(
                        topic=candidate.topic,
                        question=candidate.question,
                        source_session=self.session_key,
                    )
                    if gap is not None:
                        self._notify_knowledge_gap_added(gap)
            except Exception:
                log.debug("knowledge gap inline extraction failed", exc_info=True)

        # F5: inline [[conflict:reason]] self-tag. Aiko emits this when
        # she notices a memory contradiction mid-turn ("hold on, that
        # doesn't match what you told me last week"). We log the
        # reason for audit and force_run the F5 worker so the
        # conflict surfaces in the next idle window even if it's
        # outside the regular cadence. The cosine band + heuristic
        # gate still filters the candidate pairs -- we don't try to
        # attribute the tag to a specific (a, b) here.
        conflict_worker = getattr(self, "_memory_conflict_worker", None)
        if conflict_worker is not None and raw_assistant_text:
            try:
                from app.core.services.response_text_service import (
                    extract_conflict_tags,
                )

                tags = extract_conflict_tags(raw_assistant_text)
                if tags:
                    log.info(
                        "F5 self-flag: aiko reported %d conflict reason(s): %s",
                        len(tags),
                        [t[:120] for t in tags],
                    )
                    scheduler = getattr(self, "_idle_scheduler", None)
                    if scheduler is not None:
                        try:
                            scheduler.force_run(conflict_worker.name)
                        except Exception:
                            log.debug(
                                "F5 force_run failed", exc_info=True,
                            )
            except Exception:
                log.debug(
                    "conflict-tag inline extraction failed", exc_info=True,
                )

        # K2: inline [[predict:kind:topic:state:confidence]] self-tag.
        # Aiko's theory-of-mind prediction about the user gets parsed
        # here and upserted into the BeliefStore. We optionally embed
        # the topic so the store can fuzzy-merge near-duplicates on
        # the next upsert. The gap detector pass below picks up the
        # fresh row if its mood prediction disagrees with the live
        # affect read.
        belief_store = getattr(self, "_belief_store", None)
        if (
            belief_store is not None
            and raw_assistant_text
            and bool(getattr(self._settings.agent, "belief_tracking_enabled", True))
        ):
            try:
                from app.core.services.response_text_service import (
                    extract_predict_tags,
                )

                tags = extract_predict_tags(raw_assistant_text)
                if tags:
                    log.info(
                        "K2 self-flag: aiko predicted %d belief(s)",
                        len(tags),
                    )
                    embedder = getattr(self, "_embedder", None)
                    for t in tags:
                        embedding = None
                        if embedder is not None:
                            try:
                                embedding = embedder.embed(t.topic)
                            except Exception:
                                log.debug(
                                    "K2 embed topic failed",
                                    exc_info=True,
                                )
                        try:
                            belief = belief_store.upsert(
                                user_id=self._user_id,
                                kind=t.kind,
                                topic=t.topic,
                                predicted_state=t.predicted_state,
                                confidence=t.confidence,
                                source="self_tag",
                                topic_embedding=embedding,
                            )
                            if belief is not None:
                                log.info(
                                    "K2 belief from tag: id=%s kind=%s "
                                    "topic=%r state=%r confidence=%.2f",
                                    belief.id,
                                    belief.kind,
                                    belief.topic,
                                    belief.predicted_state,
                                    belief.confidence,
                                )
                                self._notify_belief_added(belief.to_payload())
                        except Exception:
                            log.debug(
                                "K2 upsert from tag raised", exc_info=True,
                            )
            except Exception:
                log.debug(
                    "predict-tag inline extraction failed", exc_info=True,
                )

        # K1: inline [[goal:summary]] self-tag. Aiko declares one of
        # her own long-term goals mid-turn; we hand each unique body
        # to :meth:`GoalStore.add_goal` so it becomes a ``goal``
        # memory row with ``source='self_tag'``. Cap enforcement
        # (max_active + archive-on-overflow) lives inside the store,
        # so a chatty turn that emits five tags cannot blow the ring
        # past its budget.
        goal_store = getattr(self, "_goal_store", None)
        if (
            goal_store is not None
            and raw_assistant_text
            and bool(getattr(self._settings.agent, "goals_enabled", True))
        ):
            try:
                from app.core.services.response_text_service import (
                    extract_goal_tags,
                )

                summaries = extract_goal_tags(raw_assistant_text)
                if summaries:
                    log.info(
                        "K1 self-flag: aiko declared %d goal(s)",
                        len(summaries),
                    )
                    for summary in summaries:
                        try:
                            mem = goal_store.add_goal(
                                summary=summary,
                                source="self_tag",
                                source_session=self.session_key,
                                source_turn_id=assistant_message_id,
                            )
                            if mem is not None:
                                log.info(
                                    "K1 goal from tag: id=%s summary=%r",
                                    mem.id,
                                    (mem.metadata or {}).get(
                                        "summary", mem.content
                                    )[:120],
                                )
                                if self._notify_memory_added is not None:
                                    try:
                                        self._notify_memory_added(
                                            mem.to_dict()
                                        )
                                    except Exception:
                                        log.debug(
                                            "K1 notify_memory_added raised",
                                            exc_info=True,
                                        )
                        except Exception:
                            log.debug(
                                "K1 add_goal from tag raised",
                                exc_info=True,
                            )
            except Exception:
                log.debug(
                    "goal-tag inline extraction failed", exc_info=True,
                )

        # K2: post-turn gap detector pass. Compares active mood
        # beliefs against the live affect read and active opinion
        # beliefs against the user's most recent message. Surfaced
        # gaps are stashed for the next-turn ``_render_belief_gaps_block``
        # provider to consume.
        gap_detector = getattr(self, "_belief_gap_detector", None)
        if (
            gap_detector is not None
            and bool(getattr(self._settings.agent, "belief_tracking_enabled", True))
        ):
            try:
                affect_store = getattr(self, "_affect_store", None)
                affect = (
                    affect_store.get(self._user_id)
                    if affect_store is not None
                    else None
                )
                gaps = gap_detector.detect(
                    user_id=self._user_id,
                    affect=affect,
                    recent_user_message=user_text,
                )
                if gaps:
                    self._pending_belief_gaps = list(gaps)
                    # Mirror the per-row contradiction flips out to
                    # listeners so the UI's Beliefs sub-tab can
                    # refresh without polling.
                    for g in gaps:
                        try:
                            row = belief_store.get(g.belief_id) if belief_store else None
                            if row is not None:
                                self._notify_belief_updated(row.to_payload())
                        except Exception:
                            log.debug(
                                "K2 notify_belief_updated raised",
                                exc_info=True,
                            )
            except Exception:
                log.debug("belief gap detector raised", exc_info=True)

        # K14: implicit engagement signal. Runs *before* the axes
        # updater so the closeness_delta can ride in the same
        # ``apply_turn`` call. Also stashes the label (consumed by the
        # typed-proactive eligibility predicate) and the absence_seconds
        # band (consumed by the next turn's absence-curiosity provider).
        # The tracker reads K13's rolling word-count window via the
        # provider wired at construction time, so the K13 record_turn
        # block above runs first by design (post-turn order matters).
        engagement_delta = 0.0
        engagement_tracker = getattr(self, "_engagement_tracker", None)
        if (
            engagement_tracker is not None
            and bool(getattr(self._settings.agent, "engagement_tracker_enabled", True))
        ):
            try:
                latency_seconds = self._compute_user_reply_latency_seconds(
                    user_message_id=user_message_id,
                )
                word_count = len((user_text or "").split()) or 0
                engagement = engagement_tracker.record_turn(
                    mode=getattr(self, "_last_turn_mode", "typed"),
                    latency_seconds=latency_seconds,
                    user_word_count=word_count,
                )
                engagement_delta = float(engagement.closeness_delta)
                self._last_engagement_label = engagement.label
                self._pending_absence_seconds = engagement.absence_seconds
                # K28: arm the turning-over cue in parallel with K14's
                # absence_curiosity slot. The two stack on the 90 min -
                # 4h overlap (K14 frames the welcome-back, K28 adds
                # "...and I was thinking about X"). Extracted into a
                # helper so the unit test in
                # ``tests/test_post_turn_turning_over.py`` can exercise
                # the gates without re-running the whole post-turn
                # orchestrator.
                self._maybe_arm_turning_over_slot(engagement)
                # K36: arm the "things I did while you were away" cue on
                # the same typed-gap signal (own 4h threshold). The
                # provider defers to turning_over so only one fires.
                self._maybe_arm_away_activities_slot(engagement)
                self._maybe_arm_forward_curiosity_slot(engagement)
                log.info(
                    "engagement: mode=%s label=%s delta=%+.4f "
                    "latency_s=%s length_z=%s warmed=%s",
                    engagement.mode,
                    engagement.label,
                    engagement.closeness_delta,
                    (
                        f"{engagement.latency_seconds:.2f}"
                        if engagement.latency_seconds is not None else "-"
                    ),
                    (
                        f"{engagement.length_z:+.2f}"
                        if engagement.length_z is not None else "-"
                    ),
                    engagement.warmed,
                )
            except Exception:
                log.debug("engagement tracker raised", exc_info=True)

        # Apply per-turn drift to the relationship axes. Cheap (no LLM).
        axes_updater = getattr(self, "_relationship_axes_updater", None)
        if (
            axes_updater is not None
            and bool(getattr(self._settings.agent, "relationship_axes_enabled", True))
        ):
            try:
                from app.core.relationship.shared_moment_extractor import (
                    detect_moment_reaction_tags,
                )

                reaction_tag_set = detect_moment_reaction_tags(raw_assistant_text or "")
                if reaction:
                    reaction_tag_set.add(str(reaction).lower())
                axes_state = axes_updater.apply_turn(
                    self._user_id,
                    reaction_tags=reaction_tag_set,
                    moment_vibes=moment_vibes_this_turn,
                    milestone=milestone,
                    gift_received=bool(self._last_turn_gift_received),
                    promise_kept=bool(self._last_turn_promise_kept),
                    user_text=user_text,
                    engagement_delta=engagement_delta,
                )
                # Reset per-turn flags now that they've been consumed.
                self._last_turn_gift_received = False
                self._last_turn_promise_kept = False
                self._maybe_notify_axes(axes_state)
            except Exception:
                log.debug("relationship axes update failed", exc_info=True)

        # Schedule the LLM moment detector when a moment-worthy signal
        # fired AND cadence allows. Detector internally throttles further.
        detector = getattr(self, "_moment_detector", None)
        if (
            detector is not None
            and moments_store is not None
            and bool(getattr(self._settings.agent, "shared_moments_enabled", True))
            and bool(getattr(self._settings.agent, "shared_moments_llm_enabled", True))
        ):
            try:
                detector.notify_user_turn()
                self._maybe_schedule_moment_llm_job(
                    user_text=user_text,
                    assistant_text=assistant_text,
                    raw_assistant_text=raw_assistant_text,
                    milestone=milestone,
                )
            except Exception:
                log.debug("moment detector schedule failed", exc_info=True)

        # K15 — self-disclosure / vulnerability budget spend hook.
        # Delegates to :func:`vulnerability_budget.compute_spend_for_self_tags`
        # which re-parses ``[[remember:self:...]]`` tags (same regex
        # the TurnRunner uses to extract memory rows), classifies
        # each body, sums the cost, and applies wall-clock decay +
        # spend. The pure helper makes this block source-testable
        # without spinning up the post-turn orchestrator. Soft-only:
        # exceeding capacity is allowed; the provider just renders
        # a stronger cue next turn. Best-effort -- any failure path
        # logs at DEBUG so a single broken tag can't strand the
        # post-turn pipeline. The K30 self-noticing block runs
        # earlier in this function (ring append + repeated-thought
        # detect, no spend); K15 spends here so the post-turn order
        # of operations matches the prompt assembler's render order
        # (self_noticing block, then vulnerability_budget block).
        agent_settings = getattr(self._settings, "agent", None)
        if (
            agent_settings is not None
            and bool(
                getattr(agent_settings, "vulnerability_budget_enabled", True)
            )
            and raw_assistant_text
        ):
            try:
                from datetime import datetime, timezone

                from app.core.affect import vulnerability_budget as _vb

                chat_db = getattr(self, "_chat_db", None)
                if chat_db is not None:
                    now = datetime.now(timezone.utc)
                    try:
                        stored = chat_db.kv_get(_vb.KV_BUDGET_STATE)
                    except Exception:
                        log.debug(
                            "K15 kv_get(budget) failed", exc_info=True,
                        )
                        stored = None
                    state = _vb.deserialize(stored)
                    report = _vb.compute_spend_for_self_tags(
                        raw_assistant_text, state, now,
                        settings=agent_settings,
                    )
                    # Only write when something actually spent (or
                    # the timestamp meaningfully moved). The no-spend
                    # / no-change path is the steady state and must
                    # NOT churn kv_meta on every turn.
                    if (
                        report.total_cost > 0
                        or report.new_state.spent != state.spent
                    ):
                        try:
                            chat_db.kv_set(
                                _vb.KV_BUDGET_STATE,
                                _vb.serialize(report.new_state),
                            )
                        except Exception:
                            log.debug(
                                "K15 kv_set(budget) failed", exc_info=True,
                            )
                    if report.total_cost > 0:
                        log.info(
                            "vulnerability-budget spend: cost=%d "
                            "tier_counts=%s spent=%.2f -> %.2f",
                            report.total_cost,
                            report.tier_counts,
                            float(state.spent),
                            float(report.new_state.spent),
                        )
            except Exception:
                log.debug(
                    "vulnerability-budget spend hook raised", exc_info=True,
                )

        # K52 — wants-ledger acted-on detection. A want is satisfied
        # when its topic surfaced this turn, whether Aiko raised it or
        # the user happened to (once a topic has come up even briefly,
        # it's done). Content-word overlap over user + assistant text,
        # same shape as revival detection above. Acting on a want
        # removes it and starts the re-entry cooldown so the feeder
        # doesn't immediately re-add it — that visible relief is what
        # makes the ledger read as satisfaction, not a checklist.
        if (
            agent_settings is not None
            and bool(getattr(agent_settings, "wants_ledger_enabled", True))
        ):
            try:
                from datetime import datetime, timezone

                from app.core.conversation import wants_ledger as _wl

                chat_db = getattr(self, "_chat_db", None)
                if chat_db is not None:
                    state = _wl.deserialize(
                        chat_db.kv_get(_wl.KV_WANTS_LEDGER)
                    )
                    if state.wants:
                        turn_text = " ".join(
                            t for t in (user_text, assistant_text) if t
                        )
                        hits = _wl.detect_acted(state, turn_text)
                        if hits:
                            now = datetime.now(timezone.utc)
                            for want_id in hits:
                                want = next(
                                    (
                                        w for w in state.wants
                                        if w.id == want_id
                                    ),
                                    None,
                                )
                                state = _wl.mark_acted(state, want_id, now)
                                if want is not None:
                                    log.info(
                                        "wants-ledger acted: id=%s "
                                        "source=%s pressure=%.2f text=%s",
                                        want.id, want.source,
                                        want.pressure, want.text[:80],
                                    )
                            chat_db.kv_set(
                                _wl.KV_WANTS_LEDGER, _wl.serialize(state),
                            )
            except Exception:
                log.debug("wants acted-on hook raised", exc_info=True)

        # K55 — thread-ownership stamp. When this turn carried a K53
        # initiative directive or a K52 imperative want (the provider
        # armed ``_pending_thread_open`` at assembly time), record the
        # opened topic + its embedding so the NEXT user reply gets
        # exactly one engaged-or-pivot evaluation. The flag is
        # consumed unconditionally so a disabled switch can't leave a
        # stale pending stamp behind.
        pending_open = getattr(self, "_pending_thread_open", None)
        self._pending_thread_open = None
        if (
            pending_open is not None
            and agent_settings is not None
            and bool(
                getattr(agent_settings, "thread_ownership_enabled", True)
            )
        ):
            try:
                from app.core.conversation import thread_ownership as _town

                topic = _town.derive_topic(
                    pending_open.get("topic"), assistant_text or "",
                )
                if topic:
                    embedding = None
                    embedder = getattr(self, "_embedder", None)
                    if embedder is not None:
                        try:
                            embedding = embedder.embed(topic)
                        except Exception:
                            embedding = None
                    self._owned_thread = _town.OwnedThread(
                        topic=topic,
                        source=str(
                            pending_open.get("source") or "initiative"
                        ),
                        embedding=embedding,
                    )
                    log.info(
                        "thread-ownership stamp: source=%s embedded=%s "
                        "topic=%s",
                        self._owned_thread.source,
                        embedding is not None,
                        topic[:80],
                    )
            except Exception:
                log.debug("thread ownership stamp raised", exc_info=True)
