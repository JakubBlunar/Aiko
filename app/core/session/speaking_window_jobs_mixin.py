"""Speaking-window background-job scheduling mixin.

Extracted from :mod:`app.core.session.session_controller` to keep the controller
shell readable. Covers the entire ``_maybe_schedule_*`` cluster — the
12 per-turn checks that submit cohort-level work into the
``SpeakingWindowScheduler`` so heavy LLM/IO jobs only run while Aiko
is talking and the user can't tell the engine is busy. Also moves the
adjacent ``_record_milestone_memory`` helper, which the milestone
hook calls on the same path.

Each method follows the same shape:

1. Read the relevant store/worker off ``self``; bail if it's None.
2. Ask the worker ``should_run`` / ``should_run_llm`` so we don't
   over-submit.
3. Build a closure that captures the data the job needs and
   submits a :class:`ScheduledJob` to the speaking-window scheduler.

State ownership stays in ``SessionController.__init__``; this mixin
just reads ``self.*`` and delegates to ``self._scheduler.submit``.

NB: tests that previously patched
``app.core.session.session_controller.<symbol>`` for any of the moved methods
must patch
``app.core.session.speaking_window_jobs_mixin.<symbol>`` instead.
The patch must target the module where the symbol is *looked up*.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from app.core.conversation.dialogue_act_tagger import DialogueActResult


log = logging.getLogger("app.session")


class SpeakingWindowJobsMixin:
    """Per-turn ``_maybe_schedule_*`` cluster + milestone memory helper."""

    def _record_milestone_memory(self, label: str) -> None:
        """Persist a milestone as a callback memory so RAG surfaces it."""
        if not label:
            return
        store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        if store is None or embedder is None:
            return
        humanized = label.replace("_", " ")
        content = (
            f"Aiko reached a milestone with {self.user_display_name}: {humanized}. "
            "She might naturally bring this up in conversation."
        )
        try:
            emb = embedder.embed(content)
        except Exception:
            log.debug("milestone embed failed", exc_info=True)
            return
        try:
            mem = store.add(
                content=content,
                kind="callback",
                embedding=emb,
                salience=0.6,
                source_session=self.session_key,
                # Schema v8: relationship milestones are real,
                # confirmed events. Long_term so they survive the
                # scratchpad TTL even if RAG never re-surfaces them.
                tier="long_term",
            )
        except Exception:
            log.debug("milestone memory insert failed", exc_info=True)
            return
        if mem is not None:
            log.info("relationship milestone recorded: %s", label)
            try:
                self._notify_memory_added(mem)
            except Exception:
                pass

    def _maybe_schedule_agenda_groom_job(self) -> None:
        """Phase 4a: enqueue AgendaWorker grooming pass on the speaking window."""
        worker = getattr(self, "_agenda_worker", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("agenda should_run failed", exc_info=True)
            return

        session_key = self.session_key
        user_id = self._user_id
        history_window = 16

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                worker.maybe_run(
                    user_id, history_provider=_history_provider,
                )
            except Exception:
                log.debug("agenda groom job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="agenda_groom",
                priority=70,
                estimated_seconds=4.5,
                callable=_job,
                dedupe_key="agenda_groom",
            ))
        except Exception:
            log.debug("agenda groom submit failed", exc_info=True)

    def _maybe_schedule_dialogue_act_llm_job(
        self,
        *,
        message_id: int,
        user_text: str,
        regex_result: "DialogueActResult",
    ) -> None:
        """K4: enqueue the LLM dialogue-act upgrade for one user row.

        Only fires when the regex confidence sat at the low-confidence
        floor (typically the fallback ``story`` bucket). The worker
        re-tags the message and patches ``messages.dialogue_act`` if
        the LLM disagrees with the regex.
        """
        tagger = getattr(self, "_dialogue_act_tagger", None)
        if tagger is None:
            return

        session_key = self.session_key
        history_window = 8

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        captured_text = str(user_text or "")
        captured_id = int(message_id)
        captured_regex = regex_result

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                tagger.maybe_run_llm(
                    message_id=captured_id,
                    user_text=captured_text,
                    regex_result=captured_regex,
                    history_provider=_history_provider,
                )
            except Exception:
                log.debug("dialogue_act llm job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="dialogue_act_llm",
                priority=70,
                estimated_seconds=2.0,
                callable=_job,
                dedupe_key=f"dialogue_act_llm:{captured_id}",
            ))
        except Exception:
            log.debug("dialogue_act llm submit failed", exc_info=True)

    def _maybe_schedule_moment_llm_job(
        self,
        *,
        user_text: str,
        assistant_text: str,
        raw_assistant_text: str,
        milestone: str | None,
        gift_signal: bool = False,
        promise_kept_signal: bool = False,
    ) -> None:
        """Schema v7: enqueue the LLM moment detector when signals warrant.

        Gating is a two-step process: a cheap signal check here (so we
        only spend cycles on candidate turns), and a cadence/cooldown
        check inside :class:`MomentDetector.should_run_llm`. Skipping
        either short-circuits the job.

        ``gift_signal`` / ``promise_kept_signal`` are passed in by the
        caller (snapshotted before the axes updater clears the per-turn
        flags) rather than read off ``self`` here — the J7 fix, since the
        instance flags are already cleared by the time this runs.
        """
        detector = getattr(self, "_moment_detector", None)
        if detector is None:
            return

        try:
            from app.core.relationship.shared_moment_extractor import detect_moment_reaction_tags

            reaction_signal = bool(
                detect_moment_reaction_tags(raw_assistant_text or "")
            )
        except Exception:
            reaction_signal = False

        gift_signal = bool(gift_signal)
        promise_kept_signal = bool(promise_kept_signal)
        milestone_signal = bool(milestone)
        now_monotonic = time.monotonic()
        try:
            should = detector.should_run_llm(
                reaction_signal=reaction_signal,
                milestone_signal=milestone_signal,
                gift_signal=gift_signal,
                promise_kept_signal=promise_kept_signal,
                now_monotonic=now_monotonic,
            )
        except Exception:
            log.debug("moment detector should_run failed", exc_info=True)
            return
        if not should:
            return

        session_key = self.session_key
        history_window = 10

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                detector.maybe_run_llm(
                    history_provider=_history_provider,
                    now_monotonic=time.monotonic(),
                    reaction_signal=reaction_signal,
                    milestone_signal=milestone_signal,
                    gift_signal=gift_signal,
                    promise_kept_signal=promise_kept_signal,
                )
            except Exception:
                log.debug("moment llm job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="moment_llm",
                priority=75,
                estimated_seconds=3.5,
                callable=_job,
                dedupe_key="moment_llm",
            ))
        except Exception:
            log.debug("moment llm submit failed", exc_info=True)

    def _maybe_schedule_user_profile_job(self) -> None:
        """Phase 3a: enqueue UserProfileWorker via the speaking window."""
        worker = getattr(self, "_user_profile_worker", None)
        if worker is None:
            return
        try:
            if not worker.should_run():
                return
        except Exception:
            log.debug("profile worker should_run failed", exc_info=True)
            return

        session_key = self.session_key
        user_id = self._user_id
        history_window = 24

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                worker.maybe_run(
                    user_id,
                    session_key=session_key,
                    history_provider=_history_provider,
                )
            except Exception:
                log.debug("user profile job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="user_profile",
                priority=60,
                estimated_seconds=4.0,
                callable=_job,
                dedupe_key="user_profile",
            ))
        except Exception:
            log.debug("user profile submit failed", exc_info=True)

    def _maybe_schedule_self_image_pulse(self) -> None:
        """Phase 2d: enqueue a daily self-image rebuild during TTS playback."""
        worker = getattr(self, "_self_image_worker", None)
        if worker is None:
            return
        try:
            if not worker.should_run():
                return
        except Exception:
            log.debug("self-image should_run check failed", exc_info=True)
            return

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                new_text = worker.pulse()
                if new_text:
                    log.info(
                        "self-image pulse wrote %d chars",
                        len(new_text),
                    )
            except Exception:
                log.debug("self-image pulse raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="self_image_pulse",
                priority=80,  # lowest — daily, not urgent
                estimated_seconds=5.0,
                callable=_job,
                dedupe_key="self_image_pulse",
            ))
        except Exception:
            log.debug("self-image pulse submit failed", exc_info=True)

    def _maybe_schedule_consolidator(self) -> None:
        """Phase 4b: enqueue the memory-consolidator pass."""
        worker = getattr(self, "_consolidator", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("consolidator should_run failed", exc_info=True)
            return

        user_id = self._user_id

        def _job(stop_flag: Any) -> None:
            try:
                worker.maybe_run(user_id, stop_flag=stop_flag)
            except Exception:
                log.debug("consolidator job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="memory_consolidator",
                priority=85,  # very low — daily-ish maintenance
                estimated_seconds=6.0,
                callable=_job,
                dedupe_key="memory_consolidator",
            ))
        except Exception:
            log.debug("consolidator submit failed", exc_info=True)

    def _maybe_schedule_arc_smoother(self) -> None:
        """Phase 4c: enqueue ArcSmootherWorker if it's due."""
        worker = getattr(self, "_arc_smoother", None)
        if worker is None:
            return
        try:
            if not worker.should_run():
                return
        except Exception:
            log.debug("arc smoother should_run failed", exc_info=True)
            return

        session_key = self.session_key
        user_id = self._user_id
        history_window = 12

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                current_turn = self._chat_db.get_message_count(session_key)
            except Exception:
                current_turn = 0
            try:
                worker.maybe_run(
                    user_id,
                    history_provider=_history_provider,
                    current_turn=current_turn,
                )
            except Exception:
                log.debug("arc smoother job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="arc_smoother",
                priority=72,
                estimated_seconds=3.5,
                callable=_job,
                dedupe_key="arc_smoother",
            ))
        except Exception:
            log.debug("arc smoother submit failed", exc_info=True)

    def _maybe_schedule_narrative_weaver(self) -> None:
        """Phase 4c: enqueue NarrativeWeaver to refill prepared_nudge."""
        worker = getattr(self, "_narrative_weaver", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("narrative weaver should_run failed", exc_info=True)
            return

        user_id = self._user_id

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                worker.maybe_run(user_id)
            except Exception:
                log.debug("narrative weaver job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="narrative_weaver",
                priority=68,
                estimated_seconds=3.0,
                callable=_job,
                dedupe_key="narrative_weaver",
            ))
        except Exception:
            log.debug("narrative weaver submit failed", exc_info=True)

    def _maybe_schedule_relationship_pulse(self) -> None:
        """Phase 4b: enqueue the weekly relationship-pulse summary."""
        worker = getattr(self, "_relationship_pulse", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("relationship pulse should_run failed", exc_info=True)
            return

        user_id = self._user_id

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                worker.maybe_run(user_id)
            except Exception:
                log.debug("relationship pulse job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="relationship_pulse",
                priority=82,
                estimated_seconds=5.5,
                callable=_job,
                dedupe_key="relationship_pulse",
            ))
        except Exception:
            log.debug("relationship pulse submit failed", exc_info=True)

    def _maybe_schedule_curiosity(
        self,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Phase 4c: enqueue a curiosity-follow-up pass.

        Mid-priority (75) so it lands between agenda (lower) and arc
        (higher). Internally throttled to ``min_turns_between`` /
        ``min_seconds_between`` and skips automatically when the arc
        isn't shallow.
        """
        worker = getattr(self, "_curiosity_worker", None)
        if worker is None:
            return
        store = getattr(self, "_arc_store", None)
        arc_label = "casual_check_in"
        if store is not None:
            try:
                state = store.get_or_default(self._user_id)
                arc_label = getattr(state, "arc", arc_label) or arc_label
            except Exception:
                log.debug("curiosity arc lookup failed", exc_info=True)
        session_key = self.session_key
        user_snap = (user_text or "")[:1000]
        asst_snap = (assistant_text or "")[:1000]

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                worker.maybe_run(
                    session_key=session_key,
                    user_text=user_snap,
                    assistant_text=asst_snap,
                    arc_label=arc_label,
                    on_memory_added=self._notify_memory_added,
                )
            except Exception:
                log.debug("curiosity worker job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="curiosity",
                priority=75,
                estimated_seconds=2.5,
                callable=_job,
                dedupe_key="curiosity",
            ))
        except Exception:
            log.debug("curiosity worker submit failed", exc_info=True)

    def _maybe_schedule_catchphrase_miner(self) -> None:
        """Phase 2c: enqueue the recurring-phrase miner.

        Low-priority (90) so it lands after the more reactive workers
        (reflection, narrative weaver). Internally throttled to one
        run per ``catchphrase_miner_min_seconds_between`` window.
        """
        miner = getattr(self, "_catchphrase_miner", None)
        if miner is None:
            return
        session_key = self.session_key

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                miner.maybe_run(session_key=session_key)
            except Exception:
                log.debug("catchphrase miner job raised", exc_info=True)

        try:
            from app.core.voice.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="catchphrase_miner",
                priority=90,
                estimated_seconds=2.5,
                callable=_job,
                dedupe_key="catchphrase_miner",
            ))
        except Exception:
            log.debug("catchphrase miner submit failed", exc_info=True)
