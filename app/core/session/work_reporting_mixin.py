"""Session wiring for local speech learning and durable task reporting."""

from app.core.persona.speech_guide import SpeechGuide, digest
from app.core.tasks.work_reports import WorkReports


class WorkReportingMixin:
    def _init_work_reporting(self):
        if getattr(self, "_workflow_skill_registry", None) is None:
            return
        if getattr(self, "_work_reports", None) is not None:
            return
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
        from app.core.brain.events import ProactiveEvent

        self._brain_loop.enqueue(
            ProactiveEvent(
                session_key=session,
                source="work_report",
                parked_cue_ids=(str(report_id),),
            )
        )
