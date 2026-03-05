from __future__ import annotations

import re


_REACTION_TAG_PATTERN = re.compile(
    r"\[\[reaction:(neutral|excited|surprised|sad|angry|calm)\]\]",
    flags=re.IGNORECASE,
)
_ACTION_META_LINE_PATTERN = re.compile(
    r"^(\[plan\]|\[action\]|system:\s*step\s+\d+|step\s+\d+\s*\(|awaiting confirmation)",
    flags=re.IGNORECASE,
)


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
        line = raw_line.strip()
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

        cleaned_lines.append(raw_line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned