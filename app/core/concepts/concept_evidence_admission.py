"""L31: what a concept is allowed to accept as evidence.

Creation is gated (a new concept must clear its kind's ``min_sources`` /
``min_chain`` / ``directional`` bars) but *reinforcement* was not gated at
all. ``resolve_reinforces`` checks only that the id the LLM named appears in
the list of 40 it was shown, and the worker then attached every cited source
with no similarity check of any kind. Two failure shapes grew out of that,
and they need two different bars.

**Contamination.** One ``aspiration/user`` row ("deepening emotional and
physical intimacy with Aiko from functional interaction to profound
relational bonding") accumulated 97 sources, among them "Jacob really
enjoyed Chainsaw Man's opening song" and "organizing the snack stash by
moving cookies to the kitchenette". Those are not weak evidence for the
belief; they are evidence for something else that happened to be the nearest
label on the shown list. :data:`ADMISSION_COS` refuses them.

**Accretion.** One ``ritual/relationship`` row ("tender, playful wind-downs
where vulnerability meets gentle teasing") ended up citing 145 of the 158
``shared_moment`` memories in the graph -- 92% of them. Nothing there is
off-topic: a label that vague really is near everything affectionate, and its
lowest-cosine evidence still sits at 0.385. Only :data:`MAX_SOURCES` bounds
that shape.

Where the numbers came from
--------------------------
Measured, not chosen. Over all 6091 live evidence edges, the cosine between
a piece of evidence and the concept label it supports runs p1 0.324, p5
0.384, p10 0.424, p50 0.574, p90 0.756.

:data:`ADMISSION_COS` at 0.35 refuses 2.2% of the existing stock. It catches
every piece hand-read as wrong on the contaminated row (0.243, 0.311, 0.328)
while that row's genuine evidence sits at 0.60-0.68. 0.40 refuses 6.7% and
0.45 refuses 15.1%, which is where legitimate spread starts going with it.

:data:`MAX_SOURCES` at 24 is the 99th percentile of
``distinct_source_count`` (p50 4, p90 10, p95 13), so it binds on about one
concept in a hundred. It is deliberately far above where it would *matter*:
``confidence_target`` saturates at its 0.97 cap by 8 distinct sources, so
everything past the eighth already bought nothing, and a ceiling at 24 leaves
3x headroom over that. No concept can lose confidence or fail a promotion
floor by being capped here.

Forward-only
------------
This admits or refuses *new* sources. It never removes an edge a concept
already holds, so the rows that grew before the gate existed keep their
history and simply stop growing.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

log = logging.getLogger("app.concept_evidence_admission")

#: Cosine below which a source is not about the concept it was cited for.
#: See the module docstring for the measurement behind it.
ADMISSION_COS = 0.35

#: The most distinct sources one concept may accumulate.
MAX_SOURCES = 24

#: Why a source was refused, for logs and counters.
REFUSED_OFFTOPIC = "offtopic"
REFUSED_FULL = "full"

#: ``kv_meta`` key holding the rolling sample of measured fit cosines. The
#: synthesis worker writes it and the L45 gate tuner reads it as an observed
#: population, so the key lives here rather than in either of them.
FIT_SAMPLE_KEY = "concept_synth.evidence_fit"

#: How many cosines the rolling sample keeps. Enough to estimate the shape of
#: the inflow distribution (the tuner's ``min_samples`` is a fraction of it)
#: and small enough that the row stays a few kilobytes.
FIT_SAMPLE_CAP = 500


@dataclass(slots=True)
class Refusal:
    """One source that did not get in, and why."""

    node: tuple[str, str]
    reason: str
    cosine: float | None = None


@dataclass(slots=True)
class Admission:
    """The verdict for one reinforcement."""

    #: Sources to write edges for, in the order they were cited (the
    #: ``sequence`` evidence model stamps ``ordinal`` by position, so order
    #: has to survive the filter).
    kept: list[tuple[str, str]] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    #: Cosines actually measured, for the gate's observed population. Only
    #: new sources appear here -- re-citing a source the concept already
    #: holds says nothing about where the bar should sit.
    cosines: list[float] = field(default_factory=list)
    #: How many of ``kept`` were sources the concept did not already hold,
    #: i.e. what this reinforcement actually added.
    admitted: int = 0

    @property
    def offtopic(self) -> list[Refusal]:
        return [r for r in self.refused if r.reason == REFUSED_OFFTOPIC]

    @property
    def full(self) -> list[Refusal]:
        return [r for r in self.refused if r.reason == REFUSED_FULL]

    @property
    def reinforced(self) -> bool:
        """Whether this counts as the concept having been reinforced.

        True when something was admitted, and **also** true when the only
        thing standing in the way was the ceiling. That second case is not a
        nicety: ``last_reinforced_at`` is what the L3 engine reads to decide
        a concept is still being observed, and the L46 dormancy TTL retires
        a row by wall-clock silence. A capped concept whose timestamp froze
        would drift ``active -> dormant -> retired`` while the evidence for
        it kept arriving -- the gate would quietly delete the graph's
        best-supported beliefs through a side door. Evidence that was
        *refused as off-topic* is different: nothing about the belief was
        observed, so nothing should say it was.
        """
        if self.kept:
            return True
        return bool(self.full)


def admit(
    evidence: "list[tuple[str, str]]",
    *,
    label_vector: Any,
    vectors: "dict[tuple[str, str], Any]",
    existing_sources: "set[tuple[str, str]]",
    floor: float = ADMISSION_COS,
    ceiling: int = MAX_SOURCES,
) -> Admission:
    """Decide which cited sources a concept may take on.

    ``vectors`` maps a source node to its embedding; the caller resolves
    those, because only it knows how (a ``("cluster", rep)`` node is keyed
    by a representative *memory* id, not a topic-cluster id). A node absent
    from the map, or carrying an unusable vector, is **admitted**: failing
    open risks one loose edge, while failing closed would silently starve a
    concept every time an embedding was missing or the embedding model was
    swapped.

    ``label_vector`` is the concept's own embedding, which is derived from
    its label. An unembedded concept skips the cosine bar entirely for the
    same reason -- there is nothing to compare against.

    ``floor <= 0`` disables the cosine bar and ``ceiling <= 0`` disables the
    cap, which is how the gate is turned off; there is no separate flag.

    Re-citing a source the concept already holds always passes. The edge
    write upserts, so it cannot grow ``distinct_source_count``, and refusing
    it would mean a concept at its ceiling lost the ability to restate its
    own evidence.
    """
    out = Admission()
    unit_label = _unit(label_vector)
    check_cosine = float(floor) > 0.0 and unit_label.size > 0
    cap = int(ceiling)
    room = None if cap <= 0 else max(0, cap - len(existing_sources))

    admitted_new: set[tuple[str, str]] = set()
    for node in evidence:
        if node in existing_sources or node in admitted_new:
            out.kept.append(node)
            continue

        if check_cosine:
            vec = _unit(vectors.get(node))
            if vec.size == unit_label.size and vec.size > 0:
                cos = float(np.dot(unit_label, vec))
                out.cosines.append(cos)
                if cos < float(floor):
                    out.refused.append(
                        Refusal(node=node, reason=REFUSED_OFFTOPIC, cosine=cos)
                    )
                    continue

        if room is not None and len(admitted_new) >= room:
            out.refused.append(Refusal(node=node, reason=REFUSED_FULL))
            continue

        admitted_new.add(node)
        out.kept.append(node)

    out.admitted = len(admitted_new)
    return out


def load_fit_sample(kv_get: "Callable[[str], str | None]") -> list[float]:
    """The rolling sample of measured evidence-fit cosines.

    Never raises: an unreadable or malformed row reports an empty sample,
    which the tuner reads as "not enough data yet" rather than as a reason
    to move a bar.
    """
    try:
        raw = kv_get(FIT_SAMPLE_KEY)
    except Exception:
        log.debug("evidence fit sample read failed", exc_info=True)
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[float] = []
    for item in parsed:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def save_fit_sample(
    kv_set: "Callable[[str, str], None]",
    values: "list[float]",
    *,
    cap: int = FIT_SAMPLE_CAP,
) -> None:
    """Persist the newest ``cap`` cosines, oldest dropped first."""
    keep = [round(float(v), 4) for v in values[-max(1, int(cap)):]]
    try:
        kv_set(FIT_SAMPLE_KEY, json.dumps(keep))
    except Exception:
        log.debug("evidence fit sample write failed", exc_info=True)


def _unit(vec: Any) -> "np.ndarray":
    """Unit-norm float32 view of ``vec``, or an empty array if unusable."""
    if vec is None:
        return np.zeros(0, dtype=np.float32)
    try:
        arr = np.asarray(vec, dtype=np.float32).ravel()
    except (TypeError, ValueError):
        return np.zeros(0, dtype=np.float32)
    if arr.size == 0:
        return np.zeros(0, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return np.zeros(0, dtype=np.float32)
    return arr / norm


__all__ = [
    "ADMISSION_COS",
    "FIT_SAMPLE_CAP",
    "FIT_SAMPLE_KEY",
    "MAX_SOURCES",
    "REFUSED_FULL",
    "REFUSED_OFFTOPIC",
    "Admission",
    "Refusal",
    "admit",
    "load_fit_sample",
    "save_fit_sample",
]
