"""Pure L17b classifier: which concept movement is real evolution.

The L17a timeline in :mod:`app.core.concepts.concept_event_store` records
*everything* a concept does -- promotions, decay samples, contradictions,
merges. Most of it is not learning. This module answers the narrower
question the L17c learning-event pipeline needs: **did a belief actually
change, and is the change worth remembering years later?**

Shapes
------
``succession`` is the primary one, because it is how evolution actually
manifests in this codebase. A proposal at or above the synthesis dedupe
cosine is folded into the existing concept as evidence; one below it
becomes a *new row*. So a belief that sharpens over time shows up as an
old concept fading while a semantically-near new concept rises -- two
rows, not one edited row. Pairing those is the whole job.

The rest are single-concept arcs read off one trajectory: ``emergence``
(a belief was promoted), ``loss`` (its support fell away and nothing
replaced it), ``revival`` (it came back), and ``relabel`` (the L17
relabel pipeline rewrote its wording in place). Confidence movement with
no structural consequence is *noise* and is dropped.

A ``loss`` additionally requires that the belief was ever *held* --
reinforced at least once after promotion. A concept that decayed away
having never been re-observed was one inference nothing confirmed, and
calling that a change of mind would let ordinary graph maintenance
(a decay retune, the L22 sweep) narrate hundreds of losses she never
lived. Succession is exempt: a fade matched to a rising replacement
carries its own proof that the belief was real.

Salience is weighted by the kind's L16 plasticity band, inverted: equal
movement means more in a sticky belief (``value`` at 0.2) than in a
fluid one (``taste`` at 0.5), because the sticky one had to overcome
more inertia to move at all.

Everything here is I/O-free and takes plain data. The caller owns every
scan -- notably the succession candidate pairs, which must come from one
batched matmul rather than a per-concept nearest-neighbour query (see
``ConceptDriftWorker``).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


# The recognised drift shapes. ``noise`` is never emitted as a finding --
# it is the classifier's way of saying "movement, but not learning".
DRIFT_SHAPES = frozenset(
    {"succession", "emergence", "loss", "revival", "relabel"}
)

# Event types that mean the belief structurally moved, as opposed to the
# L17a ``confidence_sample`` trail markers which only record drift along
# a status plateau.
STRUCTURAL_EVENTS = frozenset(
    {
        "discovered",
        "promoted",
        "demoted",
        "dormant",
        "retired",
        "revived",
        "contradicted",
        "relabeled",
        "merged",
    }
)

# Statuses that mean the belief is no longer carried.
_FADED_STATUSES = frozenset({"dormant", "retired"})

# Per-shape starting salience, before the plasticity weight and the
# per-shape modifiers below.
_SHAPE_BASE: dict[str, float] = {
    "succession": 0.70,
    "revival": 0.55,
    "relabel": 0.50,
    "loss": 0.50,
    "emergence": 0.40,
}

_WORD_RE = re.compile(r"[a-z0-9']+")

# Wording differences that carry no meaning. A relabel that only adds
# "really" or drops "quite" is not a refinement worth a history entry.
_FILLER_WORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "being", "been",
        "to", "of", "in", "on", "at", "for", "with", "and", "or", "but",
        "that", "this", "it", "its", "he", "she", "they", "them", "his",
        "her", "their", "quite", "really", "very", "rather", "somewhat",
        "often", "tends", "tend", "seems", "seem", "kind", "sort",
    }
)


def _utc(value: Any) -> datetime | None:
    """Parse an ISO timestamp to an aware UTC datetime, or ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clamp01(value: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out:  # NaN
        return 0.0
    return max(0.0, min(1.0, out))


# ── label helpers (shared with the relabel gate) ──────────────────────

def normalize_label(text: Any) -> str:
    """Casefold, strip punctuation, and collapse whitespace.

    The comparison key for "is this the same wording". Used both by the
    classifier and by the relabel materiality gate, so that a change of
    capitalisation or a trailing full stop can never spend a history
    entry or trigger a re-embed.
    """
    words = _WORD_RE.findall(str(text or "").lower())
    return " ".join(words)


def _stem(word: str) -> str:
    """Fold the one inflection that churns most: a trailing plural /
    third-person ``s``. Applied to both sides of every comparison, so it
    only has to be consistent, not linguistically correct."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def label_tokens(text: Any) -> frozenset[str]:
    """Meaning-bearing tokens of a label, filler removed and stemmed."""
    return frozenset(
        _stem(word)
        for word in _WORD_RE.findall(str(text or "").lower())
        if word not in _FILLER_WORDS
    )


def is_material_relabel(
    old: Any, new: Any, *, min_token_delta: int = 1
) -> bool:
    """Is ``new`` a meaningfully different wording of ``old``?

    Requires both a normalized-text difference and at least
    ``min_token_delta`` meaning-bearing tokens added or removed, so
    punctuation, casing, and filler-word churn are all rejected before
    anything as expensive as an embed or an adjudication call.
    """
    new_norm = normalize_label(new)
    if not new_norm:
        return False
    if new_norm == normalize_label(old):
        return False
    old_tokens = label_tokens(old)
    new_tokens = label_tokens(new)
    if not new_tokens:
        return False
    delta = len(old_tokens ^ new_tokens)
    return delta >= max(1, int(min_token_delta))


def plasticity_weight(plasticity: float) -> float:
    """Invert the L16 plasticity band into a salience multiplier.

    A sticky belief (low plasticity) that moves at all has overcome more
    inertia than a fluid one moving the same distance, so it weighs more.
    Ranges ``0.6`` (fully fluid) to ``1.4`` (fully sticky); an unknown or
    malformed plasticity lands at the neutral ``1.0``.
    """
    try:
        band = float(plasticity)
    except (TypeError, ValueError):
        return 1.0
    if band != band or band <= 0.0 or band > 1.0:
        return 1.0
    return 0.6 + 0.8 * (1.0 - band)


# ── input structures (plain data; the caller owns all I/O) ────────────

@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    """One event in a concept's life, as the classifier needs it."""

    event_id: int
    event_type: str
    label: str = ""
    confidence: float = 0.0
    created_at: str = ""

    @property
    def structural(self) -> bool:
        return self.event_type in STRUCTURAL_EVENTS


@dataclass(frozen=True, slots=True)
class ConceptTrace:
    """A concept plus the slice of its timeline the classifier reads.

    ``points`` must be oldest-first and should carry both ends of the
    trajectory: the origin anchor and the recent movement. The plain
    oldest-first read in ``ConceptEventStore.trajectory`` is not enough on
    a long-lived concept, where ``confidence_sample`` rows can fill the
    window and hide everything recent.
    """

    concept_id: int
    kind: str = "identity"
    subject: str = "user"
    label: str = ""
    status: str = "candidate"
    confidence: float = 0.0
    plasticity: float = 0.5
    first_evidence_at: str = ""
    promoted_at: str = ""
    last_reinforced_at: str = ""
    points: tuple[TrajectoryPoint, ...] = ()
    evidence_refs: frozenset[tuple[str, str]] = frozenset()

    @property
    def ever_reinforced(self) -> bool:
        """Did evidence ever land on this belief *after* it was promoted?

        The same rule ``concept_quality.unreinforced_since_promotion`` and
        the L22 sweep script apply, read off the concept row rather than
        the event window: a ``reinforced`` row can fall outside the bounded
        trajectory read, and a belief that was held for months must not
        look unearned just because its confirmation scrolled off.
        """
        reinforced = _utc(self.last_reinforced_at)
        if reinforced is None:
            return False
        promoted = _utc(self.promoted_at)
        if promoted is None:
            return True
        return reinforced > promoted

    def age_days(self, now: datetime) -> float:
        born = _utc(self.first_evidence_at) or _utc(
            self.points[0].created_at if self.points else None
        )
        if born is None:
            return 0.0
        return max(0.0, (now - born).total_seconds() / 86400.0)

    def structural_points(self) -> tuple[TrajectoryPoint, ...]:
        return tuple(p for p in self.points if p.structural)

    def last_of(self, *types: str) -> TrajectoryPoint | None:
        wanted = frozenset(types)
        for point in reversed(self.points):
            if point.event_type in wanted:
                return point
        return None

    def confidence_span(self) -> float:
        """Peak-to-trough confidence movement across the read window."""
        values = [float(p.confidence) for p in self.points if p.confidence]
        if len(values) < 2:
            return 0.0
        return max(values) - min(values)


@dataclass(frozen=True, slots=True)
class SuccessionCandidate:
    """One (fading, rising) pair the worker's single matmul turned up."""

    old: ConceptTrace
    new: ConceptTrace
    cosine: float


# ── output ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class DriftFinding:
    """One classified change, ready to become an L17c learning event."""

    shape: str
    concept_id: int
    new_label: str
    old_label: str = ""
    prior_concept_id: int | None = None
    salience: float = 0.0
    plasticity: float = 0.5
    kind: str = "identity"
    subject: str = "user"
    confidence_delta: float = 0.0
    because: str = ""
    resolution: str = ""
    decisive_event_id: int = 0
    trigger_event_ids: tuple[int, ...] = ()
    evidence_refs: tuple[tuple[str, str], ...] = ()
    detected_at: str = ""
    # When the change *happened*, as opposed to when it was noticed. The
    # two coincide on the forward pass, and diverge by weeks on a backfill
    # -- or by everything, when a bulk status pass supplies the fade for a
    # replacement that rose a month earlier. The story is told from this;
    # ``detected_at`` stays for the debug surfaces.
    occurred_at: str = ""
    cosine: float | None = None

    def fingerprint(self) -> str:
        """Deterministic identity for idempotent persistence.

        Keyed on the *decisive event* rather than on detection time, so
        re-running the classifier over the same history produces the same
        fingerprint and the unique index absorbs the duplicate.
        """
        raw = "|".join(
            (
                self.shape,
                str(int(self.concept_id)),
                str(int(self.prior_concept_id or 0)),
                str(int(self.decisive_event_id)),
                normalize_label(self.old_label),
                normalize_label(self.new_label),
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class DriftThresholds:
    """Tunables, defaulted so a bare call still behaves sensibly."""

    min_salience: float = 0.35
    min_age_days: float = 3.0
    min_confidence_delta: float = 0.15
    succession_min_cosine: float = 0.55
    succession_max_cosine: float = 0.86
    succession_min_overlap: float = 0.25
    succession_window_days: float = 120.0
    succession_high_cosine: float = 0.75
    relabel_min_token_delta: int = 1
    max_findings: int = 12

    @classmethod
    def from_settings(cls, settings: Any) -> "DriftThresholds":
        """Read thresholds off a settings object, falling back per field."""
        base = cls()
        if settings is None:
            return base
        return cls(
            min_salience=float(
                getattr(settings, "concept_drift_min_salience",
                        base.min_salience)
            ),
            min_age_days=float(
                getattr(settings, "concept_drift_min_age_days",
                        base.min_age_days)
            ),
            min_confidence_delta=float(
                getattr(settings, "concept_drift_min_confidence_delta",
                        base.min_confidence_delta)
            ),
            succession_min_cosine=float(
                getattr(settings, "concept_drift_succession_min_cosine",
                        base.succession_min_cosine)
            ),
            succession_max_cosine=float(
                getattr(settings, "concept_drift_succession_max_cosine",
                        base.succession_max_cosine)
            ),
            succession_min_overlap=float(
                getattr(settings, "concept_drift_succession_min_overlap",
                        base.succession_min_overlap)
            ),
            succession_window_days=float(
                getattr(settings, "concept_drift_succession_window_days",
                        base.succession_window_days)
            ),
            relabel_min_token_delta=int(
                getattr(settings, "concept_drift_relabel_min_tokens",
                        base.relabel_min_token_delta)
            ),
            max_findings=int(
                getattr(settings, "concept_drift_max_findings",
                        base.max_findings)
            ),
        )


# ── succession (the primary shape) ────────────────────────────────────

def evidence_overlap(old: ConceptTrace, new: ConceptTrace) -> float:
    """Jaccard overlap of two concepts' evidence sources.

    The structural half of the succession argument: two labels can be
    near in embedding space by coincidence, but two beliefs resting on
    the *same remembered moments* are the same belief being re-described.
    """
    left = set(old.evidence_refs)
    right = set(new.evidence_refs)
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def classify_succession(
    candidate: SuccessionCandidate,
    *,
    now: datetime,
    thresholds: DriftThresholds,
    since_event_id: int = 0,
) -> DriftFinding | None:
    """Pair a fading concept with the rising one that replaced it."""
    old, new = candidate.old, candidate.new
    if old.concept_id == new.concept_id or new.concept_id <= 0:
        return None
    if old.subject != new.subject:
        return None
    cosine = float(candidate.cosine)
    # At or above the dedupe bar these would never have become two rows,
    # so a pair up there is a consolidation problem, not an evolution.
    if not (
        thresholds.succession_min_cosine
        <= cosine
        < thresholds.succession_max_cosine
    ):
        return None

    fade = old.last_of("retired", "dormant", "demoted")
    rise = new.last_of("promoted", "discovered")
    if fade is None or rise is None:
        return None
    if old.status not in _FADED_STATUSES and old.status != "candidate":
        return None

    fade_at = _utc(fade.created_at)
    rise_at = _utc(rise.created_at)
    if fade_at is None or rise_at is None:
        return None
    gap_days = abs((fade_at - rise_at).total_seconds()) / 86400.0
    if gap_days > max(1.0, thresholds.succession_window_days):
        return None

    overlap = evidence_overlap(old, new)
    # Either the two beliefs demonstrably rest on the same moments, or
    # they are close enough in wording that shared evidence is not the
    # only admissible argument. Neither alone at low strength qualifies.
    if (
        overlap < thresholds.succession_min_overlap
        and cosine < thresholds.succession_high_cosine
    ):
        return None

    decisive = max(int(fade.event_id), int(rise.event_id))
    if decisive <= int(since_event_id):
        return None

    strength = max(
        _clamp01(overlap / max(thresholds.succession_min_overlap, 0.01)),
        _clamp01(
            (cosine - thresholds.succession_min_cosine)
            / max(
                thresholds.succession_max_cosine
                - thresholds.succession_min_cosine,
                0.01,
            )
        ),
    )
    salience = _clamp01(
        _SHAPE_BASE["succession"]
        * plasticity_weight(old.plasticity)
        * (0.7 + 0.3 * strength)
    )
    refs = tuple(sorted(set(old.evidence_refs) | set(new.evidence_refs)))
    return DriftFinding(
        shape="succession",
        concept_id=new.concept_id,
        prior_concept_id=old.concept_id,
        old_label=old.label,
        new_label=new.label,
        salience=salience,
        plasticity=old.plasticity,
        kind=new.kind,
        subject=new.subject,
        confidence_delta=float(new.confidence) - float(old.confidence),
        because=(
            f"what looked like {old.label} turned out to be better "
            f"described as {new.label}"
        ),
        resolution=f"now held as {new.label}",
        decisive_event_id=decisive,
        trigger_event_ids=(int(fade.event_id), int(rise.event_id)),
        evidence_refs=refs[:12],
        detected_at=now.isoformat(),
        # Dated by the *rise*: the belief changed when the replacement took
        # over, not when the old row's status finally caught up. That catch-up
        # can be L3 decay, or a maintenance sweep stamping hundreds of fades
        # with one timestamp -- which would pile a whole graph's worth of
        # revisions onto whichever afternoon it ran.
        occurred_at=rise.created_at or now.isoformat(),
        cosine=cosine,
    )


# ── single-concept arcs ───────────────────────────────────────────────

def classify_trajectory(
    trace: ConceptTrace,
    *,
    now: datetime,
    thresholds: DriftThresholds,
    since_event_id: int = 0,
) -> DriftFinding | None:
    """Read one concept's own arc: relabel, revival, loss, or emergence.

    Returns at most one finding -- the most recent decisive move -- so a
    concept that promoted and then faded inside one window contributes
    the fade, not both.
    """
    if trace.concept_id <= 0 or not trace.points:
        return None
    structural = trace.structural_points()
    if not structural:
        return None
    if trace.age_days(now) < thresholds.min_age_days:
        return None

    decisive = structural[-1]
    if int(decisive.event_id) <= int(since_event_id):
        return None

    shape, old_label, new_label = _shape_for(trace, decisive)
    if shape is None:
        return None

    # You can only lose what you held. A belief that faded having never
    # once been reinforced was a single inference that nothing confirmed,
    # so its fade is bookkeeping rather than a change of mind -- and
    # letting it through would let decay tuning or a maintenance sweep
    # write hundreds of "I no longer believe" entries she never lived.
    # Succession is deliberately exempt: a fade matched to a rising
    # replacement is evidence in its own right that the belief was real.
    if shape == "loss" and not trace.ever_reinforced:
        return None

    # Noise gate: a move that neither changed the wording nor shifted
    # confidence meaningfully is drift along a plateau, not learning.
    span = trace.confidence_span()
    if (
        shape != "relabel"
        and span < thresholds.min_confidence_delta
        and shape not in ("loss", "revival")
    ):
        return None

    salience = _clamp01(
        _SHAPE_BASE[shape]
        * plasticity_weight(trace.plasticity)
        * (0.75 + 0.25 * _clamp01(span / 0.5))
    )
    first_conf = float(trace.points[0].confidence)
    return DriftFinding(
        shape=shape,
        concept_id=trace.concept_id,
        old_label=old_label,
        new_label=new_label,
        salience=salience,
        plasticity=trace.plasticity,
        kind=trace.kind,
        subject=trace.subject,
        confidence_delta=float(decisive.confidence) - first_conf,
        because=_because_for(shape, old_label, new_label),
        resolution=_resolution_for(shape, new_label),
        decisive_event_id=int(decisive.event_id),
        trigger_event_ids=(int(decisive.event_id),),
        evidence_refs=tuple(sorted(trace.evidence_refs))[:12],
        detected_at=now.isoformat(),
        occurred_at=decisive.created_at or now.isoformat(),
    )


def _shape_for(
    trace: ConceptTrace, decisive: TrajectoryPoint
) -> tuple[str | None, str, str]:
    """Map the decisive event onto a shape plus its old/new labels."""
    event = decisive.event_type
    if event == "relabeled":
        # The wording immediately before the rewrite is the old label.
        old = ""
        for point in reversed(trace.points):
            if point.event_id == decisive.event_id:
                continue
            if point.label:
                old = point.label
                break
        if not is_material_relabel(old, decisive.label):
            return None, "", ""
        return "relabel", old, decisive.label
    if event == "revived":
        return "revival", trace.label, trace.label
    if event in ("retired", "dormant", "demoted"):
        return "loss", trace.label, trace.label
    if event == "promoted":
        return "emergence", "", trace.label
    # ``discovered`` alone is not learning -- a candidate that never
    # promoted is a hypothesis. ``merged`` is consolidation cleanup, and
    # ``contradicted`` is already carried by the status move it causes.
    return None, "", ""


def _because_for(shape: str, old_label: str, new_label: str) -> str:
    if shape == "relabel":
        return f"the same understanding, said more precisely: {new_label}"
    if shape == "revival":
        return f"{new_label} came back after seeming to fade"
    if shape == "loss":
        return f"the support for {old_label} fell away and nothing replaced it"
    return f"enough separate moments pointed the same way to call it: {new_label}"


def _resolution_for(shape: str, new_label: str) -> str:
    if shape == "loss":
        return "no longer held"
    if shape == "revival":
        return f"held again: {new_label}"
    return f"now held as {new_label}"


# ── orchestration ─────────────────────────────────────────────────────

def detect_drift(
    traces: Sequence[ConceptTrace],
    succession_candidates: Iterable[SuccessionCandidate] = (),
    *,
    now: datetime,
    thresholds: DriftThresholds | None = None,
    since_event_id: int = 0,
) -> list[DriftFinding]:
    """Classify a batch of trajectories into salient learning events.

    Succession is resolved first and wins: a concept that faded *into* a
    successor has its plain ``loss`` suppressed, and the successor its
    ``emergence``, because the pair is the truer account of what happened
    than either half alone.
    """
    limits = thresholds or DriftThresholds()
    findings: list[DriftFinding] = []
    superseded: set[int] = set()
    successors: set[int] = set()

    best_by_old: dict[int, DriftFinding] = {}
    for candidate in succession_candidates:
        finding = classify_succession(
            candidate,
            now=now,
            thresholds=limits,
            since_event_id=since_event_id,
        )
        if finding is None or finding.salience < limits.min_salience:
            continue
        prior = int(finding.prior_concept_id or 0)
        # A faded belief can look near several risers; keep the strongest
        # single account rather than emitting a fan-out of near-duplicates.
        existing = best_by_old.get(prior)
        if existing is None or finding.salience > existing.salience:
            best_by_old[prior] = finding
    for finding in best_by_old.values():
        findings.append(finding)
        superseded.add(int(finding.prior_concept_id or 0))
        successors.add(int(finding.concept_id))

    for trace in traces:
        finding = classify_trajectory(
            trace,
            now=now,
            thresholds=limits,
            since_event_id=since_event_id,
        )
        if finding is None or finding.salience < limits.min_salience:
            continue
        if finding.shape == "loss" and finding.concept_id in superseded:
            continue
        if finding.shape == "emergence" and finding.concept_id in successors:
            continue
        findings.append(finding)

    findings.sort(key=lambda f: (-f.salience, f.concept_id, f.shape))
    return findings[: max(1, int(limits.max_findings))]


def build_traces(
    concepts: Iterable[Any],
    events_by_concept: Mapping[int, Sequence[Any]],
    evidence_by_concept: Mapping[int, Iterable[tuple[str, str]]] | None = None,
) -> list[ConceptTrace]:
    """Adapt store rows into the classifier's plain input structures.

    Kept here (rather than in the worker) so the shape of what the
    classifier reads is defined next to the classifier, and so tests can
    build traces from lightweight stand-ins.
    """
    evidence = evidence_by_concept or {}
    out: list[ConceptTrace] = []
    for concept in concepts:
        cid = int(getattr(concept, "concept_id", 0) or 0)
        if cid <= 0:
            continue
        points = tuple(
            TrajectoryPoint(
                event_id=int(getattr(event, "event_id", 0) or 0),
                event_type=str(getattr(event, "event_type", "") or ""),
                label=str(getattr(event, "label", "") or ""),
                confidence=float(getattr(event, "confidence", 0.0) or 0.0),
                created_at=str(getattr(event, "created_at", "") or ""),
            )
            for event in events_by_concept.get(cid, ())
        )
        out.append(
            ConceptTrace(
                concept_id=cid,
                kind=str(getattr(concept, "kind", "identity") or "identity"),
                subject=str(getattr(concept, "subject", "user") or "user"),
                label=str(getattr(concept, "label", "") or ""),
                status=str(getattr(concept, "status", "candidate")
                           or "candidate"),
                confidence=float(getattr(concept, "confidence", 0.0) or 0.0),
                plasticity=float(getattr(concept, "plasticity", 0.5) or 0.5),
                first_evidence_at=str(
                    getattr(concept, "first_evidence_at", "") or ""
                ),
                promoted_at=str(getattr(concept, "promoted_at", "") or ""),
                last_reinforced_at=str(
                    getattr(concept, "last_reinforced_at", "") or ""
                ),
                points=points,
                evidence_refs=frozenset(
                    (str(t), str(i)) for t, i in evidence.get(cid, ())
                ),
            )
        )
    return out


__all__ = [
    "DRIFT_SHAPES",
    "STRUCTURAL_EVENTS",
    "ConceptTrace",
    "DriftFinding",
    "DriftThresholds",
    "SuccessionCandidate",
    "TrajectoryPoint",
    "build_traces",
    "classify_succession",
    "classify_trajectory",
    "detect_drift",
    "evidence_overlap",
    "is_material_relabel",
    "label_tokens",
    "normalize_label",
    "plasticity_weight",
]
