"""Why a prompt block never fired — the classification, not the rate (H53).

``turn_prompt_blocks`` answers "how often did each block render", and that
number is correct. What it cannot do is say what a **zero** means, and
five different things produce one:

* the block is *suppressed by design* — K16's grounding line stands in
  for ten named blocks whenever ``grounding_line_mode`` is not ``off``;
* its master switch is off;
* its gate is a wall-clock cooldown **longer than the table is old**, so
  the rate is not merely small, it is *unobservable*;
* it genuinely never opened;
* (and, for a block registered in the ladder with nothing behind it, it
  never could — which is why the ladder now carries no such name).

H53 is the entry that cost a day to the first and third of those. Ten
blocks read as dead and were the grounding line working; two more read as
dead and were a 30-day beat and a 100-day one, measured over an 18-day
table. Not one of the "reasoning blocks that never render" was a closed
gate.

So this module exists to make the classification a function rather than
an investigation. It is pure — no database, no settings object, no I/O —
so the interesting cases are cheap to test and
``scripts/block_firing_report.py`` is a thin shell around it.

**The refusal is the point.** :data:`CADENCES` names the blocks whose
gate is a clock rather than a per-turn roll, and a block whose cadence
exceeds the observation window comes back ``unobservable`` with no rate
attached. Quoting ``0 / 1,062`` for a monthly beat is worse than saying
nothing, because sample density reads as authority: 0-of-1,062 looks like
a more thorough refutation than 0-of-20 and is exactly as uninformative.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass


#: A block whose gate is a wall-clock cooldown, with the cadence in days
#: and the knob it comes from. Only blocks with an explicit time gate
#: belong here — a turn-counter cooldown (``user_expertise``'s 12 turns)
#: is observable inside any window that has turns in it, so listing it
#: would suppress a rate that is perfectly meaningful.
#:
#: Deliberately short and deliberately sourced. An invented cadence would
#: silence a real defect, which is the exact failure this guards against
#: pointed the other way.
CADENCES: dict[str, tuple[float, str]] = {
    # "She speaks about this once a month" — the render method's own
    # words. Read off ``memory.concept_reflection_cooldown_days``.
    "concept_learning_block": (30.0, "memory.concept_reflection_cooldown_days"),
    # The next unfired milestone is the binding one, and the gaps run
    # 7 → 30 → 100 → 180 → 365 days (``relationship._MILESTONES``). The
    # 100-day step is the first that outruns a month of telemetry.
    "milestone_block": (100.0, "relationship._MILESTONES (next step)"),
    # ``agent.conduct_notice_cooldown_days``.
    "conduct_notice_block": (7.0, "agent.conduct_notice_cooldown_days"),
    # ``agent.inside_joke_birth_cooldown_hours`` = 24h.
    "inside_joke_block": (1.0, "agent.inside_joke_birth_cooldown_hours"),
}

FIRES = "fires"
SUPPRESSED = "suppressed"
DISABLED = "disabled"
UNOBSERVABLE = "unobservable"
SILENT = "silent"

#: Verdicts that are *not* findings. A caller listing "what to look at"
#: should subtract these rather than re-deriving the list.
BENIGN: frozenset[str] = frozenset({FIRES, SUPPRESSED, DISABLED, UNOBSERVABLE})


@dataclass(frozen=True, slots=True)
class BlockVerdict:
    """One block's firing behaviour, and what its number means."""

    block: str
    tier: str
    verdict: str
    fired: int = 0
    turns: int = 0
    #: ``None`` when the rate would be misleading — an unobservable block
    #: has no honest rate, and callers must not substitute 0.0.
    rate: float | None = None
    avg_chars: float = 0.0
    #: Why, in one phrase, for the non-``fires`` verdicts.
    reason: str = ""

    @property
    def is_finding(self) -> bool:
        return self.verdict not in BENIGN

    def as_dict(self) -> dict[str, object]:
        return {
            "block": self.block,
            "tier": self.tier,
            "verdict": self.verdict,
            "fired": self.fired,
            "turns": self.turns,
            "rate": self.rate,
            "avg_chars": self.avg_chars,
            "reason": self.reason,
        }


def classify_block(
    block: str,
    *,
    tier: str,
    fired: int,
    turns: int,
    window_days: float,
    suppressed: Collection[str] = (),
    disabled: Collection[str] = (),
    cadences: Mapping[str, tuple[float, str]] | None = None,
    avg_chars: float = 0.0,
) -> BlockVerdict:
    """Classify one block. See :func:`classify_all` for the batch form.

    Order matters and is not arbitrary. **Firing wins over everything**:
    a block that rendered is not suppressed or disabled whatever the
    config says it should be, and if those disagree the config reading is
    the wrong one. After that, the cheap structural causes (disabled,
    suppressed) come before the statistical one, because a suppressed
    block is not "unobservable" — its zero is fully explained.
    """
    table = CADENCES if cadences is None else cadences
    fired = max(0, int(fired))
    turns = max(0, int(turns))

    if fired > 0:
        return BlockVerdict(
            block=block,
            tier=tier,
            verdict=FIRES,
            fired=fired,
            turns=turns,
            rate=round(fired / turns, 4) if turns else None,
            avg_chars=round(float(avg_chars), 1),
        )

    if block in disabled:
        return BlockVerdict(
            block=block, tier=tier, verdict=DISABLED, turns=turns,
            reason="master switch is off",
        )

    if block in suppressed:
        return BlockVerdict(
            block=block, tier=tier, verdict=SUPPRESSED, turns=turns,
            reason="replaced by the K16 grounding line",
        )

    entry = table.get(block)
    if entry is not None:
        days, source = entry
        if float(window_days) < float(days):
            return BlockVerdict(
                block=block, tier=tier, verdict=UNOBSERVABLE, turns=turns,
                reason=(
                    f"cadence is {days:g}d ({source}) but the window is "
                    f"only {float(window_days):.1f}d - no rate is honest"
                ),
            )

    return BlockVerdict(
        block=block, tier=tier, verdict=SILENT, turns=turns, rate=0.0,
        reason=f"never rendered in {turns} turns / {float(window_days):.1f}d",
    )


def classify_all(
    *,
    tier_of: Mapping[str, str],
    fired: Mapping[str, int],
    turns: int,
    window_days: float,
    suppressed: Collection[str] = (),
    disabled: Collection[str] = (),
    avg_chars: Mapping[str, float] | None = None,
    cadences: Mapping[str, tuple[float, str]] | None = None,
) -> list[BlockVerdict]:
    """Classify every registered block, in ladder order.

    ``tier_of`` is the ladder (``_BLOCK_TIER_OF``), so a block that has
    rows but is *not* registered is not silently reported — the caller
    can diff the two key sets itself, and that mismatch means the ladder
    and the recorder disagree, which is a different and worse bug.
    """
    sizes = avg_chars or {}
    return [
        classify_block(
            name,
            tier=tier,
            fired=int(fired.get(name, 0)),
            turns=turns,
            window_days=window_days,
            suppressed=suppressed,
            disabled=disabled,
            cadences=cadences,
            avg_chars=float(sizes.get(name, 0.0)),
        )
        for name, tier in tier_of.items()
    ]


def summarise(verdicts: Collection[BlockVerdict]) -> dict[str, int]:
    """Counts per verdict, for the header line."""
    out: dict[str, int] = {}
    for v in verdicts:
        out[v.verdict] = out.get(v.verdict, 0) + 1
    return out


__all__ = [
    "BENIGN",
    "CADENCES",
    "DISABLED",
    "FIRES",
    "SILENT",
    "SUPPRESSED",
    "UNOBSERVABLE",
    "BlockVerdict",
    "classify_all",
    "classify_block",
    "summarise",
]
