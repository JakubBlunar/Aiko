"""Self-noticing cues (K30 personality backlog).

Three pure detectors that let Aiko notice **her own** patterns: an
agreement streak across recent replies, a flat-affect stretch with no
movement, and a repeated-thought spike where her just-built reply is
near-duplicate of one of her recent replies. K20 covers Jacob's trust
in Aiko; K30 closes the symmetric loop -- Aiko's read of herself.

The three detectors are intentionally independent pure functions with
frozen-dataclass results so callers can render any subset, and so the
``SessionController`` can decide where each one's state lives:

* :func:`detect_agreement_streak` -- stateless, takes the last N
  rendered (post-tag-stripping) assistant replies and counts whole-word
  agreement / pushback tokens. The provider reads recent replies from
  SQLite per turn (K23-style); no in-memory ring is required.
* :func:`detect_flat_affect` -- stateless, takes a window of
  ``(valence, arousal, reaction)`` triples populated post-turn on
  :class:`SessionController`. There is no ring buffer on
  :class:`AffectState` itself (only the scalar persisted state), so
  K30 owns its own deque.
* :func:`detect_repeated_thought` -- stateless, compares the
  just-finished reply's unit-norm embedding to a ring of prior reply
  embeddings (same vector :class:`CallbackDetector` already computes
  synchronously in ``post_turn_mixin``, so no extra embed call is
  needed).

All three short-circuit cleanly on empty / under-warmup input and
never raise -- the inner-life provider should be able to call them in
sequence without try/except. See
[`docs/personality-backlog/patterns.md`](../../../docs/personality-backlog/patterns.md)
"K30. Self-noticing cues" for the design rationale and the
``neutral / calm / friendly`` low-band reaction list.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np


# ── Defaults (exported for settings + tests) ─────────────────────────────


DEFAULT_WINDOW: int = 6
DEFAULT_WARMUP: int = 4
DEFAULT_AGREEMENT_THRESHOLD: float = 0.80
DEFAULT_MAX_PUSHBACK: int = 0
DEFAULT_FLAT_VALENCE_RANGE: float = 0.10
DEFAULT_FLAT_AROUSAL_RANGE: float = 0.10
DEFAULT_REPEATED_COSINE_THRESHOLD: float = 0.85


# Reactions that count as "even-keel" for the flat-affect detector.
# A reply tagged with one of these does NOT count as "Aiko actually
# landed somewhere". Per the K30 spec at
# ``docs/personality-backlog/patterns.md`` L315-316 (deliberately
# excludes ``thoughtful`` -- that's a real landing, not a flat one;
# see K8's separate ``DEFAULT_EXCLUDED_REACTIONS`` for the empathetic
# group, which overlaps but is not the same set).
LOW_BAND_REACTIONS: frozenset[str] = frozenset({"neutral", "calm", "friendly"})


# Whole-word agreement / pushback token sets, lowercased. The detector
# scans replies via ``re.findall(_TOKEN_RE, text.lower())`` and counts
# matches against these frozensets. Multi-word phrases live in the
# adjacent ``_AGREEMENT_PHRASES`` / ``_PUSHBACK_PHRASES`` tuples and
# are matched via a separate substring scan because ``\\b`` can't span
# a multi-word phrase cleanly. Keep the lists short and high-signal;
# false positives here cost a wrongly-fired Heads-up per ~6 turns.
_AGREEMENT_TOKENS: frozenset[str] = frozenset({
    "yeah",
    "yep",
    "yup",
    "yes",
    "totally",
    "exactly",
    "absolutely",
    "definitely",
    "agreed",
    "right",
    "true",
    "ok",
    "okay",
    "sure",
    "uhuh",
})
_AGREEMENT_PHRASES: tuple[str, ...] = (
    "for sure",
    "of course",
    "right?",
    "right!",
    "makes sense",
    "good point",
    "fair point",
    "you're right",
    "youre right",
    "no doubt",
)
_PUSHBACK_TOKENS: frozenset[str] = frozenset({
    "actually",
    "but",
    "however",
    "though",
    "disagree",
})
_PUSHBACK_PHRASES: tuple[str, ...] = (
    "not sure",
    "not so sure",
    "i'd push back",
    "id push back",
    "i'd argue",
    "id argue",
    "i'd say",
    "id say",
    "hmm",
    "on the other hand",
    "wait,",
    "wait -",
    "i don't think",
    "i dont think",
    "not quite",
    "kind of disagree",
)


_TOKEN_RE: re.Pattern[str] = re.compile(r"[a-z']+")


# ── Result types ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgreementStreakResult:
    """One agreement-streak verdict.

    ``agreement_share`` is the fraction of scanned replies that
    contained at least one agreement token (not the per-reply
    proportion of tokens -- a reply with one "yeah" counts the same
    as a reply with five "yeah"s). Same for ``pushback_share``.
    ``sample_size`` is the number of replies actually scored (could
    be below ``min_samples`` when ``fired=False``).
    """

    fired: bool
    agreement_share: float
    pushback_share: float
    sample_size: int


@dataclass(frozen=True, slots=True)
class FlatAffectResult:
    """One flat-affect verdict.

    ``valence_range`` / ``arousal_range`` are ``max - min`` across the
    sampled window (not standard deviation -- the design doc asks for
    "has the affect moved", which is a range question, not a variance
    question; range catches a slow drift just as cleanly as a flat
    line and avoids the divide-by-n-1 quirk on tiny samples).
    ``notable_reaction_count`` is the number of samples whose reaction
    label was OUTSIDE :data:`LOW_BAND_REACTIONS` -- a single non-low
    reaction in the window kills the streak.
    """

    fired: bool
    valence_range: float
    arousal_range: float
    notable_reaction_count: int
    sample_size: int


@dataclass(frozen=True, slots=True)
class RepeatedThoughtResult:
    """One repeated-thought verdict.

    ``max_cosine`` is the highest cosine similarity between
    ``current_vec`` and any vector in ``prior_vecs``. ``matched_index``
    is the position in ``prior_vecs`` (0-based) where the max landed;
    ``-1`` when ``prior_vecs`` is empty or every vector is degenerate.
    Callers can map the index back to "1 turn ago" / "2 turns ago" as
    needed; the detector itself stays index-only to keep it pure.
    """

    fired: bool
    max_cosine: float
    matched_index: int


# ── Public detectors ─────────────────────────────────────────────────────


def detect_agreement_streak(
    recent_replies: Sequence[str],
    *,
    min_samples: int = DEFAULT_WARMUP,
    agreement_threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    max_pushback: int = DEFAULT_MAX_PUSHBACK,
) -> AgreementStreakResult:
    """Classify the recent-reply window for an agreement streak.

    Counts per-reply, not per-token: a reply registers as
    "agreement" if it contains any agreement token / phrase, and as
    "pushback" if it contains any pushback token / phrase. A single
    reply can be both (it just contributes to both shares); that's
    rare in practice and the streak still requires the agreement
    share to exceed the threshold AND the pushback count to be at or
    below ``max_pushback``.

    Empty input, all-whitespace input, or below-warmup input returns
    ``fired=False`` with the actual ``sample_size`` -- never raises.
    """
    cleaned = [r for r in recent_replies if r and r.strip()]
    sample_size = len(cleaned)
    if sample_size < max(1, int(min_samples)):
        return AgreementStreakResult(
            fired=False,
            agreement_share=0.0,
            pushback_share=0.0,
            sample_size=sample_size,
        )

    agreement_hits = 0
    pushback_hits = 0
    for reply in cleaned:
        low = reply.lower()
        tokens = set(_TOKEN_RE.findall(low))
        agreed = bool(tokens & _AGREEMENT_TOKENS) or any(
            phrase in low for phrase in _AGREEMENT_PHRASES
        )
        pushed = bool(tokens & _PUSHBACK_TOKENS) or any(
            phrase in low for phrase in _PUSHBACK_PHRASES
        )
        if agreed:
            agreement_hits += 1
        if pushed:
            pushback_hits += 1

    agreement_share = agreement_hits / sample_size
    pushback_share = pushback_hits / sample_size
    threshold = max(0.0, min(1.0, float(agreement_threshold)))
    max_push = max(0, int(max_pushback))
    fired = agreement_share >= threshold and pushback_hits <= max_push
    return AgreementStreakResult(
        fired=fired,
        agreement_share=agreement_share,
        pushback_share=pushback_share,
        sample_size=sample_size,
    )


def detect_flat_affect(
    samples: Sequence[tuple[float, float, str | None]],
    *,
    min_samples: int = DEFAULT_WARMUP,
    valence_range_threshold: float = DEFAULT_FLAT_VALENCE_RANGE,
    arousal_range_threshold: float = DEFAULT_FLAT_AROUSAL_RANGE,
    low_band_reactions: frozenset[str] = LOW_BAND_REACTIONS,
) -> FlatAffectResult:
    """Classify the affect-sample window for a flat-affect streak.

    Fires only when BOTH:

    * the valence range AND the arousal range across the window sit
      at or below their respective thresholds, AND
    * no sampled reaction label lies outside ``low_band_reactions``.

    The AND clause matches the K30 spec at
    ``docs/personality-backlog/patterns.md`` L313-318: a reply with a
    real reaction (e.g. ``playful``, ``annoyed``, ``thoughtful``)
    counts as Aiko landing somewhere, even if the scalar affect
    happens to be inside the threshold band that turn. Conversely, a
    flat scalar window without a single notable reaction reads as
    "she's just nodding along".

    Below-warmup input returns ``fired=False`` with the actual ranges
    computed across whatever samples were supplied (so the MCP
    diagnostic state still shows the live numbers without firing).
    """
    triples = [t for t in samples if t is not None]
    sample_size = len(triples)
    if sample_size == 0:
        return FlatAffectResult(
            fired=False,
            valence_range=0.0,
            arousal_range=0.0,
            notable_reaction_count=0,
            sample_size=0,
        )

    vals = [float(t[0]) for t in triples]
    aros = [float(t[1]) for t in triples]
    valence_range = max(vals) - min(vals)
    arousal_range = max(aros) - min(aros)

    low_band = frozenset(r.strip().lower() for r in low_band_reactions if r)
    notable = 0
    for _v, _a, reaction in triples:
        if reaction is None:
            continue
        label = str(reaction).strip().lower()
        if not label:
            continue
        if label not in low_band:
            notable += 1

    val_thresh = max(0.0, float(valence_range_threshold))
    aro_thresh = max(0.0, float(arousal_range_threshold))
    min_n = max(1, int(min_samples))
    fired = (
        sample_size >= min_n
        and valence_range <= val_thresh
        and arousal_range <= aro_thresh
        and notable == 0
    )
    return FlatAffectResult(
        fired=fired,
        valence_range=valence_range,
        arousal_range=arousal_range,
        notable_reaction_count=notable,
        sample_size=sample_size,
    )


def detect_repeated_thought(
    current_vec: np.ndarray | None,
    prior_vecs: Iterable[np.ndarray | None],
    *,
    threshold: float = DEFAULT_REPEATED_COSINE_THRESHOLD,
) -> RepeatedThoughtResult:
    """Compare ``current_vec`` against ``prior_vecs`` for near-duplicates.

    Returns ``fired=True`` when the maximum cosine similarity against
    any prior vector meets or exceeds ``threshold``. Cosine is a plain
    dot product on unit-norm vectors -- :class:`Embedder` already
    returns unit-norm vectors, but we still re-normalize defensively
    so a caller passing a stale, partially-zeroed vector doesn't
    spuriously fire or silently produce a NaN.

    ``None`` / zero-magnitude / shape-mismatched priors are skipped
    rather than raising. An empty ``prior_vecs`` returns
    ``fired=False, max_cosine=0.0, matched_index=-1``.
    """
    if current_vec is None:
        return RepeatedThoughtResult(
            fired=False, max_cosine=0.0, matched_index=-1
        )
    cur = np.asarray(current_vec, dtype=np.float32)
    cur_norm = float(np.linalg.norm(cur))
    if not math.isfinite(cur_norm) or cur_norm <= 0.0:
        return RepeatedThoughtResult(
            fired=False, max_cosine=0.0, matched_index=-1
        )
    cur_unit = cur / cur_norm

    max_cos = -1.0
    matched = -1
    for idx, vec in enumerate(prior_vecs):
        if vec is None:
            continue
        arr = np.asarray(vec, dtype=np.float32)
        if arr.shape != cur_unit.shape:
            continue
        norm = float(np.linalg.norm(arr))
        if not math.isfinite(norm) or norm <= 0.0:
            continue
        cos = float(np.dot(cur_unit, arr / norm))
        if cos > max_cos:
            max_cos = cos
            matched = idx

    if matched < 0:
        return RepeatedThoughtResult(
            fired=False, max_cosine=0.0, matched_index=-1
        )
    thresh = max(0.0, min(1.0, float(threshold)))
    return RepeatedThoughtResult(
        fired=max_cos >= thresh,
        max_cosine=max_cos,
        matched_index=matched,
    )


__all__ = [
    "DEFAULT_AGREEMENT_THRESHOLD",
    "DEFAULT_FLAT_AROUSAL_RANGE",
    "DEFAULT_FLAT_VALENCE_RANGE",
    "DEFAULT_MAX_PUSHBACK",
    "DEFAULT_REPEATED_COSINE_THRESHOLD",
    "DEFAULT_WARMUP",
    "DEFAULT_WINDOW",
    "LOW_BAND_REACTIONS",
    "AgreementStreakResult",
    "FlatAffectResult",
    "RepeatedThoughtResult",
    "detect_agreement_streak",
    "detect_flat_affect",
    "detect_repeated_thought",
]
