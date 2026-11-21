"""Session wiring for local speech learning and durable task reporting."""

import logging
import threading

from app.core.brain.events import ProactiveEvent
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.main_wake import main_wake_floor_busy
from app.core.persona.speech_guide import SpeechGuide, digest
from app.core.tasks.work_reports import WorkReports


log = logging.getLogger(__name__)


class WorkReportingMixin:
    def _init_live_work_reports(self):
        # Readiness hints contain only outbox identities. WorkReports owns the draft and receipt.
        self._live_work_reports_ready: dict[tuple[str, int], None] = {}
        self._live_work_report_event: ProactiveEvent | None = None
        self._live_work_report_lock = threading.Lock()

    def _init_work_reporting(self):
        if getattr(self, "_workflow_skill_registry", None) is None:
            return
        if getattr(self, "_work_reports", None) is not None:
            return
        self._init_live_work_reports()
        self._work_speech_guide = SpeechGuide(
            self._chat_db,
            user=lambda: self._user_id,
            session=lambda: self.session_key,
            persona=lambda: self._prompt_assembler.task_speaking_persona(),
            client=lambda: getattr(self, "_workflow_client", None),
            model=lambda: (
                getattr(self._settings.llm.routes.get("workflow"), "model", None)
                or getattr(self, "_effective_worker_model", None)
            ),
            window=lambda: self._workflow_context_window(),
            stopped=lambda: bool(
                getattr(self, "_work_reports", None) and self._work_reports.closed.is_set()
            ),
        )
        guide = self._work_speech_guide
        self._work_reports = WorkReports(
            self._chat_db,
            self._task_orchestrator,
            guide,
            user=lambda: self._user_id,
            session=lambda: self.session_key,
            client=guide.client,
            model=guide.model,
            window=guide.window,
            context=self._work_speaking_context,
            enqueue=self._enqueue_work_report,
            present=self._present_work_report,
            free=self._brain_loop_free_to_speak,
            offer_live=self._offer_live_work_report,
        )
        self._task_orchestrator.add_shutdown_callback(self._work_reports.shutdown)
        scheduler = getattr(self, "_idle_scheduler", None)
        from app.core.tasks.workflow.procedural_learning import ProceduralLearning

        self._work_methods = ProceduralLearning(
            self._chat_db, self._task_orchestrator, self._workflow_skill_registry
        )
        if scheduler is not None:
            scheduler.register(guide)
            scheduler.register(self._work_methods)
        self._work_reports.start()

    def _present_work_report(self, session, text, message_id, reaction):
        from app.core.conversation.delivery import current_delivery_id

        ledger = self.delivery_ledger()
        delivery = ledger.begin(session)
        if not delivery:
            return False
        token = current_delivery_id.set(delivery)
        try:
            ledger.finish(delivery, message_id)
            return self._proactive.present_work_report(session, text, message_id, reaction)
        finally:
            current_delivery_id.reset(token)

    def _work_speaking_context(self):
        guide = getattr(self, "_work_speech_guide", None)
        if guide is None:
            return {"revision": "unavailable"}
        window = guide.window() or 4096
        context = guide.context(max_tokens=max(256, min(1000, window // 12)))
        from app.core.concepts.concept_view import concept_view_from

        view = concept_view_from(self)
        concepts = view.for_consumer("work_speech") if view else []
        context["concepts"] = [
            {
                "id": c.concept_id,
                "kind": c.kind,
                "subject": c.subject,
                "label": c.label,
                "confidence": c.confidence,
            }
            for c in concepts
        ]
        context["revision"] = digest(
            [
                context["revision"],
                [
                    {k: row[k] for k in ("id", "kind", "subject", "label")}
                    for row in context["concepts"]
                ],
            ]
        )
        return context

    def _enqueue_work_report(self, session, report_id):
        self._brain_loop.enqueue(
            ProactiveEvent(
                session_key=session,
                source="work_report",
                parked_cue_ids=(str(report_id),),
            )
        )

    def _offer_live_work_report(self, session: str, report_id: int) -> bool:
        lock = getattr(self, "_live_work_report_lock", None)
        if lock is None or not self.is_live_presence():
            return False
        with lock:
            self._live_work_reports_ready[(session, report_id)] = None
        return True

    def _live_work_report_open(self, frame: LiveSituationFrame | None) -> bool:
        if (
            frame is None
            or frame.session_id != self.session_key
            or frame.mode_generation != self._live_mode_generation
            or frame.interaction.user_intent_sequence != self._live_user_intent_sequence
            or not self.is_live_presence()
            or not getattr(self, "_connected_clients", 0)
            or getattr(self._settings.agent, "live_quiet", False)
            or not self._live_unprompted_speech_enabled()
            or frame.constraints.speech_budget == "forbidden"
            or frame.constraints.dnd
            or frame.constraints.sleep_status in SLEEP_SPEECH_FORBID
            or frame.shared.os_idle == "locked"
            or main_wake_floor_busy(frame)
            or getattr(self, "_turn_in_progress", False)
            or self._live_tts_active()
            or not self.is_playback_drained()
        ):
            return False
        last = self._live_last_aiko_spoke_ms
        return last is None or (
            self._live_monotonic() * 1000.0 - last >= self._live_min_gap_after_speech_ms()
        )

    def _queue_live_work_report(self, frame: LiveSituationFrame) -> bool:
        lock = getattr(self, "_live_work_report_lock", None)
        if (
            lock is None or not self._live_work_reports_ready
            or not self._live_work_report_open(frame)
        ):
            return False
        with lock:
            ready = next(
                (item for item in self._live_work_reports_ready if item[0] == frame.session_id),
                None,
            )
            if ready is None:
                return False
            current = self._live_work_report_event
            if (
                current is not None and current.live_generation == frame.mode_generation
                and current.live_intent_sequence == frame.interaction.user_intent_sequence
                and current.session_key == frame.session_id
            ):
                return True
            session, report_id = ready
            event = ProactiveEvent(
                session_key=session,
                source="live_work_report",
                parked_cue_ids=(str(report_id),),
                live_generation=frame.mode_generation,
                live_intent_sequence=frame.interaction.user_intent_sequence,
            )
            self._live_work_report_event = event
        try:
            self._brain_loop.enqueue(event)
        except Exception:
            with lock:
                if self._live_work_report_event is event:
                    self._live_work_report_event = None
            log.debug("Live work report enqueue failed", exc_info=True)
            return False
        return True

    def _dispatch_work_report(self, event: ProactiveEvent) -> bool:
        reports = getattr(self, "_work_reports", None)
        if reports is None or len(event.parked_cue_ids) != 1:
            return False
        report_id = int(event.parked_cue_ids[0])
        session = event.session_key
        lock = getattr(self, "_live_work_report_lock", None)
        if event.source == "work_report":
            if (
                lock is not None and self.is_live_presence()
                and reports.is_requested_completion(session, report_id)
                and self._offer_live_work_report(session, report_id)
            ):
                return False  # A turn-based enqueue raced the change to Live.
            delivered = reports.deliver(session, report_id)
            if delivered and lock is not None:
                with lock:
                    self._live_work_reports_ready.pop((session, report_id), None)
            return delivered
        if lock is None:
            return False

        def still_open() -> bool:
            assembler = self._live_situation_assembler
            return (
                self._live_work_report_event is event
                and event.live_generation == self._live_mode_generation
                and event.live_intent_sequence == self._live_user_intent_sequence
                and session == self.session_key
                and self._live_work_report_open(assembler.last_frame)
            )

        with lock:
            if self._live_work_report_event is not event:
                return False
        delivered = False
        try:
            if not still_open():
                return False
            delivered = reports.deliver(session, report_id, can_deliver=still_open)
            if (
                delivered and session == self.session_key
                and event.live_generation == self._live_mode_generation
            ):
                self._live_mark_aiko_spoke()
            return delivered
        finally:
            keep_ready = not delivered and not still_open()
            with lock:
                if self._live_work_report_event is event:
                    # A closed floor keeps the hint. Invalid drafts go back to the outbox.
                    if not keep_ready:
                        self._live_work_reports_ready.pop((session, report_id), None)
                    self._live_work_report_event = None
