"""One explicitly requested daily summary per idle LLM admission."""

from __future__ import annotations

from app.core.conversation.archive_review import ArchiveReview
from app.core.proactive.idle_worker import WorkSignal


class ArchiveSummaryWorker:
    name = "archive_summaries"
    interval_seconds = 30.0
    sleep_policy = "continue"

    def __init__(self, db, user_id, *, enabled, client, model_context):
        self.review = ArchiveReview(db, user_id)
        self.enabled, self.client, self.model_context = enabled, client, model_context

    def is_ready(self, *, now, last_run_at):
        return bool(self.enabled())

    def demand(self, *, now, last_run_at):
        pending = self.enabled() and self.review.pending() is not None
        return WorkSignal(
            0.8 if pending else 0.0,
            "requested_day" if pending else "caught_up",
            needs_llm=bool(pending),
        )

    def run(self):
        if not self.enabled():
            return {"status": "disabled"}
        model, context = self.model_context()
        return self.review.run_one(self.client(), model, context)


def register_archive_workers(session):
    """Resolve client/model lazily so provider changes apply without stale routes."""
    from app.core.proactive.daily_overview_worker import DailyOverviewWorker

    def enabled():
        return bool(session._settings.tools.enabled and session._settings.tools.daily_overviews)

    session._idle_scheduler.register(
        DailyOverviewWorker(
            session._chat_db,
            session._user_id,
            enabled=enabled,
            timezone_provider=lambda: session._settings.tools.archive_timezone,
        )
    )
    session._idle_scheduler.register(
        ArchiveSummaryWorker(
            session._chat_db,
            session._user_id,
            enabled=enabled,
            client=lambda: session._maintenance_client,
            model_context=session._worker_route_model_ctx,
        )
    )
