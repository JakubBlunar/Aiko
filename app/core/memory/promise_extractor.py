"""Promise dataclass (Phase 3c, reworked).

Promise extraction now lives in the context-aware
:class:`app.core.memory.promise_worker.PromiseExtractionWorker` idle
worker, which reads the last few turns for context and asks the worker
LLM for self-contained promises. The old two-track design (post-turn
regex + speaking-window LLM) was retired because the regex captured
bare verb fragments with no context ("Jacob promised: never know").

This module is now just the :class:`Promise` value object + its
``to_memory_content`` renderer, shared by the worker and the promise
lifecycle helpers.

``text`` holds the bare action and the deadline rides its own fields
(H41). It used to be folded into ``text`` as a ``(by …)`` suffix before
the promise reached this class, which cost twice: the raw date tokens
joined the body's content-word fingerprint and made two takes on the
same commitment look less alike to the dedupe, and whatever register the
model happened to answer in was what got stored -- including a literal
``tomorrow``, which re-anchors to whenever it is next read.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Promise:
    """A single promise extracted from recent conversation."""

    who: str  # "user" | "assistant"
    text: str
    raw_match: str = ""
    source_turn_id: int | None = None
    source: str = "llm"  # "llm" | "self_tag"
    confidence: float = 0.5
    #: When it is owed by, if the transcript said. The machine-readable
    #: half: this is what reaches ``metadata.promise_deadline`` and what
    #: overdue is decided from.
    deadline: datetime | None = None
    #: How the deadline is written into the stored sentence. Absolute and
    #: weekday-bearing, so a reader neither has to do calendar arithmetic
    #: nor trust a relative word written days ago.
    deadline_text: str = ""

    def to_memory_content(self, user_display_name: str = "Jacob") -> str:
        """Render to a natural-language memory string.

        ``user_display_name`` defaults to "Jacob" for back-compat with
        callers that don't pass a name; the worker threads the
        configured name through.
        """
        actor = (user_display_name or "the user") if self.who == "user" else "Aiko"
        body = self.text.strip()
        if self.deadline_text:
            body = f"{body} (by {self.deadline_text})"
        # Prefix with the actor so "Aiko" promises don't read as the user's.
        return f"{actor} promised: {body}"


__all__ = ["Promise"]
