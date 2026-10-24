"""Compute-only D8 daily outline builder: one recent source day per idle tick."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from app.core.conversation.archive import (
    GENERATOR_VERSION,
    OWNER_SQL,
    ConversationArchive,
    resolve_range,
)
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal


class DailyOverviewWorker:
    name = "daily_overviews"
    interval_seconds = 600.0
    sleep_policy = "continue"

    def __init__(
        self, db, user_id: str, *, enabled: Callable[[], bool], timezone_provider: Callable[[], str]
    ) -> None:
        self.db = db
        self.user_id = user_id
        self.enabled = enabled
        self.timezone_provider = timezone_provider

    def is_ready(self, *, now, last_run_at) -> bool:
        return bool(self.enabled())

    def pending_day(self) -> tuple[str, str] | None:
        tz_name = self.timezone_provider() or "system"
        today = resolve_range(when="today", tz_name=tz_name)["local_start"][:10]
        for age in range(7):
            day = (date.fromisoformat(today) - timedelta(days=age)).isoformat()
            span = resolve_range(start=day, end=day, tz_name=tz_name)
            cached = self.db.execute_fetchone(
                "SELECT start_at, end_at FROM conversation_daily_overviews "
                "WHERE user_id=? AND timezone=? AND local_day=? AND generator_version=?",
                (self.user_id, tz_name, day, GENERATOR_VERSION),
            )
            if cached == (span["start"], span["end_exclusive"]):
                continue
            source = self.db.execute_fetchone(
                f"SELECT 1 FROM messages WHERE {OWNER_SQL} AND instr(session_id, ':')>0 "
                "AND created_at>=? AND created_at<? AND role IN ('user', 'assistant') LIMIT 1",
                (self.user_id, span["start"], span["end_exclusive"]),
            )
            if source:
                return day, tz_name
        return None

    def demand(self, *, now, last_run_at) -> WorkSignal:
        pending = self.pending_day()
        return WorkSignal(
            0.5 if pending else 0.0, "unindexed_recent_day" if pending else "caught_up"
        )

    def run(self) -> dict:
        if not self.enabled():
            return {"status": "disabled"}
        pending = self.pending_day()
        if not pending:
            return {"status": "caught_up"}
        result = ConversationArchive(self.db, self.user_id).build_day(*pending)
        # Scheduler records contain counters, never copies of private excerpts.
        return {
            "status": result["status"],
            "local_day": pending[0],
            "at": timephrase.utcnow().isoformat(),
        }
