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
    nudge), *gain_db* (linear PCM offset), *pause_before / pause_after*
    in ms, and an optional *prefix* (a tiny interjection like "Hmm,"
    that is enqueued as a separate ultra-short chunk).
  * ``ProsodyDispatcher`` is the concrete glue. It owns the underlying
    ``enqueue`` callable and any RNG state, and exposes the same
    ``(text, reaction)`` shape that ``TurnRunner`` already calls.

Three orthogonal axes feed expressive speech:

  * **Reaction** -- mood label (``[[reaction:X]]`` or sentence-level
    derivation). Drives Live2D expression and a small reaction-to-speed
    multiplier on the TTS engine.
  * **Prosody** -- per-sentence vocal delivery
    (``[[prosody:whisper|soft|slow|fast|firm]]``). Layered on top of
    the reaction-derived ``ProsodyParams`` via ``_apply_prosody_overlay``
    so a sad sentence can still be whispered, an excited one can still
    be slow. Orthogonal to mood; applies to the single sentence that
    opens with the tag.
  * **Earcons** -- non-speech audio (``[[laugh]]``, ``[[soft_sigh]]``,
    ``[[breath]]``, ...) spliced into the spoken stream by
    ``TurnRunner._dispatch_chunk_with_earcons`` and the cadence
    auto-sprinkle rule (Layer 4) that prepends ``breath`` / ``soft_sigh``
    on opener-style sad sentences.

We still deliberately stay text-only at the dispatcher level — no SSML,
no ad-hoc engine knobs. Each axis maps onto something Pocket-TTS can
honor (speed, samplerate-only pitch shift, PCM gain, silent PCM frame,
earcon splice).
"""
from __future__ import annotations

import logging
import random
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic as _monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.affect.affect_state import AffectState
    from app.core.affect.circadian import CircadianState


log = logging.getLogger("app.cadence")


@dataclass(slots=True)
class ProsodyParams:
    """Per-chunk prosody hints. Pure values; the dispatcher applies them.

    The reaction / pause / prefix / speed_hint fields are the original
    Phase 5b shape. The Layer 1b / Layer 3 expressive-speech rollout
    adds:

      * ``gain_db`` -- linear dB offset applied to the Int16 PCM at
        emit time. Negative attenuates (whisper, soft, ambient
        noise compensation = 0); positive boosts (firm prosody).
      * ``prosody_label`` -- the originating ``[[prosody:X]]`` tag, if
        any. Logged for debugging; downstream consumers ignore it.
    """

    reaction: str = "neutral"
    pause_before_ms: int = 0
    pause_after_ms: int = 0
    prefix_text: str = ""
    prefix_reaction: str = ""
    speed_hint: float = 1.0
    gain_db: float = 0.0
    prosody_label: str = ""
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
    # Phase 4b: ambient-noise speed multiplier from
    # :class:`AmbientNoiseTracker`. 1.0 in quiet rooms, slightly
    # below 1.0 (down to 0.96) when the room is loud so listeners
    # have more time per word against the background.
    ambient_noise_speed: float = 1.0
    # Layer 1b: ambient-noise volume offset (dB). 0.0 in quiet rooms,
    # +0.8 / +1.5 in noisy / very-noisy rooms. Applied to the PCM
    # gain alongside any prosody-tag attenuation in
    # :meth:`ProsodyDispatcher._apply` so a noisy room boosts and a
    # ``[[prosody:whisper]]`` attenuates -- the two stack additively.
    ambient_volume_db_offset: float = 0.0
    rng: random.Random = field(default_factory=random.Random)


# Layer 3 overlay table: per-sentence ``[[prosody:LABEL]]`` to a small
# adjustment over the reaction-derived ``ProsodyParams``. Each entry is
# a ``(speed_mult, gain_db_delta, pause_before_ms)`` triple. The cadence
# layer keeps the reaction (mood label) intact -- only delivery axes
# move. Values are intentionally small so a misplaced tag never lands
# as a noticeable artefact; the persona instruction asks the LLM to
# emit one at most per turn for moments that matter.
_PROSODY_OVERLAYS: dict[str, tuple[float, float, int]] = {
    "whisper": (0.97, -6.0, 0),
    "soft":    (0.98, -3.0, 0),
    "slow":    (0.95,  0.0, 0),
    "fast":    (1.05,  0.0, 0),
    "firm":    (0.99, +2.0, 80),
}


def _apply_prosody_overlay(
    base: ProsodyParams,
    label: str | None,
) -> ProsodyParams:
    """Return ``base`` with the overlay for ``label`` folded in.

    Unknown / missing labels return ``base`` untouched. Speed
    overlays multiply the existing ``speed_hint``; gain overlays
    add to the existing ``gain_db``; pause overlays max with the
    existing ``pause_before_ms``. The overlay never lowers a
    pause that was already higher.
    """
    if not label:
        return base
    overlay = _PROSODY_OVERLAYS.get(label.strip().lower())
    if overlay is None:
        return base
    speed_mult, gain_delta, pause_before = overlay
    return ProsodyParams(
        reaction=base.reaction,
        pause_before_ms=max(int(base.pause_before_ms), int(pause_before)),
        pause_after_ms=int(base.pause_after_ms),
        prefix_text=base.prefix_text,
        prefix_reaction=base.prefix_reaction,
        speed_hint=float(round(base.speed_hint * speed_mult, 4)),
        gain_db=float(base.gain_db + gain_delta),
        prosody_label=str(label).strip().lower(),
        rationale=(
            f"{base.rationale} +prosody={label} "
            f"speed*={speed_mult} gain+={gain_delta:+.1f}dB"
        ).strip(),
    )


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
    # Phase 4b: ambient-noise nudge. A noisy room slows speech a hair
    # so the listener has more time per word; quiet rooms don't move.
    try:
        noise_mult = float(ctx.ambient_noise_speed)
    except (TypeError, ValueError):
        noise_mult = 1.0
    if 0.85 < noise_mult < 1.15:
        base *= noise_mult
    return float(round(base, 3))


def analyze_sentence(text: str, ctx: CadenceContext) -> ProsodyParams:
    """Pure analyzer: text + context -> ProsodyParams (no side effects).

    Layer 3: a leading ``[[prosody:LABEL]]`` tag on ``text`` is
    consumed here so the rest of the analyzer reasons about the
    spoken sentence only. The overlay is applied at the end of the
    function via :func:`_apply_prosody_overlay`. A tag that isn't at
    the very start is left for :func:`strip_all_meta_tags` to
    quietly drop -- middle-of-the-sentence tags don't drive prosody
    by design (mirrors the leading-tag idiom of
    :func:`parse_reaction_at_start`).
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ProsodyParams(reaction=ctx.base_reaction or "neutral")
    # Consume the leading prosody tag (if any) before the rest of
    # the analysis runs against the spoken text. Local import keeps
    # the cadence module from importing the response_text_service
    # at top level (avoids the historical circular-import landmine).
    from app.core.services.response_text_service import (
        consume_leading_prosody_tag,
    )

    prosody_label, cleaned = consume_leading_prosody_tag(cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return ProsodyParams(
            reaction=ctx.base_reaction or "neutral",
            prosody_label=prosody_label or "",
        )
    reaction = derive_sentence_reaction(cleaned, ctx.base_reaction)
    pause_before, pause_after = _pause_for_sentence(cleaned, ctx)
    prefix_text, prefix_reaction = _maybe_prefix(cleaned, ctx)
    speed = _speed_hint(reaction, ctx)
    # Layer 1b: ambient-noise gain compensation feeds straight through.
    # Prosody-tag attenuation (Layer 3) stacks additively on top of
    # this default at the dispatcher level.
    try:
        gain_db = float(ctx.ambient_volume_db_offset)
    except (TypeError, ValueError):
        gain_db = 0.0
    rationale = (
        f"reaction={reaction} pause_before={pause_before}ms "
        f"pause_after={pause_after}ms prefix={'yes' if prefix_text else 'no'} "
        f"speed={speed} gain_db={gain_db:+.2f}"
    )
    base = ProsodyParams(
        reaction=reaction,
        pause_before_ms=pause_before,
        pause_after_ms=pause_after,
        prefix_text=prefix_text,
        prefix_reaction=prefix_reaction,
        speed_hint=speed,
        gain_db=gain_db,
        rationale=rationale,
    )
    return _apply_prosody_overlay(base, prosody_label)


# ── dispatcher ──────────────────────────────────────────────────────────


# The dispatcher's ``_enqueue`` is structurally compatible with both
# ``TtsQueue.enqueue`` (which accepts an optional ``speed=`` kwarg) and
# legacy two-argument enqueue callables. We pass speed via try/TypeError
# so a caller can plug in either one.
EnqueueCallable = Callable[..., None]


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
        earcon_auto_sprinkle: bool = True,
    ) -> None:
        self._enqueue = enqueue
        self._rng = rng or random.Random()
        self._enabled = bool(enabled)
        self._context_provider: Callable[[], CadenceContext] | None = None
        # Layer 2: queue-side silence injector. Wired by
        # :class:`SessionController` to ``TtsQueue.enqueue_silence``.
        # Stays ``None`` in tests / legacy embeds; ``_enqueue_silence``
        # is a noop in that case so the dispatcher still produces the
        # same text + speed output as before.
        self._silence_provider: Callable[[int], None] | None = None
        # Layer 4: queue-side earcon injector and the auto-sprinkle
        # gate. ``_earcon_provider`` is wired by
        # :class:`SessionController` to ``TtsQueue.enqueue_earcon``
        # so cadence can prepend a ``breath`` or ``soft_sigh`` to
        # the first sentence of a melancholy / wistful / sad turn.
        # ``_auto_sprinkle`` is the user-facing on/off; cooldown
        # tracking keeps a long heart-to-heart from wheezing.
        self._earcon_provider: Callable[[str], None] | None = None
        self._auto_sprinkle = bool(earcon_auto_sprinkle)
        self._auto_sprinkle_last: float = 0.0
        self._lock = threading.Lock()
        self._stats = {
            "chunks": 0,
            "prefixes": 0,
            "reactions_changed": 0,
            "pauses_added": 0,
            "auto_earcons": 0,
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
        # Layer 3: drop the leading ``[[prosody:LABEL]]`` from the text
        # we're about to enqueue so the catch-all strip in
        # ``prepare_tts_text`` doesn't have to do it. Trailing /
        # mid-sentence tags still fall through to the catch-all.
        if params.prosody_label:
            from app.core.services.response_text_service import (
                consume_leading_prosody_tag,
            )

            _, cleaned = consume_leading_prosody_tag(cleaned)
            cleaned = cleaned.strip()
            if not cleaned:
                return
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
        # Layer 4: auto-sprinkle a soft breath / sigh ahead of the
        # spoken text on melancholy / wistful / sad / cry openers.
        # The earcon plays through the same queue so the timing is
        # naturally serial with the sentence that follows.
        self._maybe_auto_sprinkle(params)
        if params.prefix_text:
            # Prefix interjections ride at the carrier sentence's speed
            # so a "Hmm," doesn't accidentally land at neutral pace
            # ahead of a slowed thoughtful sentence.
            self._enqueue_with_speed(
                params.prefix_text,
                params.prefix_reaction or params.reaction,
                params.speed_hint,
            )
        # Layer 2: real timed pauses. ``_apply_text_pauses`` still
        # rewrites punctuation as a fallback (engines without an
        # ``enqueue_silence`` keep getting the same legacy treatment),
        # but if the underlying queue does support real silence we
        # also splice an actual silent gap on either side. The
        # cadence layer already caps ``pause_*_ms`` via heuristics in
        # :func:`_pause_for_sentence`; ``TtsQueue.enqueue_silence``
        # additionally clamps to ``_SILENCE_MAX_MS``.
        if params.pause_before_ms > 0:
            self._enqueue_silence(int(params.pause_before_ms))
        self._enqueue_with_speed(
            _apply_text_pauses(text, params),
            params.reaction,
            params.speed_hint,
            gain_db=params.gain_db,
        )
        if params.pause_after_ms > 0:
            self._enqueue_silence(int(params.pause_after_ms))

    def _enqueue_with_speed(
        self,
        text: str,
        reaction: str | None,
        speed: float,
        *,
        gain_db: float = 0.0,
    ) -> None:
        """Forward to the enqueue callable, opportunistically passing
        ``speed=`` (and the new ``gain_db=`` kwarg from Layer 1b /
        Layer 3). Legacy two-arg enqueues are tolerated via TypeError
        so this dispatcher works against bare ``TtsQueue.enqueue`` and
        any older shim callers may have wired in."""
        try:
            self._enqueue(
                text, reaction, speed=float(speed), gain_db=float(gain_db),
            )
            return
        except TypeError:
            pass
        try:
            self._enqueue(text, reaction, speed=float(speed))
            return
        except TypeError:
            pass
        self._enqueue(text, reaction)

    def set_earcon_provider(
        self, provider: Callable[[str], None] | None,
    ) -> None:
        """Layer 4: install the queue-side ``enqueue_earcon`` callable.

        The dispatcher uses it for auto-sprinkle (a ``breath`` or
        ``soft_sigh`` on the first sentence of a sad turn) only --
        the LLM's inline ``[[chuckle]]`` / ``[[breath]]`` / ... still
        ride the existing earcon path through
        :meth:`turn_runner.TurnRunner._dispatch_chunk_with_earcons`.
        """
        self._earcon_provider = provider

    def set_auto_sprinkle(self, enabled: bool) -> None:
        self._auto_sprinkle = bool(enabled)

    # Cooldown between auto-sprinkled earcons. 25 s is long enough
    # that a heart-to-heart conversation gets one breath cue per
    # opener but doesn't pile up on every sentence; short enough
    # that two separate sad moments in the same session both land.
    _AUTO_SPRINKLE_COOLDOWN_SEC: float = 25.0
    # Probability of actually firing the earcon when the gate is
    # open. ~0.30 keeps the cue feeling natural rather than
    # mechanical -- a friend doesn't sigh on every sad sentence.
    _AUTO_SPRINKLE_PROBABILITY: float = 0.30
    _AUTO_SPRINKLE_REACTIONS: tuple[str, ...] = (
        "sad", "melancholy", "wistful", "cry", "concerned",
    )

    def _maybe_auto_sprinkle(self, params: ProsodyParams) -> None:
        """Fire a soft breath/sigh earcon ahead of opener-style sad
        sentences when the gate, cooldown, and RNG say so.

        Skipped silently when:
          * the user disabled ``agent.earcon_auto_sprinkle``,
          * no earcon provider is wired (tests, TTS-disabled),
          * the sentence reaction isn't in the sad family,
          * we're inside the cooldown from the last auto-sprinkle,
          * the RNG flips heads (~30% fire rate).
        """
        if not self._auto_sprinkle:
            return
        provider = self._earcon_provider
        if provider is None:
            return
        if (params.reaction or "").strip().lower() not in self._AUTO_SPRINKLE_REACTIONS:
            return
        # Don't compete with a tag the LLM already emitted.
        if params.prosody_label or params.prefix_text:
            return
        now = _monotonic()
        if now - self._auto_sprinkle_last < self._AUTO_SPRINKLE_COOLDOWN_SEC:
            return
        if self._rng.random() >= self._AUTO_SPRINKLE_PROBABILITY:
            return
        # Pick the cue: soft_sigh on long pause-after sentences
        # (likely an opener of a heavier beat); breath otherwise.
        cue = "soft_sigh" if params.pause_after_ms >= 200 else "breath"
        try:
            provider(cue)
        except Exception:
            log.debug("auto-sprinkle earcon raised", exc_info=True)
            return
        with self._lock:
            self._stats["auto_earcons"] += 1
        self._auto_sprinkle_last = now

    def set_silence_provider(
        self, provider: Callable[[int], None] | None,
    ) -> None:
        """Layer 2: install the queue-side ``enqueue_silence`` callable.

        The dispatcher decouples from :class:`TtsQueue` so tests can
        plug a recorder; ``SessionController`` wires the real one. A
        ``None`` provider is a noop (legacy behaviour: no real timed
        pauses, only the punctuation rewrite from
        :func:`_apply_text_pauses`).
        """
        self._silence_provider = provider

    def _enqueue_silence(self, ms: int) -> None:
        provider = getattr(self, "_silence_provider", None)
        if provider is None:
            return
        try:
            provider(int(ms))
        except Exception:
            log.debug("silence provider raised", exc_info=True)


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
