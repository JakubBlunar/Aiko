"""Privacy-capped Live labels for the 4B prompt. Never titles."""
from __future__ import annotations

from app.core.infra import timephrase

MAX_SUBJECT_CHARS = 40
_URL_MARKERS = ("http://", "https://", "www.")


def cap_live_subject(raw: object) -> str:
    """Return a short display subject, or empty if it is not prompt-safe.

    Cue subjects stay peek-only metadata. Relative deictics, URLs, and
    overlong window-title shapes are dropped rather than truncated into
    a misleading fragment.
    """
    text = " ".join(str(raw or "").replace("\n", " ").split()).strip()
    if not text:
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in _URL_MARKERS):
        return ""
    if "://" in text or "@" in text:
        return ""
    if timephrase.has_relative_deictic(text):
        return ""
    if len(text) > MAX_SUBJECT_CHARS:
        return ""
    return text


__all__ = ["MAX_SUBJECT_CHARS", "cap_live_subject"]
