"""Original answer identity carried across a potentially slow adjudicator call."""
from contextvars import ContextVar
from functools import wraps

from app.core.infra import timephrase


current_answer_event: ContextVar[dict | None] = ContextVar("answer_event", default=None)


def capture_answer_event(resolve):
    @wraps(resolve)
    def wrapped(self, *args, **kwargs):
        event = kwargs.pop("answer_event", None) or {
            "session_id": getattr(self, "session_key", None),
            "user_message_id": None,
            "observed_at": timephrase.utcnow().isoformat(),
        }
        token = current_answer_event.set(dict(event))
        try:
            return resolve(self, *args, **kwargs)
        finally:
            current_answer_event.reset(token)

    return wrapped
