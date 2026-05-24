"""Cadence: per-sentence prosody for the TTS pipeline (Phase 5b).

Until now the TTS pipeline shipped each sentence-sized chunk straight
into ``TtsQueue.enqueue(text, reaction)`` with the *carrier* reaction of
the whole turn. That works, but the result sounds metronomically even —
Aiko's voice doesn't change shape across a long reply.

This module sits between ``TurnRunner.on_tts_chunk`` and
``TtsQueue.enqueue``:

  * ``derive_sentence_reaction`` adjusts the carrier reaction based on
    sentence-level cues ("oh!" -> surprised, "hmm" -> thoughtful,
    trailing ellipsis -> wistful).
  * ``analyze_sentence`` produces a small :class:`ProsodyParams` record:
    *speed* (multiplier hint for the engine, also surfaced as a reaction
    nudge), *pause_after* in ms, and an optional *prefix* (a tiny
    interjection like "Hmm," that is enqueued as a separate ultra-short
    chunk).
  * ``ProsodyDispatcher`` is the concrete glue. It owns the underlying
    ``enqueue`` callable and any RNG state, and exposes the same
    ``(text, reaction)`` shape that ``TurnRunner`` already calls.

We deliberately stay text-only — no SSML, no ad-hoc engine knobs.
Backends decide how much to honor the reaction nudge; the structure is
identical even on engines that ignore everything.
"""
from __future__ import annotations

import logging
import random
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.affect_state import AffectState
    from app.core.circadian import CircadianState


log = logging.getLogger("app.cadence")


@dataclass(slots=True)
class ProsodyParams:
    """Per-chunk prosody hints. Pure values; the dispatcher applies them."""

    reaction: str = "neutral"
    pause_before_ms: int = 0
    pause_after_ms: int = 0
    prefix_text: str = ""
    prefix_reaction: str = ""
    speed_hint: float = 1.0
    rationale: str = ""


@dataclass(slots=True)
class CadenceContext:
    """Thin bag of inputs for cadence decisions (kept narrow on purpose)."""

    base_reaction: str = "neutral"
    mood_label: str = "content"
    mood_arousal: float = 0.4
    mood_valence: float = 0.0
    circadian_period: str = ""
    circadian_drowsy: bool = False
    rng: random.Random = field(default_factory=random.Random)


# ── per-sentence sentiment & shape ──────────────────────────────────────


_SURPRISE_RE = re.compile(
    r"(?:^|\s)(?:oh+|wait|whoa|huh|holy|wow|seriously)\b[!?]*",
    re.IGNORECASE,
)
_THOUGHT_RE = re.compile(
    r"(?:^|\s)(?:hmm+|hm+|let'?s\s+see|i\s+(?:think|wonder|guess)|maybe)\b",
    re.IGNORECASE,
)
_LAUGH_RE = re.compile(
    r"\b(?:lol|lmao|haha+|hehe+|tee?hee+)\b",
    re.IGNORECASE,
)
_SAD_RE = re.compile(
    r"\b(?:that'?s\s+(?:rough|hard|sad|too\s+bad)|i'?m\s+sorry|aww+)\b",
    re.IGNORECASE,
)
_QUESTION_TAIL_RE = re.compile(r"\?\s*$")
_EXCLAIM_RE = re.compile(r"!{1,3}\s*$")
_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)\s*$")
_COMMA_LIST_RE = re.compile(r",.*,")  # "a, b, c" pattern -> longer pause


def derive_sentence_reaction(text: str, base_reaction: str) -> str:
    """Pick a sentence-level reaction. Falls back to ``base_reaction``."""
    if not text:
        return base_reaction or "neutral"
    if _SURPRISE_RE.search(text) or _EXCLAIM_RE.search(text):
        return "surprised"
    if _LAUGH_RE.search(text):
        return "amused"
    if _SAD_RE.search(text):
        return "concerned"
    if _ELLIPSIS_RE.search(text):
        return "wistful"
    if _THOUGHT_RE.search(text):
        return "thoughtful"
    if _QUESTION_TAIL_RE.search(text):
        # Lean *slightly* curious if the carrier is bland.
        if (base_reaction or "neutral") in {"neutral", "calm"}:
            return "curious"
    return base_reaction or "neutral"


def _maybe_prefix(
    text: str,
    ctx: CadenceContext,
) -> tuple[str, str]:
    """Return ``(prefix_text, prefix_reaction)`` or ``("", "")`` to skip."""
    if not text:
        return "", ""
    rng = ctx.rng
    # Tired / late: occasional small "Mm." or "Yeah—".
    if ctx.circadian_drowsy or ctx.mood_label == "tired":
        if rng.random() < 0.10:
            return "Mm.", "calm"
    # Excited / playful: occasional "Oh!" lead-in.
    if ctx.mood_label in {"playful", "warm", "curious"} and ctx.mood_arousal > 0.6:
        if rng.random() < 0.08:
            return "Oh,", "amused"
    # Concerned / sad: gentle "Yeah,".
    if ctx.mood_valence < -0.25 and rng.random() < 0.08:
        return "Yeah,", "concerned"
    return "", ""


def _pause_for_sentence(text: str, ctx: CadenceContext) -> tuple[int, int]:
    """Return ``(pause_before_ms, pause_after_ms)`` based on shape + mood."""
    if not text:
        return 0, 0
    pause_after = 0
    pause_before = 0
    # Long sentences breathe more.
    n = len(text)
    if n > 140:
        pause_after = 220
    elif n > 80:
        pause_after = 140
    else:
        pause_after = 60
    if _ELLIPSIS_RE.search(text):
        pause_after = max(pause_after, 380)
    if _QUESTION_TAIL_RE.search(text):
        pause_after = max(pause_after, 260)
    if _COMMA_LIST_RE.search(text):
        pause_before = max(pause_before, 80)
    # Tired / drowsy adds drag; restless trims.
    if ctx.circadian_drowsy or ctx.mood_label == "tired":
        pause_after = int(pause_after * 1.3)
    elif ctx.mood_label in {"restless", "playful"} and ctx.mood_arousal > 0.7:
        pause_after = int(pause_after * 0.7)
    return int(pause_before), int(pause_after)


def _speed_hint(reaction: str, ctx: CadenceContext) -> float:
    """A small multiplier the engine *may* honour via reaction_to_speed."""
    base = 1.0
    if reaction in {"surprised", "amused"} and ctx.mood_arousal > 0.55:
        base *= 1.04
    if reaction in {"thoughtful", "wistful", "concerned"} or ctx.mood_label == "tired":
        base *= 0.95
    if ctx.circadian_drowsy:
        base *= 0.97
    return float(round(base, 3))


def analyze_sentence(text: str, ctx: CadenceContext) -> ProsodyParams:
    """Pure analyzer: text + context -> ProsodyParams (no side effects)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ProsodyParams(reaction=ctx.base_reaction or "neutral")
    reaction = derive_sentence_reaction(cleaned, ctx.base_reaction)
    pause_before, pause_after = _pause_for_sentence(cleaned, ctx)
    prefix_text, prefix_reaction = _maybe_prefix(cleaned, ctx)
    speed = _speed_hint(reaction, ctx)
    rationale = (
        f"reaction={reaction} pause_before={pause_before}ms "
        f"pause_after={pause_after}ms prefix={'yes' if prefix_text else 'no'} "
        f"speed={speed}"
    )
    return ProsodyParams(
        reaction=reaction,
        pause_before_ms=pause_before,
        pause_after_ms=pause_after,
        prefix_text=prefix_text,
        prefix_reaction=prefix_reaction,
        speed_hint=speed,
        rationale=rationale,
    )


# ── dispatcher ──────────────────────────────────────────────────────────


EnqueueCallable = Callable[[str, str | None], None]


class ProsodyDispatcher:
    """Wraps ``TtsQueue.enqueue`` with per-sentence prosody.

    Use :meth:`set_context_provider` to plug in a function that returns
    the current :class:`CadenceContext`. The dispatcher pulls a fresh
    context per chunk (so mid-turn affect/circadian changes are picked
    up). When a context provider isn't set the dispatcher behaves like a
    pass-through over the underlying ``enqueue``.
    """

    def __init__(
        self,
        enqueue: EnqueueCallable,
        *,
        rng: random.Random | None = None,
        enabled: bool = True,
    ) -> None:
        self._enqueue = enqueue
        self._rng = rng or random.Random()
        self._enabled = bool(enabled)
        self._context_provider: Callable[[], CadenceContext] | None = None
        self._lock = threading.Lock()
        self._stats = {
            "chunks": 0,
            "prefixes": 0,
            "reactions_changed": 0,
            "pauses_added": 0,
        }

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def set_context_provider(
        self, provider: Callable[[], CadenceContext] | None,
    ) -> None:
        self._context_provider = provider

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def dispatch(self, text: str, reaction: str | None = None) -> None:
        """Apply prosody to ``text`` and forward to the underlying enqueue."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if not self._enabled:
            self._enqueue(cleaned, reaction)
            return
        ctx = self._build_context(reaction or "neutral")
        params = analyze_sentence(cleaned, ctx)
        self._apply(cleaned, reaction or "neutral", params)

    def analyze(self, text: str, reaction: str = "neutral") -> ProsodyParams:
        """Pure helper for tests / introspection — same path as ``dispatch``."""
        ctx = self._build_context(reaction)
        return analyze_sentence(text, ctx)

    # ── internal ───────────────────────────────────────────────────────

    def _build_context(self, base_reaction: str) -> CadenceContext:
        provider = self._context_provider
        ctx: CadenceContext
        if provider is None:
            ctx = CadenceContext(base_reaction=base_reaction, rng=self._rng)
        else:
            try:
                ctx = provider()
                ctx.base_reaction = base_reaction
                if ctx.rng is None:  # type: ignore[truthy-bool]
                    ctx.rng = self._rng
            except Exception:
                log.debug("cadence context provider failed", exc_info=True)
                ctx = CadenceContext(base_reaction=base_reaction, rng=self._rng)
        return ctx

    def _apply(
        self,
        text: str,
        original_reaction: str,
        params: ProsodyParams,
    ) -> None:
        with self._lock:
            self._stats["chunks"] += 1
            if params.prefix_text:
                self._stats["prefixes"] += 1
            if params.reaction != original_reaction:
                self._stats["reactions_changed"] += 1
            if params.pause_after_ms or params.pause_before_ms:
                self._stats["pauses_added"] += 1
        if params.prefix_text:
            self._enqueue(params.prefix_text, params.prefix_reaction or params.reaction)
        # The carrier sentence with its (possibly adjusted) reaction.
        self._enqueue(_apply_text_pauses(text, params), params.reaction)


_TRAILING_PUNCT_RE = re.compile(r"[\s\.\?!\,;:]+\Z")


def _apply_text_pauses(text: str, params: ProsodyParams) -> str:
    """Rewrite the text with low-impact comma/ellipsis hints.

    We avoid SSML and just nudge punctuation that the synth already
    interprets as pauses. Engines that ignore punctuation are unaffected.
    """
    cleaned = text.rstrip()
    if params.pause_after_ms >= 350 and not cleaned.endswith(("…", "...", "?", "!")):
        cleaned = cleaned.rstrip(",.;:") + "…"
    elif params.pause_after_ms >= 200 and not cleaned.endswith((".", "?", "!", "…", "...")):
        cleaned = cleaned + "."
    if params.pause_before_ms >= 100 and cleaned and not cleaned.startswith(("…", "—")):
        cleaned = "— " + cleaned
    return cleaned


__all__ = [
    "CadenceContext",
    "ProsodyDispatcher",
    "ProsodyParams",
    "analyze_sentence",
    "derive_sentence_reaction",
]
