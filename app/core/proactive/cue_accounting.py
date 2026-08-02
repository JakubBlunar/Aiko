"""Did the cue this worker produced ever reach Aiko? (G4)

There are 50-odd workers on the
:class:`~app.core.proactive.idle_worker_scheduler.IdleWorkerScheduler`,
many of them making LLM calls, and until now nothing could answer the only
question that matters about any of them. We could see that a worker **ran**
-- ``get_idle_workers_status`` reports overdue seconds, a duration EMA,
error counts -- but not that it **mattered**.

The gap is structural rather than a missing dashboard. A worker writes a
finding to a ``kv_meta`` journal ring; a T6 provider later decides whether
to render it, usually behind a topic gate that returns ``""`` when the live
conversation has moved on; the four gap cues run a priority mutex where
only one may fire; ``_question_balance_suppressed`` vetoes several more.
Every one of those is a legitimate design decision, and every one of them
discards work **without leaving a trace** -- so a worker whose gate never
matches is indistinguishable from one quietly doing its job. Cooldowns and
daily caps are all hand-picked constants with no evidence behind them,
LLM-calling workers can burn budget producing cues that are structurally
unreachable, and there is no way to retire a cue type that does not work.

The armed-to-surfaced ratio is the diagnostic that did not exist
------------------------------------------------------------------
"Armed" here means *there was material waiting* -- not that a worker ran.
That distinction is the whole point: a worker that runs every ten minutes
and writes a finding every time is not producing nine wasted findings, it
is producing one that gets through and eight that were superseded.

Rather than instrument ~50 workers at their write sites, arming is read
back out of the state the providers themselves consult: the journal ring
and its ``<feature>.last_surfaced_at`` watermark. If the newest ring entry
is not the one the watermark names, there is unsurfaced material -- the
cue is armed. This is deliberately the *provider's own* definition of
"something to say", so the ratio cannot drift away from what the provider
actually saw.

Two arming paths, because there are two kinds of cue
----------------------------------------------------
- **Journal-backed** (most of them) -- a ring in ``kv_meta`` plus a
  watermark, as above.
- **Slot-backed** (the four gap cues) -- ``turning_over``,
  ``sleep_return``, ``away_activities`` and ``forward_curiosity`` are armed
  post-turn by writing a gap duration onto an in-memory
  ``_pending_*_seconds`` attribute. Nothing is journalled, so arming is
  read straight off the session.

``away_activities`` and ``forward_curiosity`` are **both** -- a slot arms
the opportunity and a journal supplies the content -- so they are armed
only when both agree, which matches what their providers require to fire.

On the gap-cue mutex, which is not a lottery
--------------------------------------------
The backlog called this a "one-of lottery". It is not: it is a
deterministic priority order (``turning_over`` -> ``sleep_return`` ->
``away_activities`` -> ``forward_curiosity``) enforced by the shared
``_gap_cue_surfaced`` flag. So the loser is not random -- the *same* cue
loses every time both are armed, which is a systematic bias and a far more
actionable finding than noise. Recording ``lost_priority`` against the
winner's name is what makes it visible.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("app.cue_accounting")


# ── outcomes and decline reasons ──────────────────────────────────────

OUTCOME_SURFACED = "surfaced"
OUTCOME_DECLINED = "declined"

# Structural declines -- the cue had something to say and the *assembly*
# refused it, for reasons visible from outside the provider. These are the
# ones worth having first: they are the machinery overruling the content,
# which is exactly the class of decision that was invisible.
REASON_LOST_PRIORITY = "lost_priority"
REASON_QUESTION_BALANCE = "question_balance"
# The provider declined for its own reasons (topic gate, cooldown, no
# candidate cleared the picker). A catch-all until the per-provider sweep
# lands, so an armed-but-unsurfaced cue is never silently unaccounted.
REASON_PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class CueSpec:
    """How to tell whether one cue had something to say.

    ``journal_key`` / ``watermark_key`` describe the ring-plus-watermark
    pattern; ``slot_attr`` names the in-memory pending attribute. A cue may
    set either or both -- when both are set, both must indicate material,
    matching what the provider itself requires.
    """

    name: str
    journal_key: str = ""
    watermark_key: str = ""
    slot_attr: str = ""
    # True for the four cues that contend for the single gap-cue slot, in
    # the priority order they appear in ``GAP_CUE_ORDER``.
    gap_cue: bool = False


# The four gap cues in the order the assembler runs them. Order is the
# mechanism, not documentation -- earlier entries win the mutex.
GAP_CUE_ORDER: tuple[str, ...] = (
    "turning_over",
    "sleep_return",
    "away_activities",
    "forward_curiosity",
)


# Cue name -> how to detect arming. Names match the ``_PROMPT_BLOCK_TIERS``
# entry minus the ``_block`` suffix, so the ledger keys line up with the
# prompt-cost view and a rename shows up in both.
CUE_SPECS: dict[str, CueSpec] = {
    spec.name: spec
    for spec in (
        # Gap cues: slot-armed, and two of them also need journal content.
        CueSpec(
            "turning_over",
            slot_attr="_pending_turning_over_seconds",
            gap_cue=True,
        ),
        CueSpec(
            "sleep_return",
            slot_attr="_pending_sleep_return_seconds",
            gap_cue=True,
        ),
        CueSpec(
            "away_activities",
            journal_key="aiko.away_activities",
            watermark_key="away_activity.last_surfaced_at",
            slot_attr="_pending_away_activities_seconds",
            gap_cue=True,
        ),
        CueSpec(
            "forward_curiosity",
            journal_key="aiko.forward_curiosity",
            watermark_key="forward_curiosity.last_surfaced_at",
            slot_attr="_pending_forward_curiosity_seconds",
            gap_cue=True,
        ),
        # Journal-backed cues with an explicit watermark.
        CueSpec(
            "follow_up",
            journal_key="aiko.follow_up_cues",
            watermark_key="follow_up.last_surfaced_at",
        ),
        CueSpec(
            "growth_witness",
            journal_key="aiko.growth_witness",
            watermark_key="growth_witness.last_surfaced_at",
        ),
        CueSpec(
            "self_callback",
            journal_key="aiko.self_callback",
            watermark_key="self_callback.last_surfaced_at",
        ),
        # Pool-only: no ring, no slot. The post-turn detector writes
        # straight to ``cue_pool``, so a queued row is the whole of the
        # arming signal -- see the pure-pool branch in ``armed_cues``.
        CueSpec("self_correction"),
        # Same shape: banked on the turn path when a pushback or a light
        # offence lands, so a waiting row is the whole opportunity. Its
        # rows also sit under a ``not_before`` for the first hour, which
        # ``count_pending`` already honours -- a debt too fresh to be a
        # callback correctly reports as unarmed.
        CueSpec("tease_ledger"),
        # Also pool-only, and the one entry whose arming is knowably
        # under-reported: whether an aged memory relevant to this turn
        # exists is the *result* of the provider's vector search, so
        # there is nothing to inspect beforehand. A pending row means a
        # previous callback went unused and is worth a second offer,
        # which is the only arming signal that can be read cheaply.
        CueSpec("long_arc_callback"),
        CueSpec("shared_ritual"),
        CueSpec(
            "aspiration_momentum",
            journal_key="aiko.aspiration_momentum",
            watermark_key="aspiration_momentum.last_surfaced_at",
        ),
        # Pooled, so the stock answers and the watermark it used to carry
        # is gone; the ring stays as the no-store fallback, the same way
        # the five topic-gated cues below keep theirs.
        CueSpec("wellbeing_concern", journal_key="aiko.wellbeing_concern"),
        CueSpec(
            "tension",
            journal_key="aiko.tension_cue",
            watermark_key="tension_cue.last_surfaced_at",
        ),
        # These five used to dedupe by a per-topic key set rather than a
        # single watermark, so arming degraded to "the ring is non-empty"
        # and over-counted. They are pooled now and ``armed_cues`` reads
        # their stock instead, which is exact; the journal keys stay for
        # the case where no store is wired.
        CueSpec("interest_drift", journal_key="aiko.interest_drifts"),
        CueSpec("associative_wander", journal_key="aiko.associative_wanders"),
        CueSpec("curiosity_gradient", journal_key="aiko.curiosity_gradients"),
        CueSpec("dormant_interest", journal_key="aiko.dormant_interests"),
        CueSpec(
            "knowledge_gap_notice", journal_key="aiko.knowledge_gap_notices",
        ),
    )
}

# ── the cue pool policy registry ──────────────────────────────────────
#
# CueSpec above answers "did this cue have anything to say"; CuePolicy
# answers everything that happens after. How much stock the worker should
# keep, how long a cue stays good, what counts as Aiko having used it, and
# which slab of persona travels with it when it surfaces.
#
# The reason this is one table rather than seven implementations is that
# the equivalent knowledge exists today as hardcoded cooldowns and daily
# caps scattered across seven workers and seven renderers -- two per day
# here, a 3600-second gate there, a 0.50 cosine somewhere else -- and none
# of it can be compared, tuned, or even found. "Surfacing behaves
# differently per cue type" only stops being a slogan once the differences
# are written down next to each other.


# How a cue is judged used.
#
# ``spoken`` -- Aiko saying it is the whole point. She wanted to drift onto
# the subject and she drifted; whether the user takes the bait is a
# separate question and not this cue's business.
#
# ``answered`` -- the cue is a question, so she has only done her part when
# she asks. If she asks about X and the reply is about Y, she still does
# not know, the curiosity is not satisfied, and the cue has to survive.
# That is what the ``awaiting`` state is for.
#
# ``either_party`` -- spent once the subject gets discussed at all, by
# whoever raises it. Curiosity seeds already work this way.
FULFILMENT_SPOKEN = "spoken"
FULFILMENT_ANSWERED = "answered"
FULFILMENT_EITHER = "either_party"

# Whether a cosine hit is trustworthy for this cue, and the axis is not
# the obvious one. ``echo_detector``'s docstring warns that a surfaced item
# was selected for topical similarity, so cosine against the reply mostly
# measures "was on topic". True -- but only for cues whose subject IS the
# live topic. Several of these exist precisely to introduce something the
# conversation is *not* on, and for those a high cosine is the signal we
# want: it means she pivoted.
MATCH_LEXICAL = "lexical"
MATCH_LEXICAL_OR_COSINE = "lexical_or_cosine"

# Which text the subject is matched against.
MATCH_SCOPE_REPLY = "reply"
MATCH_SCOPE_TURN = "turn"

# Which of several waiting cues to offer first. Newest is right for
# almost everything: a cue is built from context, and the freshest
# framing is the one that still fits the conversation.
#
# ``oldest`` exists for the one type where the wait is the point. A
# tease collected an hour after it was banked is a comeback; the same
# line a week later is a callback, which is the whole joke.
PICK_NEWEST = "newest"
PICK_OLDEST = "oldest"


@dataclass(frozen=True, slots=True)
class CuePolicy:
    """Everything type-specific about a pooled cue's life.

    Defaults are the conservative choice at every field: lexical matching
    only, one repeat of each failure mode, and no persona text hoisted.
    A type that wants more says so.
    """

    name: str
    # How many pending cues counts as stocked. The worker reports pressure
    # from the shortfall against this, which is what retires the old daily
    # caps -- inventory is the thing those constants were approximating.
    inventory_target: int = 3
    # How long a cue stays worth saying. A dormant topic keeps for weeks;
    # "did the espresso machine arrive" does not.
    ttl_hours: float = 168.0
    # Times the cue may sit in the prompt without Aiko raising it.
    max_surfacings: int = 2
    # Times she may raise it without getting an answer. Only meaningful
    # for ``answered``.
    max_asks: int = 2
    # How long an unanswered question waits before she may ask again.
    reask_cooldown_hours: float = 24.0
    # How long after any cue of this type reached the prompt before
    # another *new* one may. Zero, the default, means the shelf alone
    # decides -- fine for a type Aiko can reasonably raise several of in
    # an evening.
    #
    # Some types are rare by nature rather than by scarcity, and before
    # the pool their rarity came from the producer: a worker that drafts
    # one cue a fortnight surfaces one a fortnight. Deficit-driven
    # production removes that accident, so a type whose whole character is
    # "this happens twice a month" has to say so here or a full shelf will
    # empty itself over consecutive turns.
    #
    # Gates first claims only. A released row is a cue Aiko was already
    # shown and did not take, and the retry is the point of holding it --
    # cadence governs how often she opens a new one, not how long she
    # takes to finish one.
    surface_cooldown_hours: float = 0.0
    fulfilment: str = FULFILMENT_SPOKEN
    match_mode: str = MATCH_LEXICAL
    match_scope: str = MATCH_SCOPE_REPLY
    # Cosine floor, used only when ``match_mode`` allows it. Provisional
    # everywhere except ``curiosity_seed``, which inherits a value that has
    # been running in production; the rest are recorded as ``used_evidence``
    # on every verdict so the real floors can be read off the distribution
    # later rather than guessed now.
    match_threshold: float = 0.55
    # Shared content words needed for a lexical hit. One, because the
    # subject is matched rather than the whole cue sentence, and a subject
    # term is distinctive by construction -- it survived stopword filtering
    # and topic clustering to become a subject in the first place.
    min_overlap: int = 1
    # Which waiting cue to reach for first. Independent of the
    # already-surfaced-last rule, which always leads: this only orders
    # cues that have had the same number of chances.
    pick_order: str = PICK_NEWEST
    # The persona header whose handling text rides along with this cue
    # instead of sitting in the cache prefix on every turn, and the prompt
    # block whose presence is the trigger for sending it. The block name
    # is stated rather than derived from ``name`` because the two differ
    # (``curiosity_seed`` renders as ``curiosity_seeds_block``), and a
    # silent near-miss here would drop the note without any error.
    handling_section: str = ""
    block: str = ""


CUE_POLICIES: dict[str, CuePolicy] = {
    policy.name: policy
    for policy in (
        # ── on-topic by construction: cosine is confounded, lexical decides
        CuePolicy(
            "knowledge_gap_notice",
            # "I keep circling X and never dug in" -- X is the live topic,
            # so a high cosine against her reply says nothing at all.
            fulfilment=FULFILMENT_ANSWERED,
            match_mode=MATCH_LEXICAL,
            inventory_target=3,
            handling_section="Topics you keep circling but never dug into:",
            block="knowledge_gap_notice_block",
        ),
        CuePolicy(
            "interest_drift",
            # May or may not be current; gets the conservative treatment
            # until the recorded cosines say otherwise.
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL,
            inventory_target=2,
            ttl_hours=168.0,
            handling_section="When your interests shift over time:",
            block="interest_drift_block",
        ),
        # ── off-topic by construction: a high cosine means she pivoted
        CuePolicy(
            "associative_wander",
            # Matched against the *distant* half of the pair, which is by
            # definition not what the conversation is about.
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            inventory_target=3,
            ttl_hours=72.0,
            handling_section="When your mind wanders and connects two things:",
            block="associative_wander_block",
        ),
        CuePolicy(
            "dormant_interest",
            # A topic that has gone quiet -- the opposite of the live one.
            # Reopening it is the point, so silence means it failed.
            fulfilment=FULFILMENT_ANSWERED,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            inventory_target=2,
            ttl_hours=336.0,
            reask_cooldown_hours=72.0,
            handling_section=(
                "When something {user_name} used to love has gone quiet:"
            ),
            block="dormant_interest_block",
        ),
        CuePolicy(
            "curiosity_gradient",
            # The thin edge beside the dense cluster: adjacent to the live
            # topic, deliberately not it.
            fulfilment=FULFILMENT_ANSWERED,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            inventory_target=3,
            handling_section=(
                "When you're curious about the edge of something familiar:"
            ),
            block="curiosity_gradient_block",
        ),
        CuePolicy(
            "self_callback",
            # An aged note of her own, chosen for its age rather than for
            # the live thread -- so unlike the on-topic types above, a
            # high cosine here genuinely means she brought it up.
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            # The excerpt runs to 140 characters, so it is a sentence and
            # not a topic. Same reasoning as ``turning_over``: one shared
            # word between a sentence and a reply is noise.
            min_overlap=3,
            inventory_target=2,
            ttl_hours=336.0,
            # The one type whose whole character is scarcity. Ten days is
            # what ``self_callback_cooldown_days`` defaulted to when the
            # producer held the rarity; moving it here keeps the observed
            # cadence while letting the shelf stay stocked, so a good
            # moment inside the window finds something rather than
            # nothing.
            surface_cooldown_hours=240.0,
            handling_section="Closing the loop on myself (my own continuity):",
            block="self_callback_block",
        ),
        CuePolicy(
            "forward_curiosity",
            # Something in the user's life she has been wondering about,
            # fired on a conversational gap rather than on topic.
            fulfilment=FULFILMENT_ANSWERED,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            inventory_target=2,
            ttl_hours=72.0,
            handling_section="Things I've been wondering about you:",
            block="forward_curiosity_block",
        ),
        CuePolicy(
            "wellbeing_concern",
            # Read off *behaviour* over days -- small-hours activity, "I
            # haven't eaten" said twice in a week -- so the subject has no
            # relationship to what the conversation is currently on and a
            # cosine hit is real signal. It also does the work the lexical
            # path cannot here: the subject is the pattern ("eating") and
            # the check-in is the natural sentence ("have you actually
            # eaten today"), which do not share a token.
            inventory_target=1,
            ttl_hours=72.0,
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            # Rare by nature. Seven days is what the producer-side
            # ``wellbeing_concern_cooldown_days`` defaulted to, and the
            # anti-nag discipline is the whole feature, so it keeps that
            # cadence rather than inheriting the shelf's.
            surface_cooldown_hours=168.0,
            handling_section="When I'm worried about you:",
            block="wellbeing_concern_block",
        ),
        CuePolicy(
            "shared_ritual",
            # The ritual's own label -- "our Friday wind-downs" -- which
            # is a standing pattern rather than the live topic. Two words
            # to match, since a label is a phrase and one shared word is
            # as likely to be the weekday as the ritual.
            inventory_target=1,
            ttl_hours=336.0,
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            min_overlap=2,
            surface_cooldown_hours=72.0,
            handling_section="Our things (shared rituals):",
            block="shared_ritual_block",
        ),
        # ── event-armed: nothing stocks these, the pool is a retry buffer
        #
        # ``inventory_target=0`` is the discriminator, not a tuning
        # choice. No worker drafts these ahead of time: the arming event
        # (a typed gap) carries a duration and nothing else, and *which*
        # reflection or journal beat gets used is picked when the block
        # renders, against the message being answered. A row appears only
        # once one has surfaced, so the shelf is empty by design and the
        # scheduler must never read that as a deficit to fill.
        #
        # Their TTLs are hours rather than days for the same reason a
        # welcome-back is not worth saying on Thursday: the moment they
        # belong to is the return itself.
        CuePolicy(
            "turning_over",
            # A reflection she was mulling. The subject is the reflection
            # text, which is a sentence rather than a topic -- hence the
            # raised overlap, since one shared word between a sentence and
            # a reply means nothing.
            inventory_target=0,
            ttl_hours=12.0,
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            min_overlap=3,
            handling_section="What I've been turning over (between sessions):",
            block="turning_over_block",
        ),
        CuePolicy(
            "sleep_return",
            # The dream, or failing that the spot she dozed off in.
            inventory_target=0,
            ttl_hours=6.0,
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            min_overlap=2,
            handling_section="When I dozed off:",
            block="sleep_return_block",
        ),
        CuePolicy(
            "away_activities",
            # The journal entry's summary clause: "repotted the basil".
            inventory_target=0,
            ttl_hours=12.0,
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            min_overlap=2,
            handling_section="Things I did while you were away:",
            block="away_activities_block",
        ),
        CuePolicy(
            "long_arc_callback",
            # Picked at render like the three above, but by a vector
            # search against the live message rather than off a gap. That
            # is also why it is the one type here that must stay lexical:
            # the candidate was *selected* for cosine similarity to what
            # the user just said, so a high cosine against Aiko's reply
            # measures her staying on topic and would score every turn a
            # hit. Three shared words, because the subject is a quoted
            # snippet rather than a topic.
            inventory_target=0,
            # Long enough to outlast the thread that summoned it, short
            # enough that a callback about hiking is gone by the time the
            # conversation is about anything else. The relevance gate on
            # the retry is the real bound; this is the backstop.
            ttl_hours=12.0,
            max_surfacings=2,
            fulfilment=FULFILMENT_SPOKEN,
            match_mode=MATCH_LEXICAL,
            min_overlap=3,
            # Was ``long_arc_callback_cooldown_hours``, spent at render
            # whether or not she used what she was shown. Here it gates
            # opening a *new* callback only, so an ignored one gets its
            # second chance on the next turn about the same thread
            # instead of costing six hours of silence.
            surface_cooldown_hours=6.0,
            handling_section="When something reaches way back:",
            block="long_arc_callback_block",
        ),
        CuePolicy(
            "self_correction",
            # Also event-armed, but by a post-turn detector rather than a
            # gap, and the content is settled the moment the slip is
            # found -- so unlike the three above this one is queued a turn
            # ahead and claimed normally.
            inventory_target=0,
            # The line opens "a moment ago you said", which survives one
            # turn and is a fiction by the next. Both bounds say the same
            # thing; the surfacing budget is the one that will bite.
            ttl_hours=0.5,
            max_surfacings=2,
            fulfilment=FULFILMENT_SPOKEN,
            # Lexical only, and not for want of calibration: the
            # contradiction is about whatever they are talking about, so
            # her staying on topic would push the cosine up whether or not
            # she ever corrected anything.
            match_mode=MATCH_LEXICAL,
            min_overlap=2,
            handling_section="Catching myself:",
            block="self_correction_block",
        ),
        CuePolicy(
            "tease_ledger",
            # Banked on the turn path like ``self_correction``, and the
            # only entry in the table that wants its cues *stale*. A
            # mock-grudge collected an hour after it was banked is a
            # comeback; the gap between banking and collecting is what
            # makes it a callback, which is the whole joke.
            inventory_target=0,
            pick_order=PICK_OLDEST,
            # Was ``agent.tease_expiry_days``. An old grudge stops being
            # funny, and two weeks is where it stops.
            ttl_hours=336.0,
            # Deliberately looser than the default two. For every other
            # cue an unused surfacing is a small failure; here the note
            # itself says to skip it entirely when no opening shows up,
            # so a miss is the expected outcome and retiring a debt after
            # two of them would throw away good material.
            max_surfacings=3,
            fulfilment=FULFILMENT_SPOKEN,
            # Lexical, and the subject is the user's own quoted words --
            # collecting means echoing them back ("like the time you
            # swore my playlist was objectively chaotic"). Three shared
            # words for the same reason ``long_arc_callback`` uses three:
            # the subject is a phrase, not a topic label.
            match_mode=MATCH_LEXICAL,
            min_overlap=3,
            # Zero, not because there is no cadence but because this
            # one's cannot live here: ``tease_collect_cooldown_hours`` is
            # divided by the J11 affection-style bias, so the interval
            # moves with how well teasing lands for this user. The
            # renderer spends it against ``last_surfaced_at``.
            surface_cooldown_hours=0.0,
            handling_section="Collecting on the ledger:",
            block="tease_ledger_block",
        ),
        # ── whole-turn, unchanged from what already runs
        CuePolicy(
            "curiosity_seed",
            fulfilment=FULFILMENT_EITHER,
            match_mode=MATCH_LEXICAL_OR_COSINE,
            match_scope=MATCH_SCOPE_TURN,
            # The threshold K9 has been resolving seeds at since it
            # shipped. Overridden from
            # ``agent.curiosity_seed_resolve_threshold`` at read time so the
            # existing config key keeps working.
            match_threshold=0.50,
            # Overridden from ``agent.curiosity_seed_max_active``.
            inventory_target=6,
            ttl_hours=336.0,
            handling_section="Quiet curiosity:",
            block="curiosity_seeds_block",
        ),
    )
}

# The cue types that live in ``cue_pool``. Everything else in
# ``CUE_SPECS`` still rides its kv_meta journal, and -- more importantly --
# is still exempt from consumption matching. The old reasoning that "echo
# is meaningless for a cue, because a cue is an instruction" is correct for
# a tone or posture instruction; it is wrong only for a cue that names a
# specific subject, which is exactly the set below.
POOLED_CUES: frozenset[str] = frozenset(CUE_POLICIES)

# Cues whose arming signal is coarse -- a ring with no watermark, so
# "armed" can only mean "the ring is non-empty", which over-counts when
# the provider has already shown that entry's topic. Surfaced in the
# debug view so the ratio is never read as more precise than it is.
# Pooled cues are excluded: their stock is an exact count of unspent
# material, which is what the heuristic was reaching for.
COARSE_ARMING: frozenset[str] = frozenset(
    name for name, spec in CUE_SPECS.items()
    if spec.journal_key and not spec.watermark_key
) - POOLED_CUES


def policy_for(cue_type: str) -> CuePolicy | None:
    """The policy for a pooled cue type, or ``None`` if it is not pooled."""
    return CUE_POLICIES.get(str(cue_type or ""))


def _newest_entry_stamp(chat_db: Any, journal_key: str) -> str | None:
    """The ``at`` of the newest ring entry, or ``None`` when there is none.

    Journals are JSON arrays of dicts appended in time order, so the newest
    entry is the last one. Returns ``None`` on any failure -- an unreadable
    journal must read as "nothing waiting" rather than raise into a turn.
    """
    if chat_db is None or not journal_key:
        return None
    try:
        raw = chat_db.kv_get(journal_key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        ring = json.loads(raw)
    except Exception:
        return None
    if not isinstance(ring, list) or not ring:
        return None
    newest = ring[-1]
    if not isinstance(newest, dict):
        return None
    stamp = str(newest.get("at") or "")
    return stamp or None


def _journal_has_material(chat_db: Any, spec: CueSpec) -> bool:
    """Is there a ring entry the provider has not already surfaced?

    Uses the provider's own watermark so this cannot drift from what the
    provider considers new. Cues without a watermark degrade to "the ring
    is non-empty" -- see :data:`COARSE_ARMING`.
    """
    stamp = _newest_entry_stamp(chat_db, spec.journal_key)
    if stamp is None:
        return False
    if not spec.watermark_key:
        return True
    try:
        watermark = chat_db.kv_get(spec.watermark_key)
    except Exception:
        watermark = None
    return not (watermark and str(watermark) == stamp)


def _pool_stock(session: Any, name: str) -> int | None:
    """Pending cues of this type, or ``None`` when the pool cannot answer.

    ``None`` is not "no stock": it means this cue type does not live in
    the pool, or the session has no store, and the caller should fall
    back to the journal ring.
    """
    if name not in POOLED_CUES:
        return None
    store = getattr(session, "_cue_store", None)
    if store is None:
        return None
    try:
        return int(store.count_pending(name))
    except Exception:
        log.debug("cue pool arming read failed: cue=%s", name, exc_info=True)
        return None


def armed_cues(session: Any) -> set[str]:
    """Which cues had something to say at assembly time.

    Read before the T6 providers run, since several of them *consume* the
    state this inspects -- ``turning_over`` clears its pending slot and the
    journal-backed ones advance their watermark. Reading afterwards would
    report almost nothing as armed, and the ratio would come out looking
    perfect precisely when the machinery was busiest.

    For pooled cues the pool answers instead of the ring, and answers
    better: a pending row is by definition one the provider has not spent,
    which is the exact question the ring-plus-watermark heuristic was
    approximating. ``forward_curiosity`` still needs both halves -- the
    slot arms the opportunity, the pool supplies the content -- which is
    the same conjunction it needed when the content came from a ring.

    The event-armed types invert that. Nothing stocks them, so their
    shelf is empty on the arming turn and a conjunction would report them
    unarmed exactly when they are about to fire. For those the pool is a
    second, independent way to be armed: a row exists only because a
    previous surfacing went unused, and that retry is an opportunity in
    its own right, slot or no slot.
    """
    chat_db = getattr(session, "_chat_db", None)
    out: set[str] = set()
    for name, spec in CUE_SPECS.items():
        stock = _pool_stock(session, name)
        policy = CUE_POLICIES.get(name)
        retry_pool = (
            stock is not None
            and policy is not None
            and policy.inventory_target <= 0
        )
        if retry_pool:
            if not spec.journal_key and not spec.slot_attr:
                # Nothing outside the pool arms this one, so the row is
                # the whole opportunity. Without this branch the two
                # "no signal to check" defaults below would both come out
                # true and the cue would read as armed on every turn.
                if stock > 0:
                    out.add(name)
                continue
            # Content comes from wherever it always did; the pool only
            # adds the released rows on top.
            fresh = not spec.journal_key or _journal_has_material(
                chat_db, spec,
            )
            slot_armed = (
                not spec.slot_attr
                or getattr(session, spec.slot_attr, None) is not None
            )
            if (slot_armed and fresh) or stock > 0:
                out.add(name)
            continue
        has_content = (
            stock > 0
            if stock is not None
            else bool(spec.journal_key)
            and _journal_has_material(chat_db, spec)
        )
        if spec.slot_attr:
            if getattr(session, spec.slot_attr, None) is None:
                continue
            # Slot armed. When the cue also needs content, both have to
            # agree -- which is what its provider requires.
            if (spec.journal_key or stock is not None) and not has_content:
                continue
            out.add(name)
            continue
        if has_content:
            out.add(name)
    return out


@dataclass(slots=True)
class CueTurnDecisions:
    """One turn's worth of cue decisions, before they are persisted.

    Built at assembly time and drained post-turn, mirroring how L37's
    surfaced items are stashed and settled. Kept as a plain record rather
    than written straight through, because the ``assistant_message_id``
    these rows key against does not exist yet when the prompt is built.
    """

    armed: set[str] = field(default_factory=set)
    surfaced: set[str] = field(default_factory=set)
    # cue -> reason, for armed cues that did not surface.
    declined: dict[str, str] = field(default_factory=dict)

    def rows(self) -> list[tuple[str, str, str]]:
        """``(cue, outcome, reason)`` for every armed cue.

        Only armed cues produce rows: "not armed" is the common case and
        carries no information, and recording it would multiply the table
        by the cue count on every turn for nothing.
        """
        out: list[tuple[str, str, str]] = []
        for cue in sorted(self.armed):
            if cue in self.surfaced:
                out.append((cue, OUTCOME_SURFACED, ""))
            else:
                out.append((
                    cue,
                    OUTCOME_DECLINED,
                    self.declined.get(cue, REASON_PROVIDER),
                ))
        return out


def decisions_from_block_chars(
    armed: set[str],
    block_chars: dict[str, int] | None,
    *,
    question_balance_suppressed: bool = False,
) -> CueTurnDecisions:
    """Turn an armed set plus rendered sizes into a decision record.

    ``block_chars`` already records a character count for every registered
    block on every assembly, where ``0`` means the block rendered empty.
    So "was this cue surfaced?" is a question the prompt assembler has been
    answering all along -- it just was not being kept. That is why this
    needs no instrumentation inside the 60-odd T6 providers.

    Decline reasons are attributed structurally, in the order that
    actually decided the outcome:

    - **lost_priority** -- an armed gap cue that did not surface while an
      earlier one in :data:`GAP_CUE_ORDER` did. The reason names the winner,
      turning "the cue vanished" into "it lost to ``turning_over``", which
      is the difference between a shrug and a decision to make.
    - **question_balance** -- the share-first countdown was active, which
      vetoes the question-shaped cues wholesale.
    - **provider** -- everything else, pending the per-provider sweep.
    """
    chars = block_chars or {}

    def _rendered(cue: str) -> bool:
        # Registered under either the bare name or the ``_block`` suffix,
        # matching ``block_char_table``'s own resolution order.
        for key in (cue, f"{cue}_block"):
            if key in chars:
                return int(chars.get(key) or 0) > 0
        return False

    surfaced = {cue for cue in CUE_SPECS if _rendered(cue)}
    gap_winner = next(
        (cue for cue in GAP_CUE_ORDER if cue in surfaced), "",
    )

    declined: dict[str, str] = {}
    for cue in armed - surfaced:
        spec = CUE_SPECS.get(cue)
        if spec is not None and spec.gap_cue and gap_winner and cue != gap_winner:
            declined[cue] = f"{REASON_LOST_PRIORITY}:{gap_winner}"
        elif question_balance_suppressed:
            declined[cue] = REASON_QUESTION_BALANCE
        else:
            declined[cue] = REASON_PROVIDER

    return CueTurnDecisions(
        # A cue that surfaced without being detected as armed is a gap in
        # the arming model -- and it definitionally *had* material, or it
        # could not have rendered. Folding it in keeps the ratio at or
        # below 100% and keeps the row: ``rows()`` only walks ``armed``, so
        # leaving it out would silently drop a real surfacing.
        armed=set(armed) | surfaced,
        surfaced=surfaced,
        declined=declined,
    )


__all__ = [
    "COARSE_ARMING",
    "CUE_POLICIES",
    "CUE_SPECS",
    "FULFILMENT_ANSWERED",
    "FULFILMENT_EITHER",
    "FULFILMENT_SPOKEN",
    "GAP_CUE_ORDER",
    "MATCH_LEXICAL",
    "MATCH_LEXICAL_OR_COSINE",
    "MATCH_SCOPE_REPLY",
    "MATCH_SCOPE_TURN",
    "OUTCOME_DECLINED",
    "OUTCOME_SURFACED",
    "POOLED_CUES",
    "REASON_LOST_PRIORITY",
    "REASON_PROVIDER",
    "REASON_QUESTION_BALANCE",
    "CuePolicy",
    "CueSpec",
    "CueTurnDecisions",
    "armed_cues",
    "decisions_from_block_chars",
    "policy_for",
]
