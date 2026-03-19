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