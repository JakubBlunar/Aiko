"""Public, owner-scoped archive browsing and explicit generation requests."""

from __future__ import annotations

import json

from app.core.conversation.archive_review import ArchiveReview
from app.llm.tools.conversation_history import ConversationHistoryTool


class ArchiveMixin:
    def archive_generation_available(self) -> bool:
        return bool(
            getattr(self, "_idle_scheduler", None) is not None
            and getattr(self, "_chat_db", None) is not None
            and self._settings.tools.enabled
            and self._settings.tools.daily_overviews
        )

    def read_conversation_archive(self, arguments: dict) -> dict:
        result = json.loads(
            ConversationHistoryTool(
                self._chat_db,
                self.user_id,
                self._settings.tools.archive_timezone,
            ).run(arguments)
        )
        result["generation_available"] = self.archive_generation_available()
        result["archive_timezone"] = self._settings.tools.archive_timezone
        if (
            arguments.get("mode", "overviews") == "overviews"
            and result["status"] != "store_unavailable"
        ):
            result["jobs"] = ArchiveReview(self._chat_db, self.user_id).jobs()
        return result

    def request_archive_summary(self, day: str, timezone: str = "", *, cancel=False) -> dict:
        review = ArchiveReview(self._chat_db, self.user_id)
        zone = timezone or self._settings.tools.archive_timezone
        if cancel:
            review.remove(day, zone)
            return {"status": "cancelled"}
        if not self.archive_generation_available():
            raise RuntimeError(
                "Enable tools and daily overviews with the idle scheduler to generate summaries."
            )
        return review.enqueue(day, zone)
