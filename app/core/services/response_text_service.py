from __future__ import annotations

import dataclasses
import re


# Reaction grammar: ``[[reaction:NAME]]`` for a single reaction, or
# ``[[reaction:A+B]]`` for a *stack* — the renderer takes the first
# token as the primary persistent reaction and treats subsequent
# tokens as sustained companion overlays (blush + grin, etc.). The
# character class allows the standard word chars plus ``+`` so the
# parser still rejects ``[[reaction:hello world]]`` and other
# garbage tokens. Stacks longer than two entries are accepted by the
# regex but the persona discourages them — see
# ``data/persona/aiko_companion.txt`` for the idiom.
_REACTION_TAG_PATTERN = re.compile(
    r"\[\[reaction:([\w+]+)\]\]",
    flags=re.IGNORECASE,
)
_REACTION_AT_START_PATTERN = re.compile(
    r"^\s*\[\[reaction:([\w+]+)\]\]\s*\n*",
    flags=re.IGNORECASE | re.MULTILINE,
)
_ACTION_META_LINE_PATTERN = re.compile(
    r"^(\[plan\]|\[action\]|system:\s*step\s+\d+|step\s+\d+\s*\(|awaiting confirmation)",
    flags=re.IGNORECASE,
)
_INLINE_ACTION_META_PATTERN = re.compile(
    r"\s*\[(plan|action|note)\].*$",
    flags=re.IGNORECASE,
)


def parse_reaction_at_start(text: str) -> tuple[str | None, str]:
    """If text starts with [[reaction:X]], return (X, rest). Otherwise (None, text).
    Use for streaming: call with accumulated buffer; when tag is complete, strip it and use rest for TTS.

    Stack form ``[[reaction:A+B]]``: only the **primary** (first)
    component is returned as the reaction name so every existing
    consumer (TTS, affect updater, mood broadcast) keeps working
    against a single-token mood string. Callers that care about the
    full stack (e.g. to fire companion overlays) should use
    :func:`parse_reaction_stack_at_start` instead — it returns the
    same primary plus the list of stacked companions.
    """
    primary, _companions, rest = parse_reaction_stack_at_start(text)
    return primary, rest


def parse_reaction_stack_at_start(
    text: str,
) -> tuple[str | None, list[str], str]:
    """Variant of :func:`parse_reaction_at_start` that surfaces the full stack.

    Returns ``(primary, companions, rest)`` where:
      - ``primary`` is the first component (or ``None`` when no tag),
      - ``companions`` is the ordered, deduped list of remaining
        components (empty for a plain ``[[reaction:X]]``),
      - ``rest`` is the input text with the leading tag stripped
        (identical to the second element of
        :func:`parse_reaction_at_start`).

    The dispatch boundary in :mod:`app.core.session.turn_runner` uses
    ``companions`` to fire long-duration overlay pulses on top of the
    persistent reaction — see the Phase 3 entry in
    ``docs/alexia-model-notes.md`` §3 / persona stack idiom.
    """
    source = str(text or "")
    match = _REACTION_AT_START_PATTERN.match(source)
    if not match:
        return None, [], source
    # Local import avoids a circular dep between
    # ``response_text_service`` and ``reactions`` at module load.
    from app.core.affect.reactions import split_reaction_stack
    components = split_reaction_stack(match.group(1))
    rest = source[match.end() :].lstrip("\n")
    if not components:
        return None, [], rest
    primary, *companions = components
    return primary, companions, rest


def extract_tts_reaction_tag(text: str) -> tuple[str | None, str]:
    source = str(text or "")
    matches = list(_REACTION_TAG_PATTERN.finditer(source))
    if not matches:
        return None, source

    reaction = matches[-1].group(1).strip().lower()
    cleaned = _REACTION_TAG_PATTERN.sub("", source)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return reaction, cleaned


def strip_all_reaction_tags(text: str) -> str:
    """Remove all [[reaction:X]] tags from text. Use for display/streaming so UI never shows tags."""
    return _REACTION_TAG_PATTERN.sub("", str(text or ""))


def strip_action_meta_for_tts(text: str) -> str:
    source = str(text or "")
    if not source:
        return ""

    cleaned_lines: list[str] = []
    skip_plan_block = False
    for raw_line in source.splitlines():
        stripped_inline = _INLINE_ACTION_META_PATTERN.sub("", raw_line)
        line = stripped_inline.strip()
        lowered = line.lower()

        if not line:
            if not skip_plan_block:
                cleaned_lines.append("")
            continue

        if lowered.startswith("[plan]"):
            skip_plan_block = True
            continue

        if skip_plan_block:
            # Plan blocks are line-based; stop skipping on the next non-plan section.
            if lowered.startswith("[action]") or lowered.startswith("[note]"):
                skip_plan_block = False
            else:
                continue

        if _ACTION_META_LINE_PATTERN.match(line):
            continue

        cleaned_lines.append(stripped_inline)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# Two-tier reply: [[spoken]]...[[/spoken]] and optional [[detail]]...[[/detail]]
_SPOKEN_OPEN = "[[spoken]]"
_SPOKEN_CLOSE = "[[/spoken]]"
_DETAIL_OPEN = "[[detail]]"
_DETAIL_CLOSE = "[[/detail]]"


def parse_two_tier_reply(raw: str) -> tuple[str, str]:
    """Split reply into (spoken_part, full_for_display). If no [[spoken]] block, whole reply is spoken.
    full_for_display has tags stripped so it can be shown (and optionally markdown-rendered) in the transcript."""
    source = str(raw or "").strip()
    if not source:
        return "", ""

    # Find first [[spoken]] and [[/spoken]]
    so = source.find(_SPOKEN_OPEN)
    sc = source.find(_SPOKEN_CLOSE) if so >= 0 else -1

    if so >= 0 and sc > so:
        spoken_part = source[so + len(_SPOKEN_OPEN) : sc].strip()
        # Build full_for_display: strip tags but keep content (spoken + detail)
        before = source[:so].strip()
        after = source[sc + len(_SPOKEN_CLOSE) :].strip()
        # Remove [[detail]]...[[/detail]] from spoken_part (already done - spoken is between spoken tags)
        # full_for_display = before (e.g. reaction) + spoken content + after (may contain detail block)
        parts_for_display = []
        if before:
            parts_for_display.append(_strip_two_tier_tags(before))
        parts_for_display.append(spoken_part)
        if after:
            parts_for_display.append(_strip_two_tier_tags(after))
        full_for_display = "\n\n".join(p for p in parts_for_display if p).strip()
    elif so >= 0 and sc < 0:
        # [[spoken]] without closing: treat from [[spoken]] to end as spoken
        spoken_part = source[so + len(_SPOKEN_OPEN) :].strip()
        full_for_display = _strip_two_tier_tags(source)
    else:
        spoken_part = source
        full_for_display = _strip_two_tier_tags(source)

    return spoken_part, full_for_display


def _strip_two_tier_tags(text: str) -> str:
    """Remove [[spoken]]/[[/spoken]], [[detail]]/[[/detail]], and [[reaction:X]] tags but keep their content."""
    s = str(text or "")
    for tag in (_SPOKEN_OPEN, _SPOKEN_CLOSE, _DETAIL_OPEN, _DETAIL_CLOSE):
        s = s.replace(tag, "")
    s = _REACTION_TAG_PATTERN.sub("", s)
    return s.strip()


_DETAIL_BLOCK_PATTERN = re.compile(
    r"\[\[detail\]\][\s\S]*?(?:\[\[/detail\]\]|\Z)",
    flags=re.IGNORECASE,
)
_REMEMBER_TAG_PATTERN = re.compile(
    r"\[\[remember:[^\]]*?\]\]",
    flags=re.IGNORECASE,
)
# Matches an unclosed [[remember:... at end of string (the closing ]] never arrived).
_REMEMBER_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[remember:[^\]]*\Z",
    flags=re.IGNORECASE,
)

# Phase 3c: self-correction grammar.
#   [[correct]]old text[[/correct]]new text
# Behaviour:
#   - TTS says ONLY the ``new text`` (the old text was a slip).
#   - Chat UI keeps the structured marker so it can render the old text
#     with strikethrough and the new text as a replacement (the
#     stripped-down text-only fallback also keeps just the new text).
#   - A ``tsk`` earcon plays at the correction boundary (handled by
#     :class:`TurnRunner` on top of the existing earcon plumbing).
_CORRECTION_OPEN = "[[correct]]"
_CORRECTION_CLOSE = "[[/correct]]"
_CORRECTION_BLOCK_PATTERN = re.compile(
    r"\[\[correct\]\]([\s\S]*?)\[\[/correct\]\]",
    flags=re.IGNORECASE,
)
# Matches an unclosed [[correct]]... at end of string. Used by
# :func:`safe_visible_prefix` to hold back the in-progress correction.
_CORRECTION_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[correct\]\][\s\S]*\Z",
    flags=re.IGNORECASE,
)


def extract_corrections(text: str) -> list[tuple[str, int]]:
    """Return ``(old_text, end_offset)`` pairs for every fully-formed
    ``[[correct]]old[[/correct]]`` block in ``text``. ``end_offset`` is
    the character position **just after** ``[[/correct]]`` in the
    original string — useful for splicing in a ``tsk`` earcon at the
    boundary.
    """
    source = str(text or "")
    out: list[tuple[str, int]] = []
    for match in _CORRECTION_BLOCK_PATTERN.finditer(source):
        out.append((match.group(1), match.end()))
    return out


def strip_correction_for_tts(text: str) -> str:
    """Drop the ``old`` text from every correction block so TTS only
    speaks the corrected version. Surrounding whitespace is preserved
    so we don't merge two sentences accidentally.
    """
    source = str(text or "")
    if not source:
        return source
    return _CORRECTION_BLOCK_PATTERN.sub("", source)

# Phase 1c: stage-direction earcons. ``[[laugh]]`` / ``[[sigh]]`` etc.
# are stripped from chat text and surfaced as side-channel markers
# routed to ``EarconPlayer``. ``[[tsk]]`` is reserved for Phase 3c
# (self-correction) but lives in the same grammar so we don't have
# to re-thread the parser later.
STAGE_DIRECTION_KINDS: tuple[str, ...] = (
    "laugh", "sigh", "gasp", "hum", "tsk",
    # Layer 4: expanded palette (chuckle / soft_sigh / sharp_gasp /
    # breath / mm). The synth recipes live in :mod:`app.audio.earcons`;
    # the cadence layer auto-sprinkles ``breath`` / ``soft_sigh`` on
    # opening melancholy / wistful / sad sentences but the LLM can
    # still emit any of them inline via ``[[chuckle]]`` etc.
    "chuckle", "soft_sigh", "sharp_gasp", "breath", "mm",
)
_STAGE_DIRECTION_PATTERN = re.compile(
    r"\[\[(" + "|".join(STAGE_DIRECTION_KINDS) + r")\]\]",
    flags=re.IGNORECASE,
)
# Phase 4a: [[agenda:goal]] / [[agenda:0.7:goal]] — extracted by
# AgendaStore in SessionController and stripped from user-visible text.
_AGENDA_TAG_PATTERN = re.compile(
    r"\[\[agenda(?::[0-9.]+)?:[^\]]*?\]\]",
    flags=re.IGNORECASE,
)
_AGENDA_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[agenda(?::[0-9.]+)?:[^\]]*\Z",
    flags=re.IGNORECASE,
)

# Schema v7: [[moment:vibe:short summary]] — Aiko-curated shared moment
# (see :mod:`app.core.relationship.shared_moment_extractor`). Stripped from chat text
# and TTS; persisted to a ``shared_moment`` memory row by
# SessionController. The grammar matches the [[remember:…]] family so
# the streaming hold logic in :func:`safe_visible_prefix` already covers
# the in-progress tail.
_MOMENT_TAG_PATTERN = re.compile(
    r"\[\[moment:[^\]]*?\]\]",
    flags=re.IGNORECASE,
)
_MOMENT_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[moment:[^\]]*\Z",
    flags=re.IGNORECASE,
)

# Layer 3 (expressive speech): [[prosody:NAME]] — per-sentence vocal
# delivery tag, orthogonal to [[reaction:X]] (mood label) and the
# stage-direction earcons. Five values for v1: ``whisper``, ``soft``,
# ``slow``, ``fast``, ``firm``. Each maps to a small overlay applied
# by :class:`app.core.voice.cadence.ProsodyDispatcher` (speed multiplier +
# gain dB + sometimes a pause hint). Leading position in the
# sentence wins; trailing tags are still stripped from chat / TTS by
# :func:`strip_all_meta_tags` so a misplaced tag never leaks audio.
PROSODY_TAG_VALUES: tuple[str, ...] = (
    "whisper", "soft", "slow", "fast", "firm",
)
_PROSODY_TAG_PATTERN = re.compile(
    r"\[\[prosody:(?P<label>[a-z_]+)\]\]",
    flags=re.IGNORECASE,
)
_PROSODY_LEADING_PATTERN = re.compile(
    r"^\s*\[\[prosody:(?P<label>[a-z_]+)\]\]\s*",
    flags=re.IGNORECASE,
)
_PROSODY_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[prosody:[^\]]*\Z",
    flags=re.IGNORECASE,
)


def parse_prosody_tag(text: str) -> str | None:
    """Return the leading ``[[prosody:LABEL]]`` value (if any).

    The cadence dispatcher consumes this as a per-sentence overlay
    on top of the reaction-derived ``ProsodyParams``. Single-valued
    per sentence -- a sentence can carry at most one prosody tag,
    and only when the tag is at the *start* of the sentence (a
    middle-of-the-sentence tag still gets stripped by
    :func:`strip_all_meta_tags` but doesn't drive prosody, mirroring
    the leading-tag idiom of :func:`parse_reaction_at_start`).

    Returns the lowercased label when valid, else ``None``.
    """
    source = str(text or "")
    match = _PROSODY_LEADING_PATTERN.match(source)
    if not match:
        return None
    label = (match.group("label") or "").strip().lower()
    if not label or label not in PROSODY_TAG_VALUES:
        return None
    return label


def consume_leading_prosody_tag(text: str) -> tuple[str | None, str]:
    """Strip the leading ``[[prosody:LABEL]]`` from ``text``.

    Returns ``(label_or_None, remainder)``. Used by the cadence
    dispatcher: consume the leading tag, route it through the
    overlay table, then dispatch the remainder as the spoken
    sentence. A non-leading or malformed tag returns
    ``(None, text)`` and is dropped only by
    :func:`strip_all_meta_tags` later in the pipeline.
    """
    label = parse_prosody_tag(text)
    if label is None:
        return None, str(text or "")
    source = str(text or "")
    rest = _PROSODY_LEADING_PATTERN.sub("", source, count=1)
    return label, rest


# H1: [[arc:NAME]] — Aiko's optional self-tag of the conversation arc
# (one of the six values in :data:`app.core.conversation.conversation_arc.VALID_ARCS`).
# Stripped from display + TTS like the other meta tags; consumed by
# :class:`ArcStore.set_from_self_tag` at confidence 0.85 so a regex hit
# can't immediately overwrite it. Tag is single-valued per turn -- if
# Aiko emits more than one, callers take the last and ignore the rest.
_ARC_TAG_PATTERN = re.compile(
    r"\[\[arc:(?P<arc>[a-z_]+)\]\]",
    flags=re.IGNORECASE,
)
_ARC_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[arc:[^\]]*\Z",
    flags=re.IGNORECASE,
)

# F2 personality backlog: [[gap:topic:short question]] — knowledge-gap
# journal entry (see :mod:`app.core.memory.knowledge_gap_extractor`). Same
# stripping treatment as [[remember:...]] and [[moment:...]]: invisible
# to chat / TTS, persisted to a ``knowledge_gap`` memory row by
# SessionController._post_turn_inner_life. F1's background fact-checker
# may later resolve the gap and write the answer alongside it.
_GAP_TAG_PATTERN = re.compile(
    r"\[\[gap:[^\]]*?\]\]",
    flags=re.IGNORECASE,
)
_GAP_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[gap:[^\]]*\Z",
    flags=re.IGNORECASE,
)

# F5 personality backlog: [[conflict:reason]] — Aiko self-flag for a
# memory contradiction she noticed mid-turn ("hold on, that doesn't
# match what you told me last week"). The body is a short free-text
# reason (4-200 chars, no square brackets / newlines) the worker
# logs alongside the auto-detected pair. Stripped from chat / TTS
# the same way as ``[[gap:...]]``; the SessionController dispatch
# (``_post_turn_inner_life``) logs the reason and force_runs the
# F5 worker so the conflict surfaces in the next idle window.
_CONFLICT_TAG_PATTERN = re.compile(
    r"\[\[conflict:([^\[\]\n]{4,200}?)\]\]",
    flags=re.IGNORECASE,
)
_CONFLICT_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[conflict:[^\]]*\Z",
    flags=re.IGNORECASE,
)

# K1 personality backlog: [[goal:summary]] — Aiko self-tag declaring
# one of her own long-term personal goals (something she wants to grow
# into / explore / become better at). Single colon, body is a short
# free-text summary (4-200 chars, no square brackets / newlines).
# Stripped from chat / TTS the same way as ``[[gap:...]]``. The
# SessionController dispatch (``_post_turn_inner_life``) hands every
# extracted tag to :meth:`app.core.goals.goal_store.GoalStore.add_goal`.
_GOAL_TAG_PATTERN = re.compile(
    r"\[\[goal:([^\[\]\n]{4,200}?)\]\]",
    flags=re.IGNORECASE,
)
_GOAL_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[goal:[^\]]*\Z",
    flags=re.IGNORECASE,
)


# K2 personality backlog: [[predict:kind:topic:state:confidence]] —
# Aiko self-tag for a theory-of-mind prediction about the user
# ("I think Jacob is excited about the tokyo trip"). The grammar
# uses four colon-separated fields mirroring the existing
# ``[[moment:vibe:summary]]`` precedent:
#
#   kind        -- 'mood' | 'opinion' (canonical, lowercased)
#   topic       -- short topic phrase, 2-80 chars, no '[' / ']' / '\n'
#   state       -- predicted state phrase, 2-120 chars, same character set
#   confidence  -- decimal in [0, 1], one or two digit fraction OK
#
# Examples::
#
#   [[predict:mood:tokyo trip:excited:0.8]]
#   [[predict:opinion:rust language:overhyped:0.6]]
#
# Stripped from chat / TTS the same way as ``[[conflict:...]]``.
# The SessionController dispatch (``_post_turn_inner_life``) calls
# :func:`extract_predict_tags` and upserts each tuple via
# :class:`app.core.relationship.belief_store.BeliefStore`, then force_runs the
# K2 worker so any belief Aiko already inferred gets a fresh
# gap-detector evaluation in the same tick.
_PREDICT_TAG_PATTERN = re.compile(
    r"\[\[predict:"
    r"([a-zA-Z]{3,12}):"           # kind
    r"([^:\[\]\n]{2,80}?):"        # topic
    r"([^:\[\]\n]{2,120}?):"       # predicted state
    r"(\d{1}(?:\.\d{1,3})?)"        # confidence (0, 1, 0.7, 0.85, ...)
    r"\]\]",
    flags=re.IGNORECASE,
)
_PREDICT_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[predict:[^\]]*\Z",
    flags=re.IGNORECASE,
)

# Alexia bundle: [[overlay:NAME]] fires a transient overlay pulse on
# the avatar (sweat / blush / dizzy / question / ...). The grammar
# is identical in shape to ``[[reaction:X]]`` — the LLM emits one
# inline and the renderer pulses the corresponding parameter for
# ~1.5s. Stripped from chat text and TTS; surfaced to the avatar via
# the ``avatar_overlay`` WS event by :class:`TurnRunner`.
#
# Stacked form ``[[overlay:A+B]]`` (Phase 3 expression overhaul):
# the body matches ``X+Y[+Z…]`` and the dispatch path
# (``turn_runner._emit_overlay_stack``) splits on ``+`` so each
# component fires a separate overlay pulse. ``OverlayChannel``
# already supports concurrent param pulses, so ``blush+grin`` runs
# them additively (blush is a Param58 pulse; grin is an
# ``expr:lzx`` pulse and locks the expression slot for its
# lifetime, the others paint their own params alongside it).
_OVERLAY_TAG_PATTERN = re.compile(
    r"\[\[overlay:([A-Za-z_][A-Za-z0-9_+]*)\]\]",
    flags=re.IGNORECASE,
)
_OVERLAY_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[overlay:[^\]]*\Z",
    flags=re.IGNORECASE,
)
# Alexia bundle: [[outfit:NAME]] is a persistent outfit override.
# Same shape as overlay but the SessionController treats it as
# sticky state (until the next circadian boundary) rather than a
# transient pulse.
_OUTFIT_TAG_PATTERN = re.compile(
    r"\[\[outfit:([A-Za-z_][A-Za-z0-9_]*)\]\]",
    flags=re.IGNORECASE,
)
_OUTFIT_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[outfit:[^\]]*\Z",
    flags=re.IGNORECASE,
)
# Alexia bundle: [[motion:NAME]] plays a Live2D motion file.
_MOTION_TAG_PATTERN = re.compile(
    r"\[\[motion:([A-Za-z_][A-Za-z0-9_]*)\]\]",
    flags=re.IGNORECASE,
)
_MOTION_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[motion:[^\]]*\Z",
    flags=re.IGNORECASE,
)
# K31 soft physicality: [[touch:KIND]] reaches toward the user with a
# small physical gesture (hug, head_pat, poke, wave, ...). The
# taxonomy + axes gate + cadence live in
# ``app/core/touch/touch_gestures.py``; the dispatch path threads
# through ``TurnRunner.on_touch`` -> ``avatar_mixin._emit_avatar_touch``
# -> WS ``avatar_touch`` event. The literal touch is sold by the
# bubble footer badge + persona action banner; the avatar lean-in is
# an approximation because the Alexia rig has no real reach params.
_TOUCH_TAG_PATTERN = re.compile(
    r"\[\[touch:([A-Za-z_][A-Za-z0-9_]*)\]\]",
    flags=re.IGNORECASE,
)
_TOUCH_OPEN_TAIL_PATTERN = re.compile(
    r"\[\[touch:[^\]]*\Z",
    flags=re.IGNORECASE,
)


def extract_overlays(text: str) -> list[tuple[str, int]]:
    """Return ``(name, char_offset)`` overlay markers from ``text``.

    Position is the offset *into the original string* so callers can
    splice the dispatch in at the right point in the stream.
    """
    source = str(text or "")
    if not source:
        return []
    return [
        (m.group(1).strip().lower(), m.start())
        for m in _OVERLAY_TAG_PATTERN.finditer(source)
    ]


def extract_outfit_commands(text: str) -> list[tuple[str, int]]:
    """Return ``(name, char_offset)`` ``[[outfit:NAME]]`` markers."""
    source = str(text or "")
    if not source:
        return []
    return [
        (m.group(1).strip().lower(), m.start())
        for m in _OUTFIT_TAG_PATTERN.finditer(source)
    ]


def extract_motion_commands(text: str) -> list[tuple[str, int]]:
    """Return ``(name, char_offset)`` ``[[motion:NAME]]`` markers."""
    source = str(text or "")
    if not source:
        return []
    return [
        (m.group(1).strip().lower(), m.start())
        for m in _MOTION_TAG_PATTERN.finditer(source)
    ]


def extract_touch_commands(text: str) -> list[tuple[str, int]]:
    """Return ``(kind, char_offset)`` ``[[touch:KIND]]`` markers.

    Used by the post-turn pass in ``avatar_mixin._persist_turn_gestures``
    to gather every touch kind Aiko emitted in this turn even if some
    landed in chunks the streaming path didn't see (e.g. when a tool
    pass merged into the final text). Stays as the canonical list-order
    so consecutive ``[[touch:hug]] [[touch:hug]]`` reads as two entries.
    """
    source = str(text or "")
    if not source:
        return []
    return [
        (m.group(1).strip().lower(), m.start())
        for m in _TOUCH_TAG_PATTERN.finditer(source)
    ]


def parse_arc_tags(text: str) -> list[str]:
    """Return every well-formed ``[[arc:NAME]]`` value from ``text``.

    Values are lowercased and trimmed; validation against
    :data:`app.core.conversation.conversation_arc.VALID_ARCS` happens at the dispatch
    site (this module avoids the import cycle). Tag is intentionally
    single-valued per turn -- if Aiko emits more than one, the dispatcher
    takes ``parse_arc_tags(...)[-1]`` and ignores the rest.
    """
    source = str(text or "")
    if not source:
        return []
    out: list[str] = []
    for m in _ARC_TAG_PATTERN.finditer(source):
        value = (m.group("arc") or "").strip().lower()
        if value:
            out.append(value)
    return out


def extract_goal_tags(text: str) -> list[str]:
    """Return the trimmed body of every ``[[goal:summary]]`` tag in ``text``.

    Duplicates within a single text (case-insensitive) are collapsed
    so a repeated tag inside the same reply doesn't flood the goal
    journal. Empty / malformed tags are silently skipped.
    """
    source = str(text or "")
    if not source:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _GOAL_TAG_PATTERN.finditer(source):
        body = (m.group(1) or "").strip()
        if not body:
            continue
        key = body.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(body)
    return out


def extract_conflict_tags(text: str) -> list[str]:
    """Return the body of every well-formed ``[[conflict:reason]]`` tag.

    The body is trimmed but preserves the original case (the F5 worker
    logs it verbatim alongside the auto-detected pair). Empty / mal-
    formed tags are skipped.
    """
    source = str(text or "")
    if not source:
        return []
    out: list[str] = []
    for m in _CONFLICT_TAG_PATTERN.finditer(source):
        body = (m.group(1) or "").strip()
        if body:
            out.append(body)
    return out


@dataclasses.dataclass(frozen=True, slots=True)
class PredictTag:
    """Parsed ``[[predict:kind:topic:state:confidence]]`` tuple.

    The K2 dispatch path on :class:`SessionController` hands these
    straight to :meth:`BeliefStore.upsert`. ``kind`` and ``topic`` are
    lowercased + trimmed; ``predicted_state`` preserves its original
    casing because UI / TTS render it back to the user. ``confidence``
    is clamped to ``[0, 1]``.
    """

    kind: str
    topic: str
    predicted_state: str
    confidence: float


def extract_predict_tags(text: str) -> list[PredictTag]:
    """Return every well-formed ``[[predict:kind:topic:state:conf]]`` tag.

    Malformed tags (kind outside ``{mood, opinion}``, empty topic/state,
    confidence outside ``[0, 1]``) are silently skipped so a partial
    LLM emission can never break the turn.
    """
    source = str(text or "")
    if not source:
        return []
    out: list[PredictTag] = []
    for m in _PREDICT_TAG_PATTERN.finditer(source):
        kind = (m.group(1) or "").strip().lower()
        topic = (m.group(2) or "").strip().lower()
        state = (m.group(3) or "").strip()
        try:
            confidence = float(m.group(4))
        except ValueError:
            continue
        if kind not in ("mood", "opinion"):
            continue
        if not topic or not state:
            continue
        confidence = max(0.0, min(1.0, confidence))
        out.append(
            PredictTag(
                kind=kind,
                topic=topic,
                predicted_state=state,
                confidence=confidence,
            )
        )
    return out


def strip_all_meta_tags(text: str) -> str:
    """Remove every closed meta tag the assistant emits.

    Behaviour:
      - ``[[reaction:X]]`` markers: removed (content is the marker itself).
      - ``[[spoken]]`` / ``[[/spoken]]`` markers: removed, content kept (so a
        legacy two-tier reply degrades into spoken-only).
      - ``[[detail]]...[[/detail]]`` block: marker AND content removed.
      - ``[[detail]]`` opener with no closing tag yet (end of text): everything
        from the opener to end is suppressed (private detail-in-progress).
      - ``[[remember:...]]`` block: marker AND content removed (private).
      - ``[[remember:...`` opener with no closing ``]]`` yet: suppressed
        through end of text.

    Does NOT call ``.strip()`` on the result, so streaming callers can keep
    monotonic ``len(visible)`` offsets across deltas. Partial / unrecognized
    tags pass through unchanged -- callers are expected to use
    :func:`safe_visible_prefix` for streaming display.
    """
    s = str(text or "")
    if not s:
        return s
    # Phase 3c: drop ``[[correct]]old[[/correct]]`` blocks entirely so
    # only the corrected text survives in plain-text views (TTS, DB
    # transcript). The chat UI receives the raw text upstream and is
    # expected to render strikethrough on the ``old`` span itself.
    s = _CORRECTION_BLOCK_PATTERN.sub("", s)
    # Unclosed correction opener at end of stream: hide the in-progress
    # ``old`` text until the close arrives.
    s = _CORRECTION_OPEN_TAIL_PATTERN.sub("", s)
    # Drop fully-formed detail blocks first (greedy on the *content*).
    s = _DETAIL_BLOCK_PATTERN.sub("", s)
    # Drop an unclosed detail opener that runs off the end of the string.
    open_idx = s.lower().rfind("[[detail]]")
    if open_idx >= 0 and "[[/detail]]" not in s[open_idx:].lower():
        s = s[:open_idx]
    # Drop fully-formed remember tags + content.
    s = _REMEMBER_TAG_PATTERN.sub("", s)
    # Drop an unclosed remember opener that runs off the end.
    s = _REMEMBER_OPEN_TAIL_PATTERN.sub("", s)
    # Phase 4a: same treatment for [[agenda:...]].
    s = _AGENDA_TAG_PATTERN.sub("", s)
    s = _AGENDA_OPEN_TAIL_PATTERN.sub("", s)
    # Schema v7: same treatment for [[moment:vibe:summary]].
    s = _MOMENT_TAG_PATTERN.sub("", s)
    s = _MOMENT_OPEN_TAIL_PATTERN.sub("", s)
    # H1: same treatment for [[arc:NAME]] self-tags.
    s = _ARC_TAG_PATTERN.sub("", s)
    s = _ARC_OPEN_TAIL_PATTERN.sub("", s)
    # Layer 3: per-sentence [[prosody:LABEL]] vocal-delivery tags.
    # The leading tag at sentence start has already been consumed by
    # :func:`consume_leading_prosody_tag` upstream and routed into the
    # cadence overlay; this strip catches misplaced / trailing tags
    # so they don't reach TTS or the chat transcript.
    s = _PROSODY_TAG_PATTERN.sub("", s)
    s = _PROSODY_OPEN_TAIL_PATTERN.sub("", s)
    # F2: same treatment for [[gap:topic:question]].
    s = _GAP_TAG_PATTERN.sub("", s)
    s = _GAP_OPEN_TAIL_PATTERN.sub("", s)
    # F5: same treatment for [[conflict:reason]] self-tags.
    s = _CONFLICT_TAG_PATTERN.sub("", s)
    s = _CONFLICT_OPEN_TAIL_PATTERN.sub("", s)
    # K1: same treatment for [[goal:summary]] self-tags. The body is
    # extracted upstream by :func:`extract_goal_tags` and dispatched
    # in ``_post_turn_inner_life``; the strip here makes the tag
    # invisible to chat / TTS regardless of whether the side-channel
    # ran.
    s = _GOAL_TAG_PATTERN.sub("", s)
    s = _GOAL_OPEN_TAIL_PATTERN.sub("", s)
    # K2: same treatment for [[predict:kind:topic:state:confidence]].
    s = _PREDICT_TAG_PATTERN.sub("", s)
    s = _PREDICT_OPEN_TAIL_PATTERN.sub("", s)
    # Alexia bundle: drop fully-formed overlay / outfit / motion tags
    # + their unclosed openers at end-of-stream. Side-channel
    # (TurnRunner) extracted them earlier; stripping here guarantees
    # they never leak into the chat transcript or TTS even if that
    # side-channel was skipped.
    s = _OVERLAY_TAG_PATTERN.sub("", s)
    s = _OVERLAY_OPEN_TAIL_PATTERN.sub("", s)
    s = _OUTFIT_TAG_PATTERN.sub("", s)
    s = _OUTFIT_OPEN_TAIL_PATTERN.sub("", s)
    s = _MOTION_TAG_PATTERN.sub("", s)
    s = _MOTION_OPEN_TAIL_PATTERN.sub("", s)
    # K31 soft physicality: drop ``[[touch:KIND]]`` tags so they never
    # leak into chat / TTS. The side-channel (``TurnRunner.on_touch``)
    # extracted them earlier; this is the belt-and-braces strip.
    s = _TOUCH_TAG_PATTERN.sub("", s)
    s = _TOUCH_OPEN_TAIL_PATTERN.sub("", s)
    # Phase 1c: stage-direction earcons are stripped from display text;
    # the audio side-channel pulls them via :func:`extract_stage_directions`
    # before this stripping runs. Stripping here keeps the chat
    # transcript clean even if the side-channel was skipped.
    s = _STAGE_DIRECTION_PATTERN.sub("", s)
    # Strip the spoken/reaction markers (content kept for spoken).
    for tag in (_SPOKEN_OPEN, _SPOKEN_CLOSE):
        s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE)
    s = _REACTION_TAG_PATTERN.sub("", s)
    return s


def split_text_with_stage_directions(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into a sequence of ``("text", chunk)`` /
    ``("earcon", kind)`` pairs preserving order.

    Used by :class:`TtsQueue` to interleave short audio cues (laughs,
    sighs, gasps) into spoken playback. The text chunks still carry
    everything else (reaction tags, etc.); the caller is expected to
    run :func:`strip_all_meta_tags` on them before synthesis.

    Example::

        >>> split_text_with_stage_directions("Yeah [[laugh]] right.")
        [("text", "Yeah "), ("earcon", "laugh"), ("text", " right.")]
    """
    source = str(text or "")
    if not source:
        return []
    pieces: list[tuple[str, str]] = []
    cursor = 0
    for match in _STAGE_DIRECTION_PATTERN.finditer(source):
        if match.start() > cursor:
            pieces.append(("text", source[cursor : match.start()]))
        pieces.append(("earcon", match.group(1).strip().lower()))
        cursor = match.end()
    if cursor < len(source):
        pieces.append(("text", source[cursor:]))
    return pieces


def extract_stage_directions(text: str) -> list[tuple[str, int]]:
    """Return ``(kind, char_position)`` markers as they appear in
    ``text``. Position is the offset *into the original string* (not the
    cleaned one) so callers can choose to splice them in pre- or
    post-strip. Most callers should prefer
    :func:`split_text_with_stage_directions`.
    """
    source = str(text or "")
    return [
        (m.group(1).strip().lower(), m.start())
        for m in _STAGE_DIRECTION_PATTERN.finditer(source)
    ]


# Tokens that mark the start of a meta block but might not yet be complete in
# a streaming buffer. If we see any of these prefixes (or a partial form like
# ``[[de``) we have to hold the tail back until we have enough chars to decide.
_META_OPENERS = (
    "[[reaction:",
    "[[spoken]]",
    "[[/spoken]]",
    "[[detail]]",
    "[[/detail]]",
    "[[remember:",
    "[[laugh]]",
    "[[sigh]]",
    "[[gasp]]",
    "[[hum]]",
    "[[tsk]]",
    "[[chuckle]]",
    "[[soft_sigh]]",
    "[[sharp_gasp]]",
    "[[breath]]",
    "[[mm]]",
    "[[correct]]",
    "[[/correct]]",
    "[[overlay:",
    "[[outfit:",
    "[[motion:",
    "[[moment:",
    "[[arc:",
    "[[gap:",
    "[[conflict:",
    "[[predict:",
    "[[prosody:",
    "[[goal:",
    "[[touch:",
)


def _looks_like_partial_opener(suffix: str) -> bool:
    """Return True if ``suffix`` could still grow into a known meta opener.

    The streaming holdback uses this to decide which characters can be safely
    emitted *now* vs held until more deltas arrive. We're permissive on
    purpose: any string that is a prefix of any known opener forces a hold.
    """
    if not suffix:
        return False
    lowered = suffix.lower()
    # Single ``[`` or ``[[`` could be the start of any opener.
    if lowered in {"[", "[["}:
        return True
    for opener in _META_OPENERS:
        if opener.startswith(lowered):
            return True
    # ``[[reaction:foo`` (no closing yet) -- still ambiguous; hold.
    if lowered.startswith("[[reaction:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[remember:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[overlay:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[moment:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[arc:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[gap:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[conflict:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[prosody:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[goal:") and "]]" not in lowered:
        return True
    if lowered.startswith("[[touch:") and "]]" not in lowered:
        return True
    # Mid-tag like ``[[d`` / ``[[de`` / ``[[s`` etc.
    if lowered.startswith("[["):
        return True
    return False


def safe_visible_prefix(text: str) -> str:
    """Return the prefix of ``text`` that is safe to display *right now*.

    Used by the streaming UI/TTS pipeline. Any tail that could still grow
    into a meta tag is held back; once the next delta arrives the caller
    re-runs this on the new cumulative text. The full text after stream
    completion goes through :func:`strip_all_meta_tags` for final cleanup.

    Algorithm: strip every *complete* meta tag, then find the leftmost ``[``
    whose suffix could still grow into a known opener. Hold back from there.
    """
    if not text:
        return ""
    cleaned = strip_all_meta_tags(text)
    if not cleaned:
        return ""
    holdback_start = len(cleaned)
    for i, ch in enumerate(cleaned):
        if ch != "[":
            continue
        if _looks_like_partial_opener(cleaned[i:]):
            holdback_start = i
            break
    return cleaned[:holdback_start]