"""Per-consumer concept diets -- which concepts a worker gets to think with.

Background workers used to read concepts in one of three ways: a single
hardcoded ``kind=`` (``TensionCueWorker`` asks only for tensions), every
kind at once (``for_cluster`` returns whatever is edged to a cluster), or
not at all. None of the three is a decision about what that worker needs
to *understand*, and none of them is measured -- a worker prompt has no
input token accounting, so "read the concept layer" quietly means "read
all of it" as the store grows.

A diet is that decision, written down once per consumer:

    register_diet(ConceptDiet(
        consumer="belief_inference",
        kinds=("identity", "value", "affective", "taste", "aspiration"),
        subject="user",
        rationale="...",
    ))

Three things make a diet more than a filter tuple.

**It is budgeted.** ``weight`` scales a global token allowance derived
from the *worker* context window (see :func:`resolve_budget`), so the
concept section of a worker prompt has a known size and a reflection
worker can be given more room than a one-line cue worker without either
of them being able to grow without bound.

**It is balanced.** :meth:`ConceptView.for_consumer` draws round-robin
across the declared kinds rather than taking a global ranked prefix. A
ranked prefix would be sorted by strength, and because ``importance``
is a per-kind prior, the strongest concepts are almost always
``boundary`` (0.9) and ``value`` (0.85). A worker fed that list can
restate what Aiko already holds and never reach past it.

**It cannot be all rails.** A diet naming any ``guide``-role kind must
also name at least one ``generative`` kind -- see
:func:`diet_problems`. The invariant exists because the failure it
prevents is silent: a cue worker given boundaries and values produces
cues that keep her exactly as she is, and nothing about that looks like
a bug from the outside.

**The exclusion principle.** Producers of concepts do not get diets.
Feeding ``ConceptSynthesisWorker`` or ``HypothesisProposerWorker`` the
existing concept set would let the layer confirm itself -- new concepts
proposed in the shape of the old ones. Their direct reads for novelty
and dedupe are a different question from "what should I think with" and
stay as they are. Workers that produce something *else* (cues, goals,
beliefs, wants) are ordinary consumers and do get diets.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.concepts.concept_kinds import (
    CONCEPT_KINDS,
    ROLE_GENERATIVE,
    ROLE_GUIDE,
    get_kind,
)

if TYPE_CHECKING:
    from collections.abc import Callable


log = logging.getLogger("app.concept_diets")


@dataclass(frozen=True, slots=True)
class ConceptDiet:
    """What one consumer is allowed to think with."""

    #: Stable key the consumer passes to ``ConceptView.for_consumer``.
    consumer: str
    #: Kind names to draw from, round-robin. Order is not significance --
    #: the draw balances across kinds regardless.
    kinds: tuple[str, ...]
    #: ``None`` means any subject. Set it when a consumer is only about
    #: one side (belief inference models *him*; the wants ledger offers
    #: things of *hers*).
    subject: str | None = None
    min_confidence: float = 0.0
    #: Appetite relative to the global budget. A reflection pass that
    #: reasons over the whole picture earns more than a worker that
    #: renders one line.
    weight: float = 1.0
    #: Off by default: the token budget is the limit that normally binds.
    #: Set it only where a consumer genuinely wants a short list for
    #: reasons of its own (a cue worker that picks exactly one).
    max_concepts: int | None = None
    #: How deep any single kind may be drawn before the round-robin.
    per_kind_cap: int | None = None
    #: Why this consumer needs these kinds. Read by humans, and by the
    #: docs that have to explain the choice later.
    rationale: str = ""


CONCEPT_DIETS: dict[str, ConceptDiet] = {}


def register_diet(diet: ConceptDiet) -> ConceptDiet:
    """Register (or replace) a diet by consumer name."""
    CONCEPT_DIETS[diet.consumer] = diet
    return diet


def diet_for(consumer: str) -> ConceptDiet | None:
    """The diet for ``consumer``, or ``None`` if it has none.

    ``None`` is the ordinary answer for most of the codebase and means
    "this consumer does not read concepts through a diet" -- either it
    does not read them at all, or it is a producer covered by the
    exclusion principle. Callers treat it as "no concepts", never as an
    error.
    """
    return CONCEPT_DIETS.get(str(consumer or ""))


# ── budget ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DietTuning:
    """Everything :meth:`ConceptView.for_consumer` needs from settings.

    Bundled into one argument so the facade's constructor does not grow a
    parameter per knob, and so a caller that has no settings to hand (a
    test, a cold install) gets coherent defaults by omitting it.

    ``importance_strength`` defaults to ``0.0``, which switches the L32
    axis off and leaves within-kind ordering on banded confidence -- the
    behaviour every existing direct construction of a
    :class:`ConceptView` already has. Wiring it up is
    :func:`concept_view_from`'s job.
    """

    #: The *worker* route's window, not the chat route's.
    context_window: int = 0
    token_fraction: float = 0.06
    max_tokens: int = 600
    min_tokens: int = 150
    importance_strength: float = 0.0
    importance_lift: float = 0.5
    importance_min_samples: int = 3
    affect_max_age_days: float = 120.0


DEFAULT_DIET_TUNING = DietTuning()


def tuning_from_host(host: object) -> DietTuning:
    """Resolve a :class:`DietTuning` from a ``SessionController``-like host.

    Sized against the **worker** route's context window, not the chat
    route's. Diets feed background workers, and the common deployment
    points chat at a large remote model while the workers stay on a
    smaller local one -- reading the chat window here would hand a 64k
    local worker a budget computed from a 200k cloud context.

    Every read is defensive: this also runs against test doubles and
    half-built hosts, and anything missing falls back to the dataclass
    default, which is a working configuration rather than a zero.
    """
    ms = getattr(host, "_memory_settings", None)
    window = 0
    resolve = getattr(host, "_worker_route_model_ctx", None)
    if callable(resolve):
        try:
            _model, window = resolve()
        except Exception:
            log.debug("worker route context window read failed", exc_info=True)
            window = 0
    strength = (
        float(getattr(ms, "concept_importance_strength", 0.0) or 0.0)
        if bool(getattr(ms, "concept_importance_enabled", True))
        else 0.0
    )
    return DietTuning(
        context_window=int(window or 0),
        token_fraction=float(
            getattr(ms, "concept_diet_token_fraction", 0.06) or 0.0
        ),
        max_tokens=int(getattr(ms, "concept_diet_max_tokens", 600) or 0),
        min_tokens=int(getattr(ms, "concept_diet_min_tokens", 150) or 0),
        importance_strength=strength,
        importance_lift=float(
            getattr(ms, "concept_importance_affect_lift", 0.5) or 0.0
        ),
        importance_min_samples=int(
            getattr(ms, "concept_importance_affect_min_samples", 3) or 0
        ),
        affect_max_age_days=float(
            getattr(ms, "cluster_affect_max_age_days", 120.0) or 0.0
        ),
    )


def resolve_budget(diet: ConceptDiet, tuning: DietTuning) -> int:
    """Token allowance for one diet: ``min(fraction * ctx, max_tokens)``
    scaled by ``weight``, floored at ``min_tokens``.

    The same shape as the main prompt's surfacing budget
    (``_size_context_budget``), and for the same reason: a budget stated
    as a share of the window auto-scales when the route changes instead
    of silently becoming a bigger or smaller slice of the prompt.

    The absolute cap is not redundant with the fraction -- it is what
    makes the fraction meaningful. A concept renders at roughly 15-20
    tokens, so on a 64k worker route even a few percent is hundreds of
    concepts, likely more than the store holds; the fraction alone would
    never bind and every diet would quietly mean "all of them". So the
    fraction is the guard that protects a *small* window and the cap is
    what actually sizes the section on a large one.

    An unknown window (``0``) falls back to the cap rather than to zero:
    a view built without settings should still hand a worker a sane
    handful of concepts instead of silently starving it.
    """
    ctx = max(0, int(tuning.context_window))
    cap = max(0, int(tuning.max_tokens))
    share = int(max(0.0, float(tuning.token_fraction)) * ctx)
    base = min(share, cap) if ctx > 0 else cap
    scaled = int(base * max(0.0, float(diet.weight)))
    return max(max(0, int(tuning.min_tokens)), scaled)


# ── invariants ────────────────────────────────────────────────────────


def diet_problems(diet: ConceptDiet) -> list[str]:
    """Everything wrong with ``diet``, as human-readable strings.

    Returns ``[]`` for a healthy diet. Checked by a test over the whole
    registry rather than at import time: a malformed diet should fail the
    build, not take the app down on a cold start where the concept layer
    might not even be wired.
    """
    problems: list[str] = []
    if not diet.consumer:
        problems.append("diet has no consumer name")
    if not diet.kinds:
        problems.append(f"{diet.consumer}: declares no kinds")
    seen: set[str] = set()
    for name in diet.kinds:
        if name in seen:
            problems.append(f"{diet.consumer}: duplicate kind {name!r}")
        seen.add(name)
        if get_kind(name) is None:
            problems.append(f"{diet.consumer}: unknown kind {name!r}")
    roles = {
        CONCEPT_KINDS[n].role for n in diet.kinds if n in CONCEPT_KINDS
    }
    if ROLE_GUIDE in roles and ROLE_GENERATIVE not in roles:
        problems.append(
            f"{diet.consumer}: names a guide kind but nothing generative -- "
            "a consumer that only sees what constrains her will only ever "
            "produce what keeps her the same"
        )
    if diet.weight <= 0.0:
        problems.append(f"{diet.consumer}: weight must be positive")
    if diet.max_concepts is not None and diet.max_concepts <= 0:
        problems.append(f"{diet.consumer}: max_concepts must be positive or None")
    return problems


def registry_problems() -> list[str]:
    """:func:`diet_problems` across every registered diet."""
    out: list[str] = []
    for diet in CONCEPT_DIETS.values():
        out.extend(diet_problems(diet))
    return out


# ── ranking ───────────────────────────────────────────────────────────
# The L32 stakes axis lives here rather than in ``concept_view`` on
# purpose, and a test enforces it: that module must not so much as name
# ``concept_importance``. Its other lanes (``core``, ``for_target``,
# ``core_lane``) feed T0/T1 prompt blocks in the cache prefix, and
# importance is a live, affect-sensitive number -- a topic's emotional
# weather shifting mid-session would resequence the profile block and
# invalidate the cache behind it. Diets have no such constraint: they
# feed off-turn worker prompts that are rebuilt every run anyway. Keeping
# the two apart at module level is what lets the guard stay a blanket
# check instead of an ordering assertion someone has to remember.


def importance_lookup(
    concept_ids: "set[int]",
    *,
    store: object,
    topic_graph: object,
    kv_get: "Callable[[str], str | None] | None",
    tuning: DietTuning,
) -> "Callable[[object], float]":
    """An importance-per-concept lookup for one diet's candidate pool.

    Degrades in two steps rather than failing. With ``importance_strength``
    at zero the axis is off and everything scores neutral, so ranking
    falls back to confidence. Without a usable affect context (no
    ``kv_get``, a cold topic graph, a failed read) it falls back to the
    bare kind prior -- which is *constant within a kind*, and since diets
    rank inside kind buckets that also collapses to confidence ordering.
    Neither is a regression; they are kept distinct because the first is a
    deliberate switch and the second is a missing dependency.
    """
    from app.core.concepts.concept_importance import (
        IMPORTANCE_NEUTRAL,
        kind_importance,
    )

    if float(tuning.importance_strength) <= 0.0:
        return lambda _c: IMPORTANCE_NEUTRAL
    ctx = _affect_context(
        concept_ids, store=store, topic_graph=topic_graph,
        kv_get=kv_get, tuning=tuning,
    )
    if ctx is None:
        return lambda c: kind_importance(getattr(c, "kind", ""))
    return ctx.for_concept


def _affect_context(
    concept_ids: "set[int]",
    *,
    store: object,
    topic_graph: object,
    kv_get: "Callable[[str], str | None] | None",
    tuning: DietTuning,
):
    """One L32 ``ImportanceContext`` over a candidate set, or ``None``.

    Three bounded reads and no per-concept query: the two cluster affect
    maps, the memory -> cluster bridge off the live topic graph, and a
    single bulk join for the candidates' cluster evidence edges.
    Best-effort by design -- importance is a lens on the ordering, never
    a reason for a worker to lose its concepts.
    """
    if store is None or not concept_ids or kv_get is None:
        return None
    try:
        from app.core.concepts.cluster_affect import (
            KV_CLUSTER_AFFECT_AIKO,
            KV_CLUSTER_AFFECT_USER,
            load_map,
        )
        from app.core.concepts.concept_importance import (
            ImportanceContext,
            cluster_membership,
        )

        return ImportanceContext(
            affect_user=load_map(kv_get, KV_CLUSTER_AFFECT_USER),
            affect_aiko=load_map(kv_get, KV_CLUSTER_AFFECT_AIKO),
            cluster_by_memory=cluster_membership(topic_graph),
            memory_ids_by_concept=store.cluster_evidence_for(concept_ids),
            lift=float(tuning.importance_lift),
            min_samples=int(tuning.importance_min_samples),
            max_age_days=float(tuning.affect_max_age_days),
        )
    except Exception:
        log.debug("diet importance context build failed", exc_info=True)
        return None


# ── the registry ──────────────────────────────────────────────────────
# Consumers only. See the exclusion principle in the module docstring for
# why the concept *producers* are absent.


register_diet(
    ConceptDiet(
        consumer="interest_drift",
        kinds=("identity", "affective", "value", "taste", "pursuit"),
        weight=0.3,
        rationale=(
            "K64b names why a topic is pulling on her. The mass series "
            "only knows a cluster grew, so the diet supplies the why: what "
            "he is like about it (identity), how it lands (affective), the "
            "principle under it (value), and -- so the answer can be plain "
            "enthusiasm rather than always a conviction -- what she enjoys "
            "in it (taste, pursuit)."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="knowledge_map_reflection",
        kinds=(
            "identity",
            "value",
            "affective",
            "generalization",
            "aspiration",
            "taste",
            "pursuit",
        ),
        weight=2.0,
        rationale=(
            "K64d reflects on the shape of the whole map, which is the "
            "broadest question any worker asks, so it gets the broadest "
            "diet and double the ordinary budget. Generalizations matter "
            "here specifically: a reflection on territories is exactly "
            "where an abstraction over several of them should speak."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="tension_cue",
        kinds=("tension",),
        weight=0.4,
        rationale=(
            "L12's cue producer is about tensions and nothing else. Kept "
            "as a declared diet rather than an inline kind= so the "
            "registry is the full picture of who reads what."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="aspiration_momentum",
        kinds=("aspiration",),
        weight=0.4,
        rationale=(
            "L14's momentum worker asks whether a trajectory has gone "
            "quiet, which is a question only an aspiration can answer."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="wants_ledger",
        kinds=("pursuit",),
        subject="aiko",
        weight=0.4,
        rationale=(
            "K85e turns an interest of her own into a standing offer to "
            "share. Held to pursuits for now: a want carries a "
            "``pursuit:{id}`` source_ref that the ledger's self-heal "
            "pruner matches on, so widening the diet means generalising "
            "that key first."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="forward_curiosity",
        kinds=("aspiration", "affective", "taste", "pursuit"),
        min_confidence=0.6,
        weight=0.5,
        rationale=(
            "K34 drafts what she has been wondering about. Its three "
            "memory-row pools mean a plan, a callback or a note is the "
            "only thing she can be curious about; these kinds let her "
            "wonder about a direction he is on, a topic that moves him, "
            "or something of her own. Tension is deliberately absent -- "
            "it has its own strictly-cooldowned cue and is delivered with "
            "more care than a casual gap-return question. The confidence "
            "floor keeps her from asking about half-formed readings."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="belief_inference",
        kinds=("identity", "value", "affective", "taste", "aspiration"),
        subject="user",
        weight=1.0,
        rationale=(
            "K2 infers what he is thinking *now*; the durable layer is a "
            "prior on what to look for. Subject-scoped to the user "
            "because a belief is a prediction about him. Taste and "
            "aspiration are in the diet so the extractor looks for what "
            "he is enjoying and heading toward, not only what he holds."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="goals_block",
        kinds=("aspiration",),
        subject="aiko",
        weight=0.4,
        rationale=(
            "L28's last gated consumer. An aspiration is who she is "
            "becoming and a K1 goal is an actionable to-do; the block "
            "leads with the former and floors on the latter."
        ),
    )
)

register_diet(
    ConceptDiet(
        consumer="stance",
        kinds=("value", "taste", "pursuit"),
        subject="aiko",
        weight=0.4,
        rationale=(
            "K29 gives her a stance of her own. Values alone would make "
            "every opinion a principle; taste and pursuit are what let "
            "one be a preference she simply has. The clearest case of the "
            "guide-implies-generative invariant doing real work."
        ),
    )
)


__all__ = [
    "CONCEPT_DIETS",
    "DEFAULT_DIET_TUNING",
    "ConceptDiet",
    "DietTuning",
    "diet_for",
    "diet_problems",
    "importance_lookup",
    "register_diet",
    "registry_problems",
    "resolve_budget",
    "tuning_from_host",
]
