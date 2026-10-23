"""Explicit provider/turn outcomes, independent of character preferences."""

from dataclasses import dataclass, field
import uuid

import requests


class ProviderOutcomeError(requests.RequestException):
    def __init__(self, message: str, *, outcome: str, finish_reason: str | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.finish_reason = finish_reason


@dataclass(slots=True)
class TurnOutcome:
    provider_id: str = "unknown"
    model: str = ""
    route_revision: str = ""
    status: str = "pending"
    finish_reason: str | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def learning_eligible(self) -> bool:
        return self.status == "completed"

    def finish(self, *, reason: str | None, cancelled: bool, has_answer: bool) -> None:
        self.finish_reason = reason
        self.status = (
            "cancelled" if cancelled or reason == "cancelled"
            else "truncated" if reason in {"length", "incomplete"}
            else "provider_blocked" if reason in {"content_filter", "refusal"}
            else "provider_error" if reason in {"error", "failed"}
            else "completed" if has_answer else "empty"
        )

    def fail(self, error: Exception) -> None:
        if isinstance(error, ProviderOutcomeError):
            self.status, self.finish_reason = error.outcome, error.finish_reason
        elif isinstance(error, requests.RequestException):
            self.status = "transport_error"
        else:
            self.status = "error"
