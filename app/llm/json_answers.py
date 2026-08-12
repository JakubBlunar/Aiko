"""Shape-tolerant parsing of "give me a JSON list" worker answers.

Ollama's ``format: "json"`` constrains generation to a JSON **object**.
A prompt that asks for a bare array therefore sets the model an
impossible task: it reasons its way to the right answer and then the
grammar forces an object anyway. Observed directly on ``qwen3.6:27b``,
whose trace ended ``"No concrete promises. Empty array is correct.
Output: `[]`"`` while ``message.content`` came back as ``{}`` — and, on a
transcript that *did* contain commitments, as a single bare
``{"who": ..., "what": ...}`` object. A parser that only accepts ``[...]``
reads both as failures, so the feature reports "unparseable" or "found
nothing" forever. The promise extractor did exactly that for its entire
life: 58 calls, 0 promises.

The fix is two-sided. Prompts ask for ``{"<key>": [...]}`` (the
convention :mod:`app.core.memory.memory_extractor` already used), and
:func:`parse_json_array_answer` accepts every shape a model plausibly
returns for such a request.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable


# Fallback for providers that wrap the array in prose (no grammar
# constraint): grab the outermost bracketed span. Only used when the whole
# answer isn't valid JSON on its own.
_ARRAY_SPAN_RE = re.compile(r"\[.*\]", flags=re.DOTALL)


def parse_json_array_answer(
    raw: str,
    *,
    key: str,
    item_hint_keys: Iterable[str] = (),
) -> list[Any] | None:
    """Return the list a worker asked the model for, or ``None``.

    ``None`` means *genuinely unparseable* — worth logging as a failure.
    An empty list means "the model had nothing to report", which is a
    normal, successful outcome. Keeping those two apart is the whole
    point: conflating them is what let a broken extractor look healthy.

    Accepted shapes, in order:

    * ``{"<key>": [...]}`` — what the prompts now ask for.
    * ``[...]`` — a bare array, from providers with no object grammar.
    * ``{}`` — an empty object. Under ``format: "json"`` this is how a
      model says "nothing", since it cannot emit a bare ``[]``.
    * ``{"other_key": [...]}`` — the key drifted; take the only list.
    * ``{"who": ..., "what": ...}`` — a single item, unwrapped, when it
      carries one of ``item_hint_keys``.

    ``raw`` that is empty or whitespace returns ``None``; callers should
    normally detect an empty answer before calling, because it has a
    distinct cause (the reasoning trace consumed the token budget) and
    deserves its own log line.
    """
    text = (raw or "").strip()
    if not text:
        return None

    hints = {str(h) for h in item_hint_keys}

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if parsed is None:
        # Not valid JSON as a whole — fall back to the bracketed span.
        match = _ARRAY_SPAN_RE.search(text)
        if match is None:
            return None
        try:
            span = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return span if isinstance(span, list) else None

    if isinstance(parsed, list):
        return parsed

    if isinstance(parsed, dict):
        under_key = parsed.get(key)
        if isinstance(under_key, list):
            return under_key
        if not parsed:
            return []
        for value in parsed.values():
            if isinstance(value, list):
                return value
        if hints and hints & set(parsed.keys()):
            return [parsed]
        return None

    return None


__all__ = ["parse_json_array_answer"]
