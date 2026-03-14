from __future__ import annotations

import re


_REACTION_TAG_PATTERN = re.compile(
    r"\[\[reaction:(neutral|cheerful|excited|surprised|sad|angry|calm|serious|friendly|gentle|enthusiastic)\]\]",
    flags=re.IGNORECASE,
)
# At start of text only (for streaming): optional whitespace, then tag, then optional newlines
_REACTION_AT_START_PATTERN = re.compile(
    r"^\s*\[\[reaction:(neutral|cheerful|excited|surprised|sad|angry|calm|serious|friendly|gentle|enthusiastic)\]\]\s*\n*",
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