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
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Promise:
    """A single promise extracted from recent conversation."""

    who: str  # "user" | "assistant"
    text: str
    raw_match: str = ""
    source_turn_id: int | None = None
    source: str = "llm"  # "llm" | "self_tag"
    confidence: float = 0.5

    def to_memory_content(self, user_display_name: str = "Jacob") -> str:
        """Render to a natural-language memory string.

        ``user_display_name`` defaults to "Jacob" for back-compat with
        callers that don't pass a name; the worker threads the
        configured name through.
        """
        actor = (user_display_name or "the user") if self.who == "user" else "Aiko"
        # Prefix with the actor so "Aiko" promises don't read as the user's.
        return f"{actor} promised: {self.text.strip()}"


__all__ = ["Promise"]
