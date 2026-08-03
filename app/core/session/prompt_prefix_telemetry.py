"""P44 — where the provider's prompt cache stops matching, and why.

Prompt caching is prefix caching: the provider reuses tokens up to the
FIRST byte that differs from a previous request. So "how much of the
prompt was cacheable" is entirely a question about the earliest block
that changed between two consecutive turns — everything after it pays
full price no matter how stable it is.

This module answers that question without touching the prompt. A
:class:`PrefixSnapshot` records what a turn's prompt looked like (block
digests, block sizes, history digests); :func:`diagnose_divergence`
compares two of them and reports the earliest change in ladder order,
what it cost, and whether the history moved underneath it.

:func:`diagnose_divergence` is deliberately pure and takes its ladder as
an argument, so the interesting cases can be tested against a five-block
synthetic ladder instead of assembling a real 30 KB prompt.

Records go to their own JSONL file (``data/prompt-cache.jsonl``), never
to ``app.log`` — see :func:`configure_prompt_cache_log` in
``app/core/infra/crash_logging.py``. The file is opt-in because a
per-turn record is only worth writing while someone is actually reading
it back with ``scripts/prefix_break_report.py``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# The dedicated sink. Configured with ``propagate = False`` so these
# records never reach the ``app`` logger's stderr / file / ring-buffer
# handlers; see ``configure_prompt_cache_log``.
PROMPT_CACHE_LOGGER = "app.promptcache"

log = logging.getLogger(PROMPT_CACHE_LOGGER)


def message_digest(text: str) -> str:
    """Short content digest, matching ``block_hash_table``'s scheme."""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


@dataclass(frozen=True, slots=True)
class PrefixSnapshot:
    """What one turn's prompt looked like, for next turn to compare against.

    Holds digests rather than text: the comparison only ever asks "did
    this change", and keeping ~106 block bodies per session alive would
    be a real memory cost for no gain.
    """

    block_hashes: dict[str, str] = field(default_factory=dict)
    block_chars: dict[str, int] = field(default_factory=dict)
    history_hashes: tuple[str, ...] = ()
    history_chars: int = 0
    sys_chars: int = 0


@dataclass(frozen=True, slots=True)
class PrefixDivergence:
    """Where the cacheable prefix ended, and what that cost."""

    # Ladder name of the earliest block whose content changed, or None
    # when the whole system prompt was byte-identical to last turn.
    diverged: str | None = None
    tier: str | None = None
    # Characters at and after the break: the part of the system prompt
    # that cannot be served from cache this turn.
    lost_chars: int = 0
    lost_pct: float = 0.0
    changed: int = 0
    changed_by_tier: dict[str, int] = field(default_factory=dict)
    # Index of the first history message differing from last turn, or
    # None when the shared prefix ran to the end of the shorter list.
    history_diverged: int | None = None
    # How many messages fell off the front of the window since last
    # turn, when a pure shift explains the difference; -1 when it does
    # not (i.e. retained messages were themselves rewritten, which is
    # the fingerprint of the relative-age prefixes).
    history_slid: int = 0
    history_msgs: int = 0
    history_chars: int = 0
    sys_chars: int = 0
    # True on the first turn of a session, where there is nothing to
    # compare against. Such records carry no divergence signal and the
    # report script drops them.
    first_turn: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "diverged": self.diverged,
            "tier": self.tier,
            "lost_chars": self.lost_chars,
            "lost_pct": self.lost_pct,
            "changed": self.changed,
            "changed_by_tier": dict(self.changed_by_tier),
            "history_diverged": self.history_diverged,
            "history_slid": self.history_slid,
            "history_msgs": self.history_msgs,
            "history_chars": self.history_chars,
            "sys_chars": self.sys_chars,
            "first_turn": self.first_turn,
        }


def _history_alignment(
    prev: Sequence[str], current: Sequence[str],
) -> int:
    """Messages dropped off the front of ``prev`` to line it up with ``current``.

    Returns ``-1`` when no shift explains the difference, which is the
    interesting answer: it means messages that survived in the window
    had their *text* rewritten between turns. Relative-age prefixes
    (``[3 min ago]`` ticking to ``[4 min ago]``) do exactly that, and
    they defeat history caching in a way that a plain window slide does
    not, because a slide at least leaves a stable tail for the next turn.
    """
    if not prev or not current:
        return 0
    # Stop before the tail empties: an empty tail trivially "matches"
    # anything, so allowing it would make every comparison alignable and
    # -1 unreachable -- which would silently disable the churn signal.
    for dropped in range(len(prev)):
        tail = prev[dropped:]
        overlap = min(len(tail), len(current))
        if overlap and tail[:overlap] == current[:overlap]:
            return dropped
    return -1


def _first_difference(prev: Sequence[str], current: Sequence[str]) -> int | None:
    for index in range(min(len(prev), len(current))):
        if prev[index] != current[index]:
            return index
    return None


def diagnose_divergence(
    prev: PrefixSnapshot | None,
    current: PrefixSnapshot,
    *,
    ladder: Sequence[str] | None = None,
    tier_of: Mapping[str, str] | None = None,
) -> PrefixDivergence:
    """Compare two turns' prompts and locate the end of the cacheable prefix.

    ``ladder`` is the block order the prompt is actually assembled in and
    ``tier_of`` maps each name to its tier; both default to the real
    ones from the assembler (imported lazily to avoid an import cycle).
    Passing them explicitly is what makes the failure modes cheap to test.
    """
    if ladder is None or tier_of is None:
        from app.core.session.prompt_assembler import (
            _BLOCK_TIER_OF,
            _PROMPT_BLOCK_TIERS,
        )
        if ladder is None:
            ladder = [
                name for names in _PROMPT_BLOCK_TIERS.values() for name in names
            ]
        if tier_of is None:
            tier_of = _BLOCK_TIER_OF

    if prev is None:
        return PrefixDivergence(
            history_msgs=len(current.history_hashes),
            history_chars=current.history_chars,
            sys_chars=current.sys_chars,
            first_turn=True,
        )

    changed_names: list[str] = []
    for name in ladder:
        # A block absent from one snapshot and present in the other is a
        # change: it alters the concatenation. Empty blocks are not
        # absent -- they hash the empty string -- so "went empty" is
        # caught here too.
        if prev.block_hashes.get(name) != current.block_hashes.get(name):
            changed_names.append(name)

    changed_by_tier: dict[str, int] = {}
    for name in changed_names:
        tier = tier_of.get(name, "unknown")
        changed_by_tier[tier] = changed_by_tier.get(tier, 0) + 1

    diverged: str | None = None
    tier: str | None = None
    lost_chars = 0
    if changed_names:
        diverged = changed_names[0]
        tier = tier_of.get(diverged)
        break_index = list(ladder).index(diverged)
        lost_chars = sum(
            current.block_chars.get(name, 0)
            for name in list(ladder)[break_index:]
        )

    lost_pct = 0.0
    if current.sys_chars > 0 and lost_chars > 0:
        lost_pct = round(100.0 * lost_chars / current.sys_chars, 1)

    return PrefixDivergence(
        diverged=diverged,
        tier=tier,
        lost_chars=lost_chars,
        lost_pct=lost_pct,
        changed=len(changed_names),
        changed_by_tier=changed_by_tier,
        history_diverged=_first_difference(
            prev.history_hashes, current.history_hashes,
        ),
        history_slid=_history_alignment(
            prev.history_hashes, current.history_hashes,
        ),
        history_msgs=len(current.history_hashes),
        history_chars=current.history_chars,
        sys_chars=current.sys_chars,
    )


def prompt_cache_sink_enabled() -> bool:
    """Whether the JSONL sink is configured, so callers can skip the work.

    Building a record means rolling up tier totals and reading the
    calibration state. Cheap, but pointless while the file is off, which
    is the default.
    """
    return log.isEnabledFor(logging.INFO)


def emit_prefix_record(payload: Mapping[str, Any]) -> None:
    """Write one JSONL record to the prompt-cache sink.

    No-ops when the sink was never configured, so the hot path costs an
    ``isEnabledFor`` check while the feature is off. Serialisation
    failures are swallowed: telemetry must never break a turn.
    """
    if not log.isEnabledFor(logging.INFO):
        return
    try:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        record.update(payload)
        log.info(json.dumps(record, separators=(",", ":"), default=str))
    except Exception:  # pragma: no cover - defensive
        pass
