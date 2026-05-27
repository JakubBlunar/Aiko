from __future__ import annotations

import re


_REACTION_TAG_PATTERN = re.compile(
    r"\[\[reaction:(\w+)\]\]",
    flags=re.IGNORECASE,
)
_REACTION_AT_START_PATTERN = re.compile(
    r"^\s*\[\[reaction:(\w+)\]\]\s*\n*",
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
    Use for streaming: call with accumulated buffer; when tag is complete, strip it and use rest for TTS."""
    source = str(text or "")
    match = _REACTION_AT_START_PATTERN.match(source)
    if not match:
        return None, source
    reaction = match.group(1).strip().lower()
    rest = source[match.end() :].lstrip("\n")
    return reaction, rest


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
# (see :mod:`app.core.shared_moment_extractor`). Stripped from chat text
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

# F2 personality backlog: [[gap:topic:short question]] — knowledge-gap
# journal entry (see :mod:`app.core.knowledge_gap_extractor`). Same
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

# Alexia bundle: [[overlay:NAME]] fires a transient overlay pulse on
# the avatar (sweat / blush / dizzy / question / ...). The grammar
# is identical in shape to ``[[reaction:X]]`` — the LLM emits one
# inline and the renderer pulses the corresponding parameter for
# ~1.5s. Stripped from chat text and TTS; surfaced to the avatar via
# the ``avatar_overlay`` WS event by :class:`TurnRunner`.
_OVERLAY_TAG_PATTERN = re.compile(
    r"\[\[overlay:([A-Za-z_][A-Za-z0-9_]*)\]\]",
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
    # F2: same treatment for [[gap:topic:question]].
    s = _GAP_TAG_PATTERN.sub("", s)
    s = _GAP_OPEN_TAIL_PATTERN.sub("", s)
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
    "[[correct]]",
    "[[/correct]]",
    "[[overlay:",
    "[[outfit:",
    "[[motion:",
    "[[moment:",
    "[[gap:",
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
    if lowered.startswith("[[gap:") and "]]" not in lowered:
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