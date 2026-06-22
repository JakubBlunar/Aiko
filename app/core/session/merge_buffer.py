"""Voice-merge buffer dataclass.

Extracted from :mod:`app.core.session.session_controller` into a leaf
module so the chat-turn / voice-capture / STT-partial mixins can all
import ``_MergeBuffer`` without importing the controller (which would be
a circular import — the controller imports those mixins via the package
``__init__``). This module imports only leaf dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.session.turn_runner import TurnRunner


@dataclass
class _MergeBuffer:
    """Per-session state that lets the next live phrase merge into the
    current in-flight LLM turn instead of bargeing in.

    Set when ``chat_once_streaming`` begins streaming a live-mode turn.
    Cleared on TTS start (window closes), on the merged-restart path,
    on barge-in (existing flow), on session change, and on shutdown.

    Locked via ``SessionController._merge_lock`` because it's read on the
    capture-loop thread (``feed_stt_partial`` early abort) and written on
    the chat thread (``chat_once_streaming`` TTS-start hook).
    """
    session_key: str
    turn_runner: TurnRunner
    user_text: str
    user_message_id: int
    tts_started: bool = False
    awaiting_phrase_b: bool = False
