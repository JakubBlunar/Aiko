"""K51 — cue-register rotation: de-"Heads-up" the inner life.

Dozens of inner-life cue blocks (style ruts, self-noticing, novelty,
stagnation, clarification, calibration, misattunement, rupture,
opinion injection, mood inertia, user reactions, promise
follow-through, self-correction) share one meta-template: a line that
opens with the literal ``Heads-up: ``. The persona keeps telling Aiko
"never narrate the cue", but feeding the model the same coach
register dozens of times per session trains exactly that voice into
replies.

This module is the pure half of the fix: the producers keep emitting
``Heads-up: ...`` unchanged (single audit point, stable producer
tests), and the prompt assembler rewrites the prefix at the last
moment, rotating across a few register shapes keyed on a per-turn
seed plus a per-block ordinal so two cues in the same prompt never
share a shape.

Determinism contract: the seed must be stable *within* a turn (the
tool pass and the streaming pass assemble the same prompt twice — a
clock- or random-keyed rotation would make them disagree) and vary
*across* turns. :func:`turn_seed` derives it from the user text plus
the history length; no wall clock, no RNG.

Cache note: every rotated block lives in the already-uncached T5/T6
tail of the system prompt, so rotation has zero effect on the
OpenAI prompt-cache hit rate (see ``docs/prompt-caching.md``).
"""
from __future__ import annotations

import zlib

# The literal prefix every producer emits. Rotation only ever touches
# lines that start with this — everything else passes through
# byte-identical.
_CUE_PREFIX = "Heads-up:"

# Register shapes. Index 0 keeps the original so roughly a quarter of
# cues still read as the classic "Heads-up:" (the persona examples
# stay truthful), the middle two swap the coach register for quieter
# notice-words, and the last drops the prefix entirely — the body is
# already a second-person observation, so bare works as-is.
_SHAPES: tuple[str, ...] = (
    "Heads-up:",
    "Quiet note:",
    "Noticing:",
    "",
)


def turn_seed(user_text: str, history_len: int = 0) -> int:
    """Deterministic per-turn rotation seed.

    Same ``(user_text, history_len)`` -> same seed, so the tool pass
    and the streaming pass of one turn rotate identically. The
    history length keeps two different turns with identical text
    ("ok" twice in a row) from landing on the same shapes.
    """
    crc = zlib.crc32((user_text or "").encode("utf-8", errors="replace"))
    return (crc ^ (max(0, int(history_len)) * 0x9E3779B1)) & 0x7FFFFFFF


def rotate_cue_prefix(block: str, *, seed: int, ordinal: int) -> str:
    """Rewrite the ``Heads-up:`` prefix of ``block`` into a rotated shape.

    No-op for blocks that don't start with the prefix (safe to
    over-apply). Multi-line blocks (K30 self-noticing joins 1-3
    ``Heads-up`` lines) get one shape per matching line, advancing the
    ordinal line-to-line so they differ within the block.

    Returns the number of rewritten lines indirectly via the caller's
    convention: callers should advance their running ordinal by
    :func:`count_cue_lines` of the original block.
    """
    if not block or _CUE_PREFIX not in block:
        return block
    out_lines: list[str] = []
    bump = 0
    for line in block.split("\n"):
        if line.startswith(_CUE_PREFIX):
            out_lines.append(_reshape(line, seed=seed, ordinal=ordinal + bump))
            bump += 1
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def count_cue_lines(block: str) -> int:
    """How many lines of ``block`` start with the cue prefix.

    Callers advance their running ordinal by this so the next block's
    first line continues the rotation instead of repeating a shape.
    """
    if not block or _CUE_PREFIX not in block:
        return 0
    return sum(
        1 for line in block.split("\n") if line.startswith(_CUE_PREFIX)
    )


def lint_shared_prefixes(
    blocks: list[str],
    *,
    threshold: int = 2,
) -> list[tuple[str, int]]:
    """Histogram the first two words of each non-empty block line set.

    Returns ``(prefix, count)`` pairs for prefixes that open more than
    ``threshold`` blocks in one prompt — the regression signal K51
    exists to prevent. Only a block's *first* line counts (the
    repeated meta-template is an opener problem).
    """
    counts: dict[str, int] = {}
    for block in blocks:
        text = (block or "").strip()
        if not text:
            continue
        first_line = text.split("\n", 1)[0]
        words = first_line.split()
        if len(words) < 2:
            continue
        prefix = " ".join(words[:2])
        counts[prefix] = counts.get(prefix, 0) + 1
    return sorted(
        ((prefix, n) for prefix, n in counts.items() if n > threshold),
        key=lambda item: (-item[1], item[0]),
    )


def _reshape(line: str, *, seed: int, ordinal: int) -> str:
    """Apply the shape selected by ``(seed + ordinal)`` to one line."""
    shape = _SHAPES[(seed + ordinal) % len(_SHAPES)]
    if shape == _CUE_PREFIX:
        return line
    body = line[len(_CUE_PREFIX):].lstrip()
    if not body:
        return line
    if shape:
        return f"{shape} {body}"
    # Bare shape: drop the prefix entirely; capitalise the first
    # letter so the line still reads as a sentence.
    return body[0].upper() + body[1:]


__all__ = [
    "count_cue_lines",
    "lint_shared_prefixes",
    "rotate_cue_prefix",
    "turn_seed",
]
