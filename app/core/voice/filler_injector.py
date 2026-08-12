"""Filler injection for slow-first-token turns (Phase 1c).

If the LLM takes more than a configurable threshold (default 800ms) to
emit its first streaming delta, we route a short filler phrase through
the TTS callback so Aiko makes a sound — "Hmm,", "Let me think," — while
the model is still warming up.

:func:`pick_lookup_filler` and :func:`pick_looking_filler` serve the same
TTS callback for a different kind of wait: a few seconds of real work the
turn is blocked on — D3's synchronous web search, or H25's local vision
pass over a shared image. Both are spoken deliberately rather than on a
watchdog, since the wait is known before it starts.

Filler is intentionally short and conversational; it's queued ahead of
the real reply so the user hears overlap-free continuity. Once the first
real token lands the watchdog is cancelled. If the filler fired, the
real reply will simply follow it; if it didn't, nothing changes.

Picked phrases are biased on the carry-over reaction from the previous
turn so the texture matches Aiko's recent emotional state — a "playful"
last turn pulls more spirited fillers, "thoughtful" pulls slower ones.
"""
from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable

log = logging.getLogger("app.filler_injector")


# Curated by tone. Each list is short, conversational, and ends with a
# soft pause (comma or no terminator) so the real reply can flow into it.
_FILLERS_BY_TONE: dict[str, tuple[str, ...]] = {
    "playful": (
        "Ooh,",
        "Hmm, let me see —",
        "Okay, okay,",
    ),
    "warm": (
        "Mhm,",
        "Mm, let me think,",
        "Ah,",
    ),
    "thoughtful": (
        "Hmm,",
        "Let me think,",
        "Mm, just a sec,",
        "Hmm, give me a moment,",
    ),
    "concerned": (
        "Hmm,",
        "Let me think about that,",
    ),
    "curious": (
        "Oh,",
        "Hmm,",
        "Interesting —",
    ),
    "neutral": (
        "Hmm,",
        "Let me think,",
        "One sec,",
    ),
}

# Map TurnRunner reactions / mood labels onto a filler tone bucket.
_REACTION_TO_TONE: dict[str, str] = {
    "cheerful": "playful",
    "excited": "playful",
    "playful": "playful",
    "warm": "warm",
    "tender": "warm",
    "friendly": "warm",
    "thoughtful": "thoughtful",
    "calm": "thoughtful",
    "focused": "thoughtful",
    "concerned": "concerned",
    "sad": "concerned",
    "melancholy": "concerned",
    "cry": "concerned",
    "tired": "concerned",
    "curious": "curious",
    "surprised": "curious",
    "neutral": "neutral",
}


# D3: spoken while a slow brain tool (the synchronous web search) is
# mid-flight. Different job from the generic filler above — that one
# covers a model that is merely warming up, this one covers a few seconds
# of real work — so it says what she is doing rather than just making a
# sound, and ends closed rather than trailing off, because the reply that
# follows it starts a fresh sentence.
_LOOKUP_FILLERS: dict[str, tuple[str, ...]] = {
    "playful": (
        "Ooh, hang on — let me look that up.",
        "One sec, checking.",
    ),
    "warm": (
        "Mm, let me check that for you.",
        "Hang on, I'll look it up.",
    ),
    "thoughtful": (
        "Hang on, let me look that up.",
        "Let me check rather than guess.",
    ),
    "concerned": (
        "Let me check that properly.",
        "Hang on, I want to get this right.",
    ),
    "curious": (
        "Ooh, let me look that up.",
        "Hang on, I want to check.",
    ),
    "neutral": (
        "Hang on, let me check.",
        "One sec, let me look that up.",
    ),
}


# H25: spoken while the local vision model is looking at an image the
# user just shared. Distinct from the lookup fillers because the beat is
# social rather than clerical — someone has held something up, and the
# natural noise is leaning in, not filing a query. Kept eager (no "let me
# check whether") so the pause reads as interest rather than doubt.
_LOOKING_FILLERS: dict[str, tuple[str, ...]] = {
    "playful": (
        "Ooh, let me look!",
        "Oh, hang on — I want to see this properly.",
    ),
    "warm": (
        "Oh, let me look at this.",
        "Mm, hang on — looking now.",
    ),
    "thoughtful": (
        "Let me have a proper look.",
        "Hang on, I'm looking.",
    ),
    "concerned": (
        "Let me look at it properly.",
        "Hang on — I want to see this.",
    ),
    "curious": (
        "Ooh, let me see.",
        "Oh — hang on, let me look.",
    ),
    "neutral": (
        "Hang on, let me look.",
        "One sec — looking at it now.",
    ),
}


def pick_filler(reaction: str | None) -> tuple[str, str]:
    """Return ``(phrase, reaction_for_tts)`` for the given carry-over tone."""
    tone = _REACTION_TO_TONE.get((reaction or "").lower(), "neutral")
    candidates = _FILLERS_BY_TONE.get(tone) or _FILLERS_BY_TONE["neutral"]
    return random.choice(candidates), reaction or "thoughtful"


def pick_lookup_filler(reaction: str | None) -> tuple[str, str]:
    """Return ``(phrase, reaction_for_tts)`` for a slow-tool announcement."""
    tone = _REACTION_TO_TONE.get((reaction or "").lower(), "neutral")
    candidates = _LOOKUP_FILLERS.get(tone) or _LOOKUP_FILLERS["neutral"]
    return random.choice(candidates), reaction or "thoughtful"


def pick_looking_filler(reaction: str | None) -> tuple[str, str]:
    """Return ``(phrase, reaction_for_tts)`` for the H25 vision pause."""
    tone = _REACTION_TO_TONE.get((reaction or "").lower(), "neutral")
    candidates = _LOOKING_FILLERS.get(tone) or _LOOKING_FILLERS["neutral"]
    return random.choice(candidates), reaction or "curious"


class FillerInjector:
    """Threaded watchdog that emits a filler if first-token is slow.

    Single-shot per turn. The owner calls :meth:`arm` after building the
    prompt and just before streaming starts, then :meth:`disarm` on the
    very first stream delta. Disarming after the timer fired is a no-op
    — the filler is already in the TTS queue.
    """

    def __init__(
        self,
        *,
        threshold_ms: int = 800,
        enabled: bool = True,
    ) -> None:
        self._threshold_s = max(0.05, threshold_ms / 1000.0)
        self._enabled = bool(enabled)
        self._timer: threading.Timer | None = None
        self._fired = False
        self._lock = threading.Lock()

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    def update_runtime(
        self,
        *,
        threshold_ms: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        if threshold_ms is not None:
            self._threshold_s = max(0.05, int(threshold_ms) / 1000.0)
        if enabled is not None:
            self._enabled = bool(enabled)

    def arm(
        self,
        on_tts_chunk: Callable[[str, str], None] | None,
        *,
        carry_over_reaction: str | None,
    ) -> None:
        """Start the watchdog. Cancels any prior armed timer first."""
        with self._lock:
            self._fired = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._enabled:
                return
            if on_tts_chunk is None:
                return
            phrase, reaction = pick_filler(carry_over_reaction)

            def _fire() -> None:
                with self._lock:
                    if self._fired:
                        return
                    # Mark fired *before* the callback so disarm() races safely.
                    self._fired = True
                try:
                    on_tts_chunk(phrase, reaction)
                except Exception:
                    log.debug("filler tts emit failed", exc_info=True)

            timer = threading.Timer(self._threshold_s, _fire)
            timer.daemon = True
            timer.start()
            self._timer = timer

    def disarm(self) -> bool:
        """Cancel the watchdog. Returns whether the filler had already fired."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            return self._fired


__all__ = [
    "FillerInjector",
    "pick_filler",
    "pick_looking_filler",
    "pick_lookup_filler",
]
