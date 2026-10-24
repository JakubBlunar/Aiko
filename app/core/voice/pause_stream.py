"""Retain only the pause-only delivery cue on the speech copy of a reply.

Display/transcript cleanup stays unchanged. Protecting the complete cue
before the shared stripper also keeps pauses inside hidden memory/detail
blocks hidden. Partial tags still use the normal streaming holdback.
"""
from __future__ import annotations

import re

from app.core.services.response_text_service import safe_visible_prefix, strip_all_meta_tags


_PAUSE_TAG = re.compile(r"\[\[prosody:[ \t]*pause[ \t]*\]\]", re.IGNORECASE)


def speech_prefix_with_pauses(text: str, *, streaming: bool = True) -> str:
    # Choose a marker absent from this input, so literal user/model text
    # cannot accidentally become a delivery instruction on restoration.
    marker = "\ue105"
    while marker in text:
        marker += "\ue105"
    # One explicit beat per reply; extra cues use ordinary metadata cleanup.
    protected = _PAUSE_TAG.sub(marker, text, count=1)
    cleaned = safe_visible_prefix(protected) if streaming else strip_all_meta_tags(protected)
    return cleaned.replace(marker, "[[prosody:pause]]")
