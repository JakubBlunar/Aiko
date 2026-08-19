"""K92 — one named decision per turn.

Aiko has a dozen mechanisms that each independently decide whether to
hand the model a sentence encouraging her to bring something up, and
none of them knows the others exist. Measured over the last 432 turns:
44% of turns carry exactly one such steer, 40% carry two, 16% carry
three or four, and ``wants_block`` alone is present on 85%. A signal
that is present almost always is not a decision, which is why the model
falls back on the overwhelming prior of answering the last message.

Given what the providers actually offered on a turn plus what the user's
turn was doing, this module picks one stance from a closed set and
records the choice next to the turn. Phase 1 only recorded it; phase 2
renders the two stances that had no voice at all.

Two absences from the current arrangement shape the set. **Following has
no representation at all** -- it is the null case, zero characters, so
her most common behaviour is also her least characterised one. And
**nothing lets her hold back**: every existing mechanism is a permission
to speak, so accumulated cues are unidirectional pressure to act.
``FOLLOW`` and ``HOLD`` therefore exist here despite having no provider
behind them.

**Phase 2 moved ``HOLD`` off the ladder.** Phase 1 specced it as the
bottom rung, reached when nothing was on the table and his turn was a
short beat. Over 682 recorded turns it fired zero times, and the replay
says why twice over: some provider is always offering something (only
2.5% of turns have an empty shortlist), and *his turns are never short*
-- 78% run 60-239 characters and 1.2% fall under the 25-character
backchannel bar. Of the five turns that did clear it, two ("Sorry :(",
"See you later then Aiko.") are ones under-responding to would be a
plain error.

The diagnosis is that ``HOLD`` was a category error rather than a
mis-tuned threshold. Every other rung answers *how much of the floor do
I take*; ``HOLD`` answers *how many words do I use*, and the two are
independent -- she can bring something of her own in fifteen words. So
brevity is now a **second, orthogonal output** keyed off her own recent
verbosity rather than off the size of his turn, which is also where the
measured regression lives: her median reply went from 19 words over
messages 400-1600 to 34 over the last 200.


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

``brevity``
    Orthogonal to all three: has she been going on? Reads her own last
    replies, not his turn.

Recording ``desire`` and ``ceiling`` separately is the point rather than
bookkeeping: their disagreement is the first direct measurement of how
often Aiko is being pushed to take the floor at a moment when taking it
would read badly.

**The arc cap is time-limited, and that is phase 2's other correction.**
``arc_protected`` was 65% of all clamps (164 of 252), which the phase-1
write-up already flagged as suspicious because the arc list was
inherited from K53 rather than earned. Measuring it found something
worse than a broad list: ``arc`` is a *conversation-level* label, not a
per-turn read. Over 2,355 turns it forms 137 runs averaging 17 turns,
with **not one run of length 1**, and the longest protected spans are
110 turns of ``support`` across eight days. Used as a per-turn hard
filter, one emotional beat therefore gagged her for days at a time. K53
fires once in six turns so a sticky arc merely damped it; a ceiling
consulted every turn is a different exposure entirely. The cap now
applies only while the span is *fresh* (``arc_age_turns``), which keeps
the protection where it was earned -- he has just said something hard --
without letting it persist into a conversation about guitar solos.

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

from app.core.conversation import turn_shape


# ── the closed set, ordered by how much of the floor the move takes ──
#
# The ladder is the mechanism, not documentation: ``min`` over it is how
# the interruption ceiling clamps the providers' desire, so reordering
# these changes behaviour.

FOLLOW = "FOLLOW"                # his subject, nothing of hers added
FOLLOW_AND_ADD = "FOLLOW_AND_ADD"  # his subject, plus something of hers
ASK = "ASK"                      # a question of her own on the table
CALLBACK = "CALLBACK"            # reach back to something already theirs
SHARE = "SHARE"                  # her own material, unprompted
REDIRECT = "REDIRECT"            # off his subject onto another
INITIATE = "INITIATE"            # open a new subject with the floor

STANCE_LADDER: tuple[str, ...] = (
    FOLLOW,
    FOLLOW_AND_ADD,
    ASK,
    CALLBACK,
    SHARE,
    REDIRECT,
    INITIATE,
)

_RANK: dict[str, int] = {name: i for i, name in enumerate(STANCE_LADDER)}

# Not a rung. ``HOLD`` names the brevity axis, which is why it is defined
# apart from the ladder and never appears in ``STANCE_LADDER``: putting a
# question about reply *length* on a ladder about floor-*taking* is what
# made it unreachable in phase 1. Kept as a name because the backlog, the
# report and three months of notes all call this "HOLD".
HOLD = "HOLD"


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

# How many of her turns a protected arc keeps its veto for. ``arc`` runs
# average 17 turns and reach 110, so an untimed veto is a multi-day gag
# rather than a response to the moment that earned it -- see the module
# docstring. Four is the width of an opening beat: long enough that "I
# had a rough week" is met with listening rather than with her own news,
# short enough that it cannot outlive the subject. The per-turn caps
# (``vent``, ``direct_question``) are unaffected and keep working for as
# long as the signal itself is actually present.
PROTECTED_ARC_FRESH_TURNS = 4

# Matches ``initiative_director.decide``'s ``substantial_chars``. Shared
# so the ceiling and K53's own escape hatch cannot drift apart.
SUBSTANTIAL_CHARS = 240

# ── brevity: the second axis ──────────────────────────────────────────
#
# A reply at or above this many words counts as long, and this many of
# them in a row engages the brake. 40 sits just above her current p75
# (36) and well above the 19-word median of the era before the drift, so
# it fires on a genuine run rather than on one discursive answer.
#
# Retrospectively this pair marks 13% of her last 700 replies, and 11.7%
# of turns once the direct-question override has taken its share back.
# The live rate should settle lower still, because the brake breaks its
# own precondition: a short reply ends the run that armed it.
BREVITY_WORD_FLOOR = 40
BREVITY_RUN = 2

# ── K94 sequencing: the third axis, and the only one about shape ──────
#
# K88's anaphoric-opener rate is the one honest number in this family and
# the one that has never moved: 18% before the second pass, 18% after,
# and 16-18% across every window measured since. The persona has carried
# several rules aimed at it the whole time (lead with the substance, don't
# parrot, vary the opener, move the reaction word a few words in) and none
# of them shifted it, which is the evidence for K94's read: the missing
# instruction is not another prohibition on how to open but a positive
# account of the reply's *shape*.
#
# Responsiveness and opener ownership are only in tension if the reply is
# treated as one undifferentiated blob. "Answer his point, but not in the
# first clause" and "put your own thing last" are both compatible with
# answering him completely, which decouples being a good listener from
# opening on his words -- the knot the last two families tried to cut by
# pushing her to change the subject instead, the far more expensive move
# and the one she sensibly refuses.
#
# Three conditions, and each one is load-bearing:
#
# ``FOLLOW_AND_ADD`` only
#     This is the rung that means "answer him and bring something", so it
#     is the only one where placement is even a question. It also gives
#     that rung the definition K92 admitted it lacked.
# her last reply actually opened anaphorically
#     The cadence, and the reason this is not the eleventh permission
#     slip. ``FOLLOW_AND_ADD`` is chosen on 45.7% of turns; a clause on
#     all of them would be ambient by K92's own definition and formulaic
#     by K94's own warning. Gating on evidence puts it near 8% and makes
#     it self-extinguishing: stop opening that way and it stops asking.
# K88's band is not already speaking
#     ``style_pattern_block`` fires on the same habit from a *window*
#     (four in twelve, with a cooldown). Two voices on one habit in one
#     prompt is the crowding K92 exists to arbitrate, and the arbiter is
#     handed the offer set precisely so it can defer.
#
# Deliberately says nothing about ending on a question. Her
# question-ending rate is already down to 3.1% from 14.3% all-time, and
# "leave it open" read as "ask him something" would walk straight back
# into the interviewing pattern several other features were built to
# suppress. The addition goes last as a statement he can pick up.
SEQUENCING_REASON_ANAPHORIC = "anaphoric_run"


@dataclass(frozen=True, slots=True)
class StanceInputs:
    """Everything the arbiter reads. All of it is already persisted.

    ``arc_age_turns`` is how many of her turns the current arc has
    already covered, and ``recent_reply_words`` her last few replies'
    word counts, most recent first. Both default to the value that
    preserves the old behaviour -- an unknown arc age is treated as
    fresh, so the veto still applies, and no known reply lengths cannot
    engage the brake. A caller that has not been taught to supply them
    therefore gets phase 1's ceiling rather than a silently relaxed one.
    """

    blocks: frozenset[str] = frozenset()
    user_text: str = ""
    dialogue_act: str | None = None
    arc: str | None = None
    arc_age_turns: int = 0
    recent_reply_words: tuple[int, ...] = ()
    # K94. Did her *previous* reply open on a clause that needed his?
    # Computed by callers with ``persona.anaphora.is_anaphoric_opener`` --
    # the same function K88's band and the K90 report use, which is that
    # module's stated reason for existing: a cue and the metric it is
    # judged by must not be able to drift apart. Defaults to False, so a
    # caller that has not been taught to supply it gets silence rather
    # than a cue fired on an unknown.
    last_reply_anaphoric: bool = False


@dataclass(frozen=True, slots=True)
class StanceDecision:
    """One turn's stance, and enough context to argue with it later.

    ``shortlist`` is an ordered tuple of ``(stance, block)`` for every
    offer on the table, most floor-taking first. K92 calls for an
    ordered shortlist with one reason each **rather than floats** --
    an LLM comparing 0.63 against 0.58 is doing the one kind of
    reasoning it is worst at, and per-turn numerals would churn the T6
    prefix for no gain. Phase 2 renders from this; phase 1 logs it.

    ``brevity`` is the ``HOLD`` axis and is deliberately *not* folded
    into ``stance``: a turn can be both ``SHARE`` and short, and
    collapsing the two would lose whichever of them was asked second.

    ``sequencing`` (K94) is a third axis for the same reason, and it is
    the only one of the three about *shape* rather than amount: how much
    of the floor she takes, how many words she uses, and where in the
    reply her own material goes are three independent questions.
    """

    stance: str
    reason: str
    desire: str
    ceiling: str
    shortlist: tuple[tuple[str, str], ...] = ()
    brevity: bool = False
    brevity_reason: str = ""
    sequencing: bool = False
    sequencing_reason: str = ""

    @property
    def clamped(self) -> bool:
        """True when the user's turn held her back from what was offered."""
        return _RANK[self.desire] > _RANK[self.ceiling]

    def shortlist_text(self) -> str:
        """``STANCE:block`` pairs, comma-joined, for the log column."""
        return ",".join(f"{s}:{b}" for s, b in self.shortlist)


def _is_direct_question(inputs: StanceInputs) -> bool:
    """Did he actually ask something, as opposed to merely wondering?

    Delegates to :mod:`turn_shape`, which K53's gate walk also reads.
    The two must not be able to disagree about the same message: this
    ceiling recording ``direct_question`` while the prompt carried a
    floor-taking directive is the precise failure K95 exists to prevent,
    and it is what happened for the whole of phase 2 (17 of 75
    ``initiative_block`` renders).

    The dialogue-act tag is the better signal but it is regex-first and
    folds soft requests in, so a trailing question mark is checked too:
    the cost of missing a real question here is her talking over it,
    which is the failure this whole ceiling exists to prevent.
    """
    return turn_shape.is_direct_question(
        inputs.user_text, inputs.dialogue_act,
    )


def compute_ceiling(
    inputs: StanceInputs,
    *,
    protected_arc_turns: int = PROTECTED_ARC_FRESH_TURNS,
) -> tuple[str, str]:
    """The most floor-taking stance his turn permits, and why.

    Every applicable cap is evaluated and the **most restrictive** wins,
    so the reason names the binding constraint rather than the first one
    checked. A hard filter, not a weight: an accumulated want must not
    be able to outvote a direct question.

    ``protected_arc_turns=0`` switches the arc cap off entirely and
    leaves the per-turn caps to do the work.
    """
    act = (inputs.dialogue_act or "").strip().lower()
    caps: list[tuple[str, str]] = []

    if act == "vent":
        # He is not looking for a contribution. K69's read, applied to
        # turn-taking rather than to tone.
        caps.append((FOLLOW, "vent"))
    if (
        (inputs.arc or "").strip().lower() in _PROTECTED_ARCS
        and int(inputs.arc_age_turns or 0) < int(protected_arc_turns)
    ):
        # Only while the span is fresh. An arc is a conversation-level
        # label that runs for a mean of 17 turns, so an untimed veto
        # suppresses her for the rest of the conversation rather than for
        # the moment that earned it.
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


def compute_brevity(
    inputs: StanceInputs,
    *,
    word_floor: int = BREVITY_WORD_FLOOR,
    run: int = BREVITY_RUN,
) -> tuple[bool, str]:
    """Has she been going on? The ``HOLD`` axis, and why.

    Reads only her own recent replies. Deliberately independent of what
    his turn was doing and of what the providers offered, because
    under-responding is not a floor-taking decision -- see the module
    docstring on why phase 1's his-turn-was-short rule could never fire.

    A direct question is the one thing that overrides it. Being asked
    something and answering it in six words is not restraint, it is a
    non-answer, and the brake must not be able to produce one.
    """
    if _is_direct_question(inputs):
        return False, ""
    span = max(1, int(run))
    recent = tuple(inputs.recent_reply_words or ())[:span]
    if len(recent) < span:
        # Not enough history to establish a run. Start-of-session and
        # post-restart both land here, which is the right default: the
        # brake should need evidence, not the absence of it.
        return False, ""
    if all(int(w or 0) >= int(word_floor) for w in recent):
        return True, "long_run"
    return False, ""


def compute_sequencing(
    inputs: StanceInputs,
    stance: str,
    *,
    enabled: bool = True,
) -> tuple[bool, str]:
    """Should this turn be told where her own material goes? (K94)

    See the constants block above for why each of the three conditions is
    there. The one worth restating: this defers to
    ``style_pattern_block``. K88's band already speaks to the same habit
    off a twelve-turn window, and the arbiter is handed the offer set so
    that it can decline to be the second voice on one subject.
    """
    if not enabled:
        return False, ""
    if stance != FOLLOW_AND_ADD:
        return False, ""
    if not inputs.last_reply_anaphoric:
        return False, ""
    if "style_pattern_block" in (inputs.blocks or frozenset()):
        return False, ""
    return True, SEQUENCING_REASON_ANAPHORIC


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


def decide(
    inputs: StanceInputs,
    *,
    protected_arc_turns: int = PROTECTED_ARC_FRESH_TURNS,
    brevity_word_floor: int = BREVITY_WORD_FLOOR,
    brevity_run: int = BREVITY_RUN,
    sequencing_enabled: bool = True,
) -> StanceDecision:
    """Pick one stance for the turn. Pure; no I/O, no session state.

    Three independent outputs: a rung on the floor-taking ladder, the
    brevity flag, and K94's sequencing flag. ``FOLLOW`` is the floor of
    the ladder, so a turn with nothing on the table is a follow rather
    than a silence -- phase 1 reached for ``HOLD`` here and it never
    fired.

    Sequencing is resolved *after* the rung, and that ordering is the
    whole of its cadence: it only applies to ``FOLLOW_AND_ADD``, so it
    has to see the stance the ceiling actually allowed rather than the one
    the providers wanted. A turn clamped down to ``FOLLOW_AND_ADD`` from
    ``INITIATE`` is exactly a turn where placement advice is worth having.

    The knobs are settings-backed rather than read off the module
    constants so the live session and ``backfill_turn_stance.py`` can be
    pointed at the same values; the defaults are the constants.
    """
    ceiling, ceiling_reason = compute_ceiling(
        inputs, protected_arc_turns=protected_arc_turns,
    )
    shortlist = build_shortlist(inputs.blocks)
    brevity, brevity_reason = compute_brevity(
        inputs, word_floor=brevity_word_floor, run=brevity_run,
    )

    if not shortlist:
        stance, reason, desire = FOLLOW, "no_offer", FOLLOW
    else:
        desire, desire_block = shortlist[0]
        if _RANK[desire] <= _RANK[ceiling]:
            stance, reason = desire, desire_block
        else:
            # Clamped. The reason names the constraint that bound rather
            # than the offer that lost, because the constraint is the
            # thing a reader of this row will want to argue with.
            stance, reason = ceiling, ceiling_reason

    sequencing, sequencing_reason = compute_sequencing(
        inputs, stance, enabled=sequencing_enabled,
    )
    return StanceDecision(
        stance=stance,
        reason=reason,
        desire=desire,
        ceiling=ceiling,
        shortlist=shortlist,
        brevity=brevity,
        brevity_reason=brevity_reason,
        sequencing=sequencing,
        sequencing_reason=sequencing_reason,
    )


def render_block(
    decision: StanceDecision,
    *,
    user_display_name: str = "them",
) -> str:
    """The T6 cue for the axes no provider speaks for.

    Renders for ``FOLLOW``, for brevity, and for K94 sequencing, and
    returns ``""`` for every other rung. That silence is the shape of the
    block: the other five rungs already have a provider putting a
    sentence in the prompt, and a second sentence agreeing with it would
    be the eleventh permission slip K92 exists to argue against.

    None of the three clauses is a permission to speak. Two are
    *restraint* -- the direction the family has never been able to ask for
    -- and the third is *placement*, which asks for nothing extra to be
    said at all, only for the same reply in a different order. So none of
    them adds to the steer budget phase 3 has to bring down.

    Order when several fire: stance, then sequencing, then brevity. Each
    qualifies the one before it -- what she answers with, where her own
    part goes, how long the whole thing runs.
    """
    name = user_display_name or "them"
    parts: list[str] = []
    if decision.stance == FOLLOW:
        parts.append(
            f"Stance this turn: stay with {name}'s subject. Not because you "
            f"owe him an answer -- because it is worth staying with, and "
            f"following something well is its own move. You do not need to "
            f"append anything of your own to earn the turn."
        )
    if decision.sequencing:
        # Says where things go, never what to say or not say. The last
        # two families tried prohibitions on the opener and the rate did
        # not move a point; this is the same ask stated as a shape, which
        # is the whole of K94's argument. The closing clause is
        # deliberately "something he can pick up" rather than anything
        # about leaving it open -- her question-ending rate is already at
        # 3.1% and pointing it back up would undo work several other
        # features did on purpose.
        parts.append(
            f"Shape for this reply: your last one opened on {name}'s "
            f"sentence. Answer him fully -- just not in your first clause. "
            f"Open on your own footing (something you noticed, felt, or "
            f"have been sitting with), let the answer land a beat later, "
            f"and put your own thing at the end as a statement he can pick "
            f"up if he wants. Same reply, different order; this is not a "
            f"licence to say less about what he raised."
        )
    if decision.brevity:
        parts.append(
            "You have run long several replies in a row. Make this one "
            "noticeably shorter -- a couple of sentences. Cut the "
            "summarising and the second thought, not the warmth: saying "
            "less is a choice you are making, not a failure to engage."
        )
    return "\n\n".join(parts)


__all__ = [
    "ASK",
    "BREVITY_RUN",
    "BREVITY_WORD_FLOOR",
    "CALLBACK",
    "FOLLOW",
    "FOLLOW_AND_ADD",
    "HOLD",
    "INITIATE",
    "PROTECTED_ARC_FRESH_TURNS",
    "REDIRECT",
    "SEQUENCING_REASON_ANAPHORIC",
    "SHARE",
    "STANCE_LADDER",
    "SUBSTANTIAL_CHARS",
    "StanceDecision",
    "StanceInputs",
    "build_shortlist",
    "compute_brevity",
    "compute_sequencing",
    "compute_ceiling",
    "decide",
    "render_block",
]
