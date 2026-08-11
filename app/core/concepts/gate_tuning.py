"""L45 gate tuning -- the pure solver behind the self-calibrating thresholds.

Every concept threshold we retuned by hand followed the same shape: look at
the live distribution, notice the bar sits in the wrong place *relative to
that distribution*, move it. ``taste_min_affinity = 0.5`` was not wrong in
the abstract -- it was wrong because no topic cluster on this ledger exceeds
0.32, so the taste pass could never mint anything. On a chattier relationship
the same constant might have been fine.

So the fix is not to tune numbers automatically, it is to **stop storing
numbers and start storing intent**. A :class:`GateSpec` declares what
population its threshold is supposed to admit; :func:`solve` measures and
solves for the value that hits it. The constant in
:class:`~app.core.infra.memory_settings.MemorySettings` becomes the fallback
for a graph too young to have a distribution yet.

Three objectives cover every gate worth calibrating:

- :data:`OBJ_SHARE_ABOVE` -- put the bar where ``target`` of the population
  sits above it. For promotion and retirement, where the question is "what
  fraction should pass".
- :data:`OBJ_POOL_MULTIPLE` -- leave an eligible pool at least ``target``
  times the lane's cap. This is the L23 / L40 reasoning made explicit: a lane
  whose eligible pool equals its cap has nothing to rotate through, so
  habituation is inert and the same rows pin every turn.
- :data:`OBJ_UNDER_REACH` -- keep the bar below what the population can
  actually reach. The taste failure, generalised: a floor above the observed
  maximum is a gate that can never open.

**Read gates apply, write gates observe.** The dividing line is whether a
gate writes to the store or only reads from it, which matters more than which
gates we happened to measure first. A read gate decides what enters one
prompt: a bad value costs one turn, self-corrects on the next run, and leaves
no trace. A write gate mutates persistent state *and* shifts the very
distribution the tuner measures next time -- lower the promote bar, promote
more, the active-confidence distribution moves, the next solve moves again.
That feedback loop earns a period of being watched, so every write gate ships
:data:`MODE_OBSERVE`: measured, solved, recorded, never applied. Promoting one
to :data:`MODE_APPLY` is a one-word change here once its history looks sane.

This module is pure -- no clock, no database, no settings object, no file
I/O. The store layer (:mod:`app.core.infra.gate_tuning_store`) owns
persistence and the apply decision; the worker
(:mod:`app.core.concepts.gate_tuner_worker`) owns measurement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.core.concepts.concept_kinds import (
    CONCEPT_KINDS,
    ROLE_GENERATIVE,
)
from app.core.concepts.concept_lifecycle import KIND_PROMOTION_FLOORS

# ── modes ─────────────────────────────────────────────────────────────

#: The gate's solved value is written to ``MemorySettings``.
MODE_APPLY = "apply"
#: The gate is measured and recorded but never applied. Every write gate
#: starts here; see the module docstring for why.
MODE_OBSERVE = "observe"

# ── objectives ────────────────────────────────────────────────────────

OBJ_SHARE_ABOVE = "share_above"
OBJ_POOL_MULTIPLE = "pool_multiple"
OBJ_UNDER_REACH = "under_reach"

# ── population keys ───────────────────────────────────────────────────
#
# The worker supplies these; the specs name them. Kept as plain strings so
# a spec can be read without importing the worker.

POP_CANDIDATE_CONFIDENCE = "candidate_confidence"
POP_ACTIVE_CONFIDENCE = "active_confidence"
POP_FADED_CONFIDENCE = "faded_confidence"
POP_CORE_POOL = "core_pool_confidence"
POP_OPENNESS_POOL = "openness_pool_confidence"
POP_PROFILE_POOL = "profile_pool_confidence"
POP_CLUSTER_ENGAGED_RATE = "cluster_engaged_rate"
POP_PAIR_COSINE = "pair_cosine"
#: Wall-clock days since each dormant concept was last reinforced. The one
#: population here measured in days rather than in a score, because the gate
#: it informes (``concept_dormant_ttl_days``) is a duration.
POP_DORMANT_QUIET_DAYS = "dormant_quiet_days"
#: L31: how well each arriving piece of evidence matched the concept it was
#: cited for. Unlike every other population here this one is *inflow* rather
#: than stock -- the synthesis worker records each cosine as it judges it and
#: rolls the sample through ``kv_meta``, because the gate acts on evidence
#: arriving, not on evidence already stored.
POP_EVIDENCE_FIT = "evidence_fit"

#: Per-kind confidence populations are keyed ``kind_confidence:<kind>``.
POP_KIND_CONFIDENCE_PREFIX = "kind_confidence:"


def kind_population(kind: str) -> str:
    return POP_KIND_CONFIDENCE_PREFIX + str(kind)


def kind_floor_key(kind: str) -> str:
    """The synthetic gate name for a per-kind promotion floor.

    The floors live as module constants in
    :mod:`app.core.concepts.concept_lifecycle`, not as settings fields, so
    they have nowhere to be applied *to*. They are observed anyway: by the
    time phase 4 promotes them into ``MemorySettings`` we want months of
    history rather than another measurement expedition.
    """
    return f"kind_floor.{kind}.min_confidence"


@dataclass(frozen=True, slots=True)
class GateSpec:
    """What one threshold is *for*, in place of what it happens to be.

    ``setting`` doubles as the gate's identity in the tuning file and, when
    ``is_setting_field``, the ``MemorySettings`` attribute to write.

    ``floor`` / ``ceiling`` are hard rails the solver never crosses no matter
    what the data says -- they encode "a value outside this range means the
    measurement is wrong, not that the threshold should move there".
    ``max_step`` caps movement per run, so a gate walks rather than jumps and
    an anomalous day cannot swing behaviour.
    """

    setting: str
    population: str
    objective: str
    target: float
    why: str
    mode: str = MODE_OBSERVE
    floor: float = 0.0
    ceiling: float = 1.0
    max_step: float = 0.05
    min_samples: int = 40
    #: For ``pool_multiple``: the settings field holding the lane's cap.
    pool_cap_setting: str = ""
    #: False for the per-kind floors, which are module constants. Recorded
    #: and reported, never written -- a second lock beyond ``mode``.
    is_setting_field: bool = True
    #: Grouping label for reports (the kind a per-kind floor belongs to).
    kind: str = ""

    @property
    def writable(self) -> bool:
        return self.mode == MODE_APPLY and self.is_setting_field


@dataclass(frozen=True, slots=True)
class GateSolution:
    """One gate's answer for one run.

    ``proposed`` is what the data asks for after every rail; ``clamped_by``
    names the rail that had the last word, which is usually the interesting
    part -- a gate permanently pinned by ``floor`` is a gate whose spec is
    wrong, and a gate reporting ``max_step`` every run is still walking.
    """

    setting: str
    mode: str
    current: float
    proposed: float
    raw: float | None
    clamped_by: str | None
    reason: str
    stats: dict[str, Any]

    @property
    def moved(self) -> bool:
        return abs(self.proposed - self.current) > 1e-9


# ── distribution helpers ──────────────────────────────────────────────


def _clean(samples: Sequence[float]) -> list[float]:
    out: list[float] = []
    for raw in samples or ():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        out.append(value)
    out.sort()
    return out


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile over an already-sorted run.

    Hand-rolled rather than ``numpy.quantile`` to keep this module free of
    the import: it is the one piece of arithmetic every spec depends on, and
    a pure implementation keeps the unit tests trivial.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = max(0.0, min(1.0, float(q))) * (len(sorted_values) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return float(sorted_values[low])
    lower = float(sorted_values[low]) * (high - pos)
    upper = float(sorted_values[high]) * (pos - low)
    return lower + upper


def describe(samples: Sequence[float]) -> dict[str, Any]:
    """The distribution summary carried in the tuning file and the snapshot."""
    values = _clean(samples)
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(values[0], 4),
        "p10": round(quantile(values, 0.10), 4),
        "median": round(quantile(values, 0.50), 4),
        "p90": round(quantile(values, 0.90), 4),
        "max": round(values[-1], 4),
        "mean": round(sum(values) / len(values), 4),
    }


# ── the objectives ────────────────────────────────────────────────────


def _share_above(values: Sequence[float], target: float) -> float:
    """Bar with ``target`` of the population above it."""
    share = max(0.0, min(1.0, float(target)))
    return quantile(values, 1.0 - share)


def _pool_multiple(
    values: Sequence[float], target: float, pool_cap: int | None,
) -> float | None:
    """Bar leaving at least ``target x pool_cap`` rows above it.

    Returns ``None`` when the cap is unknown -- a pool objective without a
    cap has nothing to be a multiple *of*, and guessing would silently
    calibrate against the wrong lane.
    """
    if pool_cap is None or int(pool_cap) <= 0:
        return None
    wanted = max(1, int(math.ceil(float(target) * int(pool_cap))))
    if wanted >= len(values):
        # The pool cannot be made big enough by lowering the bar, so ask for
        # the least selective value the data offers and let the floor rail
        # decide how far down that is allowed to go.
        return float(values[0])
    # ``values`` ascends; the k-th largest sits this far from the end. The
    # lane compares with >=, so exactly ``wanted`` rows clear it.
    return float(values[len(values) - wanted])


def _under_reach(values: Sequence[float], target: float) -> float:
    """Bar at ``target`` of the population's reach.

    ``target`` below 1.0 leaves headroom, which is the whole point: a floor
    at or above the observed maximum is a gate that can never open, which is
    exactly how the taste pass spent five weeks minting nothing.
    """
    return float(values[-1]) * max(0.0, float(target))


def solve(
    spec: GateSpec,
    samples: Sequence[float],
    *,
    current: float,
    pool_cap: int | None = None,
) -> GateSolution:
    """Solve one gate against one population.

    Rails apply in a fixed order, and whichever one had the last word is
    reported in ``clamped_by``: warmup (not enough samples yet), then the
    objective, then ``max_step`` from ``current``, then ``floor`` /
    ``ceiling``. A gate that cannot be solved keeps ``current`` -- never a
    code default, never zero -- so a failed measurement is inert rather than
    destructive.
    """
    values = _clean(samples)
    stats = describe(values)
    now = float(current)

    if len(values) < max(1, int(spec.min_samples)):
        return GateSolution(
            setting=spec.setting,
            mode=spec.mode,
            current=now,
            proposed=now,
            raw=None,
            clamped_by="warmup",
            reason=(
                f"{len(values)} samples, needs {spec.min_samples}"
            ),
            stats=stats,
        )

    if spec.objective == OBJ_SHARE_ABOVE:
        raw = _share_above(values, spec.target)
    elif spec.objective == OBJ_POOL_MULTIPLE:
        raw = _pool_multiple(values, spec.target, pool_cap)
    elif spec.objective == OBJ_UNDER_REACH:
        raw = _under_reach(values, spec.target)
    else:
        raw = None

    if raw is None:
        return GateSolution(
            setting=spec.setting,
            mode=spec.mode,
            current=now,
            proposed=now,
            raw=None,
            clamped_by="no_signal",
            reason=f"objective {spec.objective} could not be evaluated",
            stats=stats,
        )

    proposed = float(raw)
    clamped_by: str | None = None

    step = max(0.0, float(spec.max_step))
    if step > 0.0 and abs(proposed - now) > step:
        proposed = now + step if proposed > now else now - step
        clamped_by = "max_step"

    if proposed < float(spec.floor):
        proposed = float(spec.floor)
        clamped_by = "floor"
    elif proposed > float(spec.ceiling):
        proposed = float(spec.ceiling)
        clamped_by = "ceiling"

    return GateSolution(
        setting=spec.setting,
        mode=spec.mode,
        current=now,
        proposed=round(proposed, 4),
        raw=round(float(raw), 4),
        clamped_by=clamped_by,
        reason=spec.why,
        stats=stats,
    )


def solve_all(
    specs: Sequence[GateSpec],
    populations: Mapping[str, Sequence[float]],
    *,
    current: Mapping[str, float],
    caps: Mapping[str, int] | None = None,
) -> dict[str, GateSolution]:
    """Solve every spec, skipping any whose population was not supplied."""
    caps = caps or {}
    out: dict[str, GateSolution] = {}
    for spec in specs:
        samples = populations.get(spec.population)
        if samples is None:
            continue
        out[spec.setting] = solve(
            spec,
            samples,
            current=float(current.get(spec.setting, 0.0)),
            pool_cap=caps.get(spec.pool_cap_setting),
        )
    return out


# ── the v1 registry ───────────────────────────────────────────────────
#
# Read gates apply; write gates observe. Deliberately excluded, so the
# omissions are not mistaken for oversights:
#
# * Structural caps -- token budgets, batch sizes, ``max_*_per_run``,
#   ``profile_concept_max_lines`` -- are cost knobs. No distribution tells
#   you the right value; they encode what the user is willing to spend.
# * ``context_budget_*_min_relevance`` compares against per-turn relevance
#   *scores*, which no store snapshot contains. It needs telemetry from the
#   selection path rather than a solver, so it waits for its own phase.


_APPLIED_GATES: tuple[GateSpec, ...] = (
    GateSpec(
        setting="context_budget_core_min_confidence",
        population=POP_CORE_POOL,
        objective=OBJ_POOL_MULTIPLE,
        target=3.0,
        pool_cap_setting="context_budget_core_cap",
        mode=MODE_APPLY,
        floor=0.5,
        ceiling=0.95,
        max_step=0.03,
        min_samples=60,
        why=(
            "the core lane needs about three times its cap eligible or "
            "habituation has nothing to rotate through"
        ),
    ),
    GateSpec(
        setting="concept_core_openness_min_confidence",
        population=POP_OPENNESS_POOL,
        objective=OBJ_POOL_MULTIPLE,
        target=6.0,
        pool_cap_setting="concept_core_openness_slots",
        mode=MODE_APPLY,
        floor=0.3,
        ceiling=0.8,
        max_step=0.03,
        min_samples=20,
        why=(
            "the openness reserve draws from a thin pool; it needs several "
            "candidates per slot to spread across kinds and rest between turns"
        ),
    ),
    GateSpec(
        setting="profile_concept_min_confidence",
        population=POP_PROFILE_POOL,
        objective=OBJ_POOL_MULTIPLE,
        target=5.0,
        pool_cap_setting="profile_concept_max_lines",
        mode=MODE_APPLY,
        floor=0.4,
        ceiling=0.9,
        max_step=0.03,
        min_samples=40,
        why=(
            "the T0 block has no rotation, so its bar should admit only the "
            "most settled traits rather than everything it could fit"
        ),
    ),
)


_LIFECYCLE_GATES: tuple[GateSpec, ...] = (
    GateSpec(
        setting="concept_promote_min_confidence",
        population=POP_CANDIDATE_CONFIDENCE,
        objective=OBJ_SHARE_ABOVE,
        target=0.35,
        floor=0.5,
        ceiling=0.85,
        max_step=0.02,
        min_samples=40,
        why=(
            "promotion should be a real filter on the candidate pool rather "
            "than a rubber stamp or a wall"
        ),
    ),
    GateSpec(
        setting="concept_dormant_confidence_floor",
        population=POP_ACTIVE_CONFIDENCE,
        objective=OBJ_SHARE_ABOVE,
        target=0.92,
        floor=0.2,
        ceiling=0.5,
        max_step=0.02,
        min_samples=60,
        why=(
            "the faded tail of the active pool should keep going quiet at a "
            "steady rate, not all at once and not never"
        ),
    ),
    GateSpec(
        setting="concept_retire_confidence_floor",
        population=POP_FADED_CONFIDENCE,
        objective=OBJ_SHARE_ABOVE,
        target=0.85,
        floor=0.05,
        ceiling=0.3,
        max_step=0.02,
        min_samples=30,
        why=(
            "retirement should reach the bottom of the dormant pool without "
            "swallowing beliefs that are merely resting"
        ),
    ),
    GateSpec(
        setting="concept_dormant_ttl_days",
        population=POP_DORMANT_QUIET_DAYS,
        objective=OBJ_SHARE_ABOVE,
        # Keep roughly the quietest fifth of the dormant pool inside the
        # retiring end. Deliberately not aggressive: dormancy is where a
        # belief rests, and the point of the L46 duration route is to stop
        # the pool growing without bound, not to empty it.
        target=0.2,
        floor=14.0,
        ceiling=180.0,
        max_step=5.0,
        min_samples=40,
        why=(
            "a dormant pool nothing has re-observed should keep draining at "
            "a steady rate rather than accumulating forever"
        ),
    ),
    GateSpec(
        setting="taste_min_affinity",
        population=POP_CLUSTER_ENGAGED_RATE,
        objective=OBJ_UNDER_REACH,
        target=0.6,
        floor=0.05,
        ceiling=0.5,
        max_step=0.05,
        min_samples=15,
        why=(
            "the absolute floor must stay under what her best cluster can "
            "actually reach, or the taste pass can never mint anything"
        ),
    ),
)


_COSINE_GATES: tuple[GateSpec, ...] = (
    GateSpec(
        setting="concept_dedupe_cosine",
        population=POP_PAIR_COSINE,
        objective=OBJ_SHARE_ABOVE,
        target=0.004,
        floor=0.75,
        ceiling=0.95,
        max_step=0.01,
        min_samples=500,
        is_setting_field=False,
        why=(
            "creation-time dedupe should catch the genuine paraphrase twins "
            "at the very top of the similarity distribution"
        ),
    ),
    GateSpec(
        setting="concept_consolidation_merge_cosine",
        population=POP_PAIR_COSINE,
        objective=OBJ_SHARE_ABOVE,
        target=0.01,
        floor=0.7,
        ceiling=0.95,
        max_step=0.01,
        min_samples=500,
        why=(
            "the retroactive merge band sits just below the dedupe bar, where "
            "paraphrase twins that slipped through actually land"
        ),
    ),
    GateSpec(
        setting="concept_evidence_admission_cosine",
        population=POP_EVIDENCE_FIT,
        objective=OBJ_SHARE_ABOVE,
        # Keep 98% of arriving evidence admissible. The bar is there for the
        # small tail that is about something else entirely, so asking it to
        # refuse much more than the measured 2.2% of the existing stock would
        # start costing real evidence -- 0.45 already refuses 15%.
        target=0.98,
        floor=0.25,
        ceiling=0.5,
        max_step=0.01,
        # Half the rolling sample, so a few quiet days cannot move it.
        min_samples=250,
        why=(
            "reinforcement should refuse only the evidence that is about "
            "something else, not the merely weak"
        ),
    ),
    GateSpec(
        setting="concept_contradiction_similarity_min",
        population=POP_PAIR_COSINE,
        objective=OBJ_SHARE_ABOVE,
        target=0.05,
        floor=0.4,
        ceiling=0.8,
        max_step=0.01,
        min_samples=500,
        why=(
            "two beliefs must be about the same thing before they can "
            "contradict; this is the floor of that band"
        ),
    ),
)


def _kind_floor_gates() -> tuple[GateSpec, ...]:
    """One observe-only spec per kind promotion floor.

    Nearly free to measure -- the rows are already loaded and this is a
    group-by-kind on the same confidence column -- and it is the data phase 4
    will need before those constants can become settings.
    """
    out: list[GateSpec] = []
    for kind in sorted(KIND_PROMOTION_FLOORS):
        spec = CONCEPT_KINDS.get(kind)
        generative = bool(spec) and spec.role == ROLE_GENERATIVE
        out.append(
            GateSpec(
                setting=kind_floor_key(kind),
                population=kind_population(kind),
                objective=OBJ_SHARE_ABOVE,
                # A generative kind is supposed to be easier to mint than a
                # value or an identity; asking the same share of both would
                # flatten exactly the distinction the roles exist to make.
                target=0.6 if generative else 0.45,
                floor=0.4,
                ceiling=0.85,
                max_step=0.02,
                min_samples=25,
                is_setting_field=False,
                kind=kind,
                why=(
                    f"what the {kind} pool itself says its promotion "
                    f"confidence floor should be"
                ),
            )
        )
    return tuple(out)


GATE_SPECS: tuple[GateSpec, ...] = (
    _APPLIED_GATES + _LIFECYCLE_GATES + _COSINE_GATES + _kind_floor_gates()
)


def spec_for(setting: str) -> GateSpec | None:
    for spec in GATE_SPECS:
        if spec.setting == setting:
            return spec
    return None


def applied_settings() -> tuple[str, ...]:
    """The gates that may actually reach ``MemorySettings``."""
    return tuple(spec.setting for spec in GATE_SPECS if spec.writable)


def kind_floor_defaults() -> dict[str, float]:
    """Current per-kind confidence floors, keyed by synthetic gate name."""
    return {
        kind_floor_key(kind): float(floors["min_confidence"])
        for kind, floors in KIND_PROMOTION_FLOORS.items()
    }


__all__ = [
    "GATE_SPECS",
    "GateSolution",
    "GateSpec",
    "MODE_APPLY",
    "MODE_OBSERVE",
    "OBJ_POOL_MULTIPLE",
    "OBJ_SHARE_ABOVE",
    "OBJ_UNDER_REACH",
    "POP_ACTIVE_CONFIDENCE",
    "POP_CANDIDATE_CONFIDENCE",
    "POP_CLUSTER_ENGAGED_RATE",
    "POP_CORE_POOL",
    "POP_EVIDENCE_FIT",
    "POP_FADED_CONFIDENCE",
    "POP_KIND_CONFIDENCE_PREFIX",
    "POP_OPENNESS_POOL",
    "POP_PAIR_COSINE",
    "POP_PROFILE_POOL",
    "applied_settings",
    "describe",
    "kind_floor_defaults",
    "kind_floor_key",
    "kind_population",
    "quantile",
    "solve",
    "solve_all",
    "spec_for",
]
