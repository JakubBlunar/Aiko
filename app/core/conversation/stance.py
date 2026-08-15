"""K92 phase 1 — one named decision per turn, recorded and not rendered.

Aiko has a dozen mechanisms that each independently decide whether to
hand the model a sentence encouraging her to bring something up, and
none of them knows the others exist. Measured over the last 432 turns:
44% of turns carry exactly one such steer, 40% carry two, 16% carry
three or four, and ``wants_block`` alone is present on 85%. A signal
that is present almost always is not a decision, which is why the model
falls back on the overwhelming prior of answering the last message.

This module does not fix that. It *names* it. Given what the providers
actually offered on a turn plus what the user's turn was doing, it picks
one stance from a closed set and records the choice next to the turn.
Nothing it returns reaches the prompt in this phase.

Two absences from the current arrangement shape the set. **Following has
no representation at all** -- it is the null case, zero characters, so
her most common behaviour is also her least characterised one. And
**nothing lets her hold back**: every existing mechanism is a permission
to speak, so accumulated cues are unidirectional pressure to act.
``FOLLOW`` and ``HOLD`` therefore exist here despite having no provider
behind them, and finding out how often they would be chosen is most of
what phase 1 is for.

The design has three moving parts:

``desire``
    The most floor-taking stance any provider offered, read off the
    blocks that rendered. This is the *supply* side.

``ceiling``
    The most floor-taking stance the user's turn permits. K95's rule:
    interruption cost is a **hard filter on the candidate set, not
    another weight**, because a score can be outvoted by an accumulated
    want and that is exactly the regression worth insuring against.

``stance``
    ``min(desire, ceiling)`` on the ladder below.

Recording ``desire`` and ``ceiling`` separately is the point rather than
bookkeeping: their disagreement is the first direct measurement of how
often Aiko is being pushed to take the floor at a moment when taking it
would read badly.

**Deliberately derivable from stored data alone.** Every input comes
from ``turn_prompt_blocks``, ``cue_decisions`` and ``messages``, so the
arbiter can be replayed over history instead of only accumulating
forward. K90 could not be backfilled -- its inputs lived in a telemetry
object for the length of a turn -- and waiting weeks to find out whether
the eight-stance set was even the right set would have been the whole
cost of this phase.
"""
from __future__ import annotations

from dataclasses import dataclass


# ── the closed set, ordered by how much of the floor the move takes ──
#
# The ladder is the mechanism, not documentation: ``min`` over it is how
# the interruption ceiling clamps the providers' desire, so reordering
# these changes behaviour.

HOLD = "HOLD"                    # under-respond on purpose
FOLLOW = "FOLLOW"                # his subject, nothing of hers added
FOLLOW_AND_ADD = "FOLLOW_AND_ADD"  # his subject, plus something of hers
ASK = "ASK"                      # a question of her own on the table
CALLBACK = "CALLBACK"            # reach back to something already theirs
SHARE = "SHARE"                  # her own material, unprompted
REDIRECT = "REDIRECT"            # off his subject onto another
INITIATE = "INITIATE"            # open a new subject with the floor

STANCE_LADDER: tuple[str, ...] = (
    HOLD,
    FOLLOW,
    FOLLOW_AND_ADD,
    ASK,
    CALLBACK,
    SHARE,
    REDIRECT,
    INITIATE,
)

_RANK: dict[str, int] = {name: i for i, name in enumerate(STANCE_LADDER)}


# ── supply: which rendered block offers which stance ─────────────────
#
# Keys are ``_PROMPT_BLOCK_TIERS`` names exactly as they appear in
# ``turn_prompt_blocks``. A typo here would be silently invisible -- the
# arbiter would just never see that offer -- so
# ``tests/test_stance.py`` asserts every name below is registered.

_OFFERS: dict[str, tuple[str, ...]] = {
    # K53's explicit "this turn is yours" is the only block in the
    # system that already means INITIATE.
    INITIATE: (
        "initiative_block",
    ),
    # Both of these say the current subject is spent, which is a
    # different move from having something of her own to raise.
    REDIRECT: (
        "topic_appetite_block",
        "stagnation_block",
    ),
    # Her own material, unprompted: what she did while away, what she
    # has been mulling, what she leans toward, what she thinks.
    SHARE: (
        "turning_over_block",
        "sleep_return_block",
        "caught_mid_activity_block",
        "away_activities_block",
        "idle_seeds_block",
        "narrative_block",
        "pursuit_lean_block",
        "taste_lean_block",
        "opinion_injection_block",
        "interest_drift_block",
        "associative_wander_block",
    ),
    # Reaching back to something that is already between the two of
    # them. Lower on the ladder than SHARE because shared history is a
    # softer thing to spend the floor on than a subject only she holds.
    CALLBACK: (
        "thread_ownership_block",
        "self_callback_block",
        "long_arc_callback_block",
        "follow_up_block",
        "inside_joke_block",
        "shared_ritual_block",
        "anniversary_block",
        "growth_witness_block",
    ),
    # Putting a question of her own on the table.
    ASK: (
        "curiosity_seeds_block",
        "knowledge_gaps_block",
        "knowledge_gap_notice_block",
        "forward_curiosity_block",
        "concept_hypothesis_block",
        "wellbeing_concern_block",
        "dormant_interest_block",
        "curiosity_gradient_block",
        "absence_curiosity_block",
        "aspiration_momentum_block",
        "tension_block",
    ),
    # Answer him, then add. The soft-want band lives here, which is why
    # this stance is expected to dominate: ``wants_block`` renders on
    # 85% of turns.
    FOLLOW_AND_ADD: (
        "wants_block",
        "tease_ledger_block",
        "appreciation_block",
        "reciprocal_vulnerability_block",
        "concept_learning_block",
        "conduct_notice_block",
        "novelty_block",
    ),
}

# Flattened once at import: block name -> the stance it offers.
_OFFER_OF: dict[str, str] = {
    block: stance
    for stance, blocks in _OFFERS.items()
    for block in blocks
}


# ── demand: what the user's turn permits ─────────────────────────────

# Arcs in which taking the floor is wrong outright. Same pair K53
# blocks on, deliberately: two modules disagreeing about when Aiko may
# steer would be worse than either rule alone.
_PROTECTED_ARCS = frozenset({"support", "reflection"})

# Matches ``initiative_director.decide``'s ``substantial_chars``. Shared
# so the ceiling and K53's own escape hatch cannot drift apart.
SUBSTANTIAL_CHARS = 240

# Under this, with no other signal, his turn is a beat rather than a
# contribution -- the one place a deliberate under-response is a real
# option rather than a failure to engage.
_BACKCHANNEL_CHARS = 25


@dataclass(frozen=True, slots=True)
class StanceInputs:
    """Everything the arbiter reads. All of it is already persisted."""

    blocks: frozenset[str] = frozenset()
    user_text: str = ""
    dialogue_act: str | None = None
    arc: str | None = None


@dataclass(frozen=True, slots=True)
class StanceDecision:
    """One turn's stance, and enough context to argue with it later.

    ``shortlist`` is an ordered tuple of ``(stance, block)`` for every
    offer on the table, most floor-taking first. K92 calls for an
    ordered shortlist with one reason each **rather than floats** --
    an LLM comparing 0.63 against 0.58 is doing the one kind of
    reasoning it is worst at, and per-turn numerals would churn the T6
    prefix for no gain. Phase 2 renders from this; phase 1 logs it.
    """

    stance: str
    reason: str
    desire: str
    ceiling: str
    shortlist: tuple[tuple[str, str], ...] = ()

    @property
    def clamped(self) -> bool:
        """True when the user's turn held her back from what was offered."""
        return _RANK[self.desire] > _RANK[self.ceiling]

    def shortlist_text(self) -> str:
        """``STANCE:block`` pairs, comma-joined, for the log column."""
        return ",".join(f"{s}:{b}" for s, b in self.shortlist)


def _is_direct_question(inputs: StanceInputs) -> bool:
    """Did he actually ask something, as opposed to merely wondering?

    The dialogue-act tag is the better signal but it is regex-first and
    folds soft requests in, so a trailing question mark is checked too:
    the cost of missing a real question here is her talking over it,
    which is the failure this whole ceiling exists to prevent.
    """
    if (inputs.dialogue_act or "").strip().lower() == "question":
        return True
    return (inputs.user_text or "").rstrip().endswith("?")


def compute_ceiling(inputs: StanceInputs) -> tuple[str, str]:
    """The most floor-taking stance his turn permits, and why.

    Every applicable cap is evaluated and the **most restrictive** wins,
    so the reason names the binding constraint rather than the first one
    checked. A hard filter, not a weight: an accumulated want must not
    be able to outvote a direct question.
    """
    act = (inputs.dialogue_act or "").strip().lower()
    caps: list[tuple[str, str]] = []

    if act == "vent":
        # He is not looking for a contribution. K69's read, applied to
        # turn-taking rather than to tone.
        caps.append((FOLLOW, "vent"))
    if (inputs.arc or "").strip().lower() in _PROTECTED_ARCS:
        caps.append((FOLLOW_AND_ADD, "arc_protected"))
    if _is_direct_question(inputs):
        caps.append((FOLLOW_AND_ADD, "direct_question"))
    if act == "planning":
        # Working through something with her; hijacking it is the same
        # error as talking over a question.
        caps.append((FOLLOW_AND_ADD, "planning"))
    if len((inputs.user_text or "").strip()) >= SUBSTANTIAL_CHARS:
        caps.append((FOLLOW_AND_ADD, "user_substantial"))

    if not caps:
        return INITIATE, "open"
    return min(caps, key=lambda c: _RANK[c[0]])


def build_shortlist(blocks: frozenset[str]) -> tuple[tuple[str, str], ...]:
    """Every offer on the table, most floor-taking first.

    One entry per *stance*, not per block: two curiosity cues both
    offering ASK is one option with two backers, and listing it twice
    would imply a weight the arbiter does not have. The block kept is
    the alphabetically first, purely so the record is stable across
    runs and diffs cleanly.
    """
    best: dict[str, str] = {}
    for block in sorted(blocks):
        stance = _OFFER_OF.get(block)
        if stance is not None and stance not in best:
            best[stance] = block
    return tuple(
        (stance, best[stance])
        for stance in sorted(best, key=lambda s: -_RANK[s])
    )


def decide(inputs: StanceInputs) -> StanceDecision:
    """Pick one stance for the turn. Pure; no I/O, no session state.

    ``HOLD`` is the one stance no provider can offer, so it is reached
    by a rule rather than by the ladder: nothing was on the table and
    his turn was a short beat with no question in it. That rule is a
    guess -- it is the least evidenced thing in this module, and how
    often it fires against how those turns actually read is precisely
    what phase 1 is meant to settle before anything renders.
    """
    ceiling, ceiling_reason = compute_ceiling(inputs)
    shortlist = build_shortlist(inputs.blocks)

    if not shortlist:
        text = (inputs.user_text or "").strip()
        if len(text) < _BACKCHANNEL_CHARS and not _is_direct_question(inputs):
            return StanceDecision(
                stance=HOLD,
                reason="no_offer_backchannel",
                desire=HOLD,
                ceiling=ceiling,
                shortlist=(),
            )
        return StanceDecision(
            stance=FOLLOW,
            reason="no_offer",
            desire=FOLLOW,
            ceiling=ceiling,
            shortlist=(),
        )

    desire, desire_block = shortlist[0]
    if _RANK[desire] <= _RANK[ceiling]:
        return StanceDecision(
            stance=desire,
            reason=desire_block,
            desire=desire,
            ceiling=ceiling,
            shortlist=shortlist,
        )
    # Clamped. The reason names the constraint that bound rather than
    # the offer that lost, because the constraint is the thing a reader
    # of this row will want to argue with.
    return StanceDecision(
        stance=ceiling,
        reason=ceiling_reason,
        desire=desire,
        ceiling=ceiling,
        shortlist=shortlist,
    )


__all__ = [
    "ASK",
    "CALLBACK",
    "FOLLOW",
    "FOLLOW_AND_ADD",
    "HOLD",
    "INITIATE",
    "REDIRECT",
    "SHARE",
    "STANCE_LADDER",
    "SUBSTANTIAL_CHARS",
    "StanceDecision",
    "StanceInputs",
    "build_shortlist",
    "compute_ceiling",
    "decide",
]
