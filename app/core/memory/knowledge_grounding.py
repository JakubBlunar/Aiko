"""Choose the strongest distinct learned facts for a prompt."""
from __future__ import annotations

import heapq
import math
from collections.abc import Iterable
from typing import Any

import numpy as np

from app.llm.embedder import cosine_similarity


def rank_knowledge(
    rows: Iterable[Any], query: np.ndarray, *, threshold: float, limit: int,
) -> tuple[list[tuple[float, str]], int]:
    """Return at most limit texts, preferring earlier rows on equal scores."""
    if limit <= 0:
        return [], 0
    try:
        query_norm = float(np.linalg.norm(query)) if query is not None else 0.0
    except (TypeError, ValueError):
        return [], 0

    # Rank by score and input order. Only the best occurrence of a text can
    # occupy a slot; an evicted occurrence cannot beat the current floor.
    heap: list[tuple[float, int, str]] = []
    selected: dict[str, tuple[float, int, str]] = {}
    candidates = 0
    for order, memory in enumerate(rows):
        embedding = getattr(memory, "embedding", None)
        if embedding is None or getattr(embedding, "size", 0) == 0:
            continue
        try:
            score = cosine_similarity(query, embedding, a_norm=query_norm)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score) or score < threshold:
            continue
        content = (getattr(memory, "content", "") or "").strip()
        if not content:
            continue
        candidates += 1
        key = content.lower()
        rank = (score, -order, content)
        if key in selected:
            if rank <= selected[key]:
                continue
        elif len(selected) >= limit:
            weakest = heap[0]
            if rank <= weakest:
                continue
            del selected[heapq.heappop(heap)[2].lower()]

        selected[key] = rank
        heapq.heappush(heap, rank)
        # Replacing an existing text leaves its old rank stale. Discard stale
        # roots and cap the physical heap even if duplicates keep improving.
        while heap and selected.get(heap[0][2].lower()) != heap[0]:
            heapq.heappop(heap)
        if len(heap) > 2 * limit:
            heap = list(selected.values())
            heapq.heapify(heap)

    ranked = sorted(selected.values(), reverse=True)
    return [(score, content) for score, _, content in ranked], candidates
