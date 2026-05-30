"""Post-turn inner-life mixin.

Extracted from :mod:`app.core.session_controller` to keep the
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
``app.core.session_controller.<symbol>`` for any of the moved methods
must patch ``app.core.session.post_turn_mixin.<symbol>`` instead. The
patch must target the module where the symbol is *looked up*.
"""
from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("app.session")


class PostTurnMixin:
    """``_resolve_curiosity_seeds``, revival detection, ``_post_turn_inner_life``."""

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
                from app.core.speaking_window_scheduler import ScheduledJob

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

        # Phase 4a: inline [[agenda:...]] tags in raw assistant output.
        agenda_store = getattr(self, "_agenda_store", None)
        if agenda_store is not None and raw_assistant_text:
            try:
                from app.core.agenda import extract_inline_tags

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
                from app.core.conversation_arc import VALID_ARCS
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
                from app.core.shared_moment_extractor import extract_inline_tags

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
                from app.core.knowledge_gap_extractor import (
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

        # Apply per-turn drift to the relationship axes. Cheap (no LLM).
        axes_updater = getattr(self, "_relationship_axes_updater", None)
        if (
            axes_updater is not None
            and bool(getattr(self._settings.agent, "relationship_axes_enabled", True))
        ):
            try:
                from app.core.shared_moment_extractor import (
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
