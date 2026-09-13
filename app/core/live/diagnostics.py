"""Privacy-safe Live diagnostic dumps. No titles, transcripts, or journal bodies."""
from __future__ import annotations

from typing import Any


_DROP_KEYS = frozenset({
    "title",
    "text",
    "draft",
    "audio",
    "pcm",
    "wav",
    "pointer",
    "cursor",
    "transcript",
    "situation_summary",
    "raw_preview",
})


def sanitize_live_dump(value: Any) -> Any:
    """Strip private payload fields while keeping counts, intents, app names."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            token = str(key)
            if token in _DROP_KEYS:
                continue
            out[token] = sanitize_live_dump(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_live_dump(item) for item in value]
    return value
