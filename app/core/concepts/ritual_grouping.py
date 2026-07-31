"""L7 - recurring shared-moment grouping (pure single-link clustering).

Relationship *ritual* concepts (the durable "this is a thing you two do"
pattern -- Friday debugging evenings, the pre-release nerves-and-tea, the
end-of-day check-in) are mined from ``shared_moment`` memories. Those rows
already carry a ``vibe`` and a ``when`` in their metadata and an embedding
on the row; what's missing is the *grouping* -- which moments are really the
same recurring ritual rather than a one-off.

This module is the light, pure core of that grouping: single-link
agglomerative clustering over the moment embeddings (two moments join when
their cosine clears a threshold; components are the transitive closure), then
each surviving component (``>= min_size`` members) is annotated with a
dominant ``vibe`` and an optional weekday hint parsed from ``when``. The
worker's ``_run_ritual_pass`` turns each :class:`RitualGroup` into a proposer
input; the proposer names the ritual.

Pure + dependency-light (numpy only, for the cosine): no store / settings /
LLM imports, so it unit-tests in isolation. The worker does the memory I/O
and hands in :class:`MomentInput` rows (built via :func:`moment_from_memory`).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np


# Default single-link cosine threshold; the worker overrides from settings.
_DEFAULT_SIMILARITY = 0.6
_DEFAULT_MIN_SIZE = 3

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# How much moment text to keep on a member line for the proposer prompt.
_MAX_TEXT_CHARS = 160


@dataclass(slots=True)
class MomentInput:
    """One ``shared_moment`` row, normalised for grouping. ``embedding`` is a
    vector (any sequence / ndarray); ``vibe`` / ``when`` come from the memory
    metadata. Kept separate from :class:`Memory` so the grouping can be
    unit-tested without the store."""

    id: int
    embedding: Any
    text: str
    vibe: str = "general"
    when: str = ""
    salience: float = 0.0


@dataclass(slots=True, frozen=True)
class MomentLite:
    """A group member, trimmed for the proposer prompt."""

    id: int
    text: str
    vibe: str
    weekday: str | None


@dataclass(slots=True, frozen=True)
class RitualGroup:
    """A recurring cluster of shared moments -- a candidate ritual."""

    member_ids: tuple[int, ...]
    dominant_vibe: str
    weekday_hint: str | None
    members: tuple[MomentLite, ...]

    @property
    def size(self) -> int:
        return len(self.member_ids)


def moment_from_memory(mem: Any) -> MomentInput | None:
    """Build a :class:`MomentInput` from a ``shared_moment`` memory row, or
    ``None`` when the row has no usable embedding. Reads ``vibe`` / ``when`` /
    ``what`` from the metadata bag, falling back to the row content /
    ``created_at`` when a field is absent."""
    try:
        mid = int(mem.id)
    except (TypeError, ValueError, AttributeError):
        return None
    emb = getattr(mem, "embedding", None)
    if emb is None:
        return None
    vec = np.asarray(emb, dtype=np.float32)
    if vec.size == 0:
        return None
    meta = getattr(mem, "metadata", None) or {}
    text = str(meta.get("what") or getattr(mem, "content", "") or "").strip()
    if not text:
        return None
    return MomentInput(
        id=mid,
        embedding=vec,
        text=text,
        vibe=str(meta.get("vibe") or "general").strip().lower() or "general",
        when=str(meta.get("when") or getattr(mem, "created_at", "") or ""),
        salience=float(getattr(mem, "salience", 0.0) or 0.0),
    )


def _normalise(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(v))
    if norm <= 0.0:
        return v
    return v / norm


def _weekday_of(when: str) -> str | None:
    text = (when or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        # Tolerate a trailing "Z" and other minor ISO slips.
        try:
            ts = datetime.fromisoformat(re.sub(r"Z$", "+00:00", text))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return _WEEKDAYS[ts.weekday()]


def _dominant_vibe(vibes: Sequence[str]) -> str:
    """Most common non-``general`` vibe, falling back to ``general`` when a
    group has no meaningful vibe signal."""
    counts = Counter(v for v in vibes if v and v != "general")
    if counts:
        return counts.most_common(1)[0][0]
    return "general"


def _weekday_hint(whens: Sequence[str]) -> str | None:
    """A weekday hint only when a *majority* of the parseable members land on
    the same weekday (and there are at least two such members) -- otherwise a
    ritual isn't really day-anchored."""
    days = [d for d in (_weekday_of(w) for w in whens) if d]
    if len(days) < 2:
        return None
    day, n = Counter(days).most_common(1)[0]
    return day if n * 2 >= len(days) else None


def _trim(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > _MAX_TEXT_CHARS:
        return text[: _MAX_TEXT_CHARS - 1].rstrip() + "\u2026"
    return text


def group_moments(
    moments: Sequence[MomentInput],
    *,
    min_size: int = _DEFAULT_MIN_SIZE,
    similarity: float = _DEFAULT_SIMILARITY,
) -> list[RitualGroup]:
    """Single-link cosine grouping of shared moments into ritual candidates.

    Two moments are linked when their embedding cosine is ``>= similarity``;
    a group is a connected component of that link graph. Only components with
    ``>= min_size`` members survive, each annotated with a dominant vibe +
    optional weekday hint and its member :class:`MomentLite`s (salience desc).
    Groups are returned largest-first for deterministic downstream capping.
    """
    rows = [m for m in moments if m is not None]
    n = len(rows)
    min_size = max(2, int(min_size))
    if n < min_size:
        return []
    thr = max(-1.0, min(1.0, float(similarity)))

    mat = np.vstack([_normalise(m.embedding) for m in rows]).astype(np.float32)
    # Cosine matrix (rows are unit vectors); single-link => connect any pair
    # whose similarity clears the threshold, then take connected components.
    sims = mat @ mat.T

    parent = list(range(n))

    def _find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if float(sims[i, j]) >= thr:
                _union(i, j)

    comps: dict[int, list[int]] = {}
    for idx in range(n):
        comps.setdefault(_find(idx), []).append(idx)

    groups: list[RitualGroup] = []
    for members in comps.values():
        if len(members) < min_size:
            continue
        members_sorted = sorted(
            members, key=lambda k: rows[k].salience, reverse=True
        )
        mrows = [rows[k] for k in members_sorted]
        vibe = _dominant_vibe([m.vibe for m in mrows])
        hint = _weekday_hint([m.when for m in mrows])
        lites = tuple(
            MomentLite(
                id=m.id,
                text=_trim(m.text),
                vibe=m.vibe,
                weekday=_weekday_of(m.when),
            )
            for m in mrows
        )
        groups.append(
            RitualGroup(
                member_ids=tuple(m.id for m in mrows),
                dominant_vibe=vibe,
                weekday_hint=hint,
                members=lites,
            )
        )

    groups.sort(key=lambda g: g.size, reverse=True)
    return groups


__all__ = [
    "MomentInput",
    "MomentLite",
    "RitualGroup",
    "group_moments",
    "moment_from_memory",
]
