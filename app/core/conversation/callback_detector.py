"""Callback / inside-joke detector (K22 personality backlog).

Per-turn detector that fires when Aiko's just-emitted reply
semantically reaches back to an older memory the conversation had
parked. The signal is *write-only*: hits stamp ``metadata.callback_count``,
bump ``salience`` + ``revival_score``, and stash a
``last_callback_at`` timestamp on the row. The LLM is never directly
told "you just made a callback" -- the reinforcement lands entirely
through :class:`app.core.rag.rag_retriever.RagRetriever`'s read-side score
bonus on rows with ``callback_count >= 1``, so over time the memories
Aiko has actually managed to weave back in become preferred over
equally-relevant-but-never-cited siblings.

Design choices:

- **Stateless module, not a class.** All state lives on the memory
  rows themselves (``metadata.callback_count``, ``last_callback_at``)
  exactly like the sibling K8 / K17 detectors. The post-turn hook
  passes the embedded reply vector and the active memory store in
  per call.
- **Allow-list of kinds.** Only the kinds that "callback well"
  (factual recall, shared moments, catchphrases, user-tagged events
  + self-disclosures) are eligible. Ephemeral kinds (curiosity_seed,
  knowledge_gap, agenda, promise, goal_progress, milestone) are
  explicitly excluded -- those are dynamic-state rows, not the right
  targets for "she remembered the silly thing I said".
- **Age floor (default 3 days).** A memory from earlier in the same
  session isn't a callback; it's just normal context. The whole
  point of K22 is the cross-session beat.
- **Per-row cooldown (default 24h).** Prevents a string of similar
  replies on a similar topic from spamming the same memory with
  redundant callback bumps.
- **Top-K cap (default 3).** One Aiko reply rarely calls back to
  more than a handful of beats; capping prevents a single
  high-similarity sentence from blanket-bumping ten near-duplicates.

The detector is invoked from
:meth:`app.core.session.post_turn_mixin.PostTurnMixin._post_turn_inner_life`;
the RAG read-side bonus is wired in
:class:`app.core.rag.rag_retriever.RagRetriever`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import numpy as np


log = logging.getLogger("app.callback_detector")


# Allow-list of memory kinds that are eligible callback targets. The
# spirit is "things {user_name} said or did that Aiko might
# meaningfully reach back to later". Excludes:
#
# - ``curiosity_seed`` / ``knowledge_gap`` / ``open_question`` — open
#   loops, not closed beats
# - ``agenda`` / ``promise`` / ``goal`` / ``goal_progress`` /
#   ``milestone`` — dynamic-state rows owned by other workers
# - ``self`` is kept (Aiko's own self-disclosures are valid callback
#   targets — "I told you last week I get nervous around new people")
CALLBACK_KINDS: frozenset[str] = frozenset({
    "fact",
    "preference",
    "event",
    "relationship",
    "self",
    "self_tagged",
    "shared_moment",
    "catchphrase",
})


# ── Result type ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CallbackHit:
    """One detected callback. Returned from :func:`detect`, consumed by
    :func:`record` to apply the memory mutations.
    """

    memory_id: int
    kind: str
    similarity: float
    age_days: int
    prior_count: int


# ── Internals ────────────────────────────────────────────────────────


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; return ``None`` on missing/garbage.

    Mirrors the same defensive parsing as
    :func:`app.core.rag.rag_retriever._is_faded_memory` (K7) so the two
    layers behave consistently on legacy rows with malformed dates.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _on_cooldown(
    metadata: dict[str, Any] | None,
    *,
    now: datetime,
    cooldown_hours: int,
) -> bool:
    """Return ``True`` when this memory was already called back
    within the cooldown window."""
    if cooldown_hours <= 0:
        return False
    if not metadata:
        return False
    last = _parse_iso(metadata.get("last_callback_at"))
    if last is None:
        return False
    delta_hours = (now - last).total_seconds() / 3600.0
    return delta_hours < float(cooldown_hours)


# ── Public API ───────────────────────────────────────────────────────


def detect(
    *,
    assistant_vec: np.ndarray,
    memory_store: Any,
    now: datetime | None = None,
    threshold: float,
    age_floor_days: int,
    cooldown_hours: int,
    top_k: int,
    allowed_kinds: Iterable[str] | None = None,
) -> list[CallbackHit]:
    """Stateless cosine-walk of the in-memory mirror returning ordered hits.

    Walks every memory in the store, filters to ``allowed_kinds`` (or
    the default :data:`CALLBACK_KINDS` when ``None``), drops rows
    whose age is below the floor or that are currently on cooldown,
    computes cosine similarity against ``assistant_vec``, keeps
    rows ``>= threshold``, and returns the top ``top_k`` sorted by
    similarity descending.

    ``assistant_vec`` is expected to be unit-norm (the Embedder
    already normalises). The function is defensive against bad
    inputs: empty store, missing embeddings, malformed timestamps,
    zero-dim vectors — all return an empty list rather than raise.
    """
    if assistant_vec is None:
        return []
    vec = np.asarray(assistant_vec, dtype=np.float32)
    if vec.size == 0:
        return []
    # Renormalise defensively in case caller passed a non-unit vec
    # (the Embedder already does this; cheap insurance for tests).
    vnorm = float(np.linalg.norm(vec))
    if vnorm <= 0.0:
        return []
    if abs(vnorm - 1.0) > 1e-3:
        vec = vec / vnorm

    if memory_store is None:
        return []

    kinds = (
        frozenset(s.strip().lower() for s in allowed_kinds if s)
        if allowed_kinds is not None
        else CALLBACK_KINDS
    )

    now_ts = now or datetime.now(timezone.utc)
    age_floor = max(1, int(age_floor_days))
    cooldown = max(0, int(cooldown_hours))
    top_n = max(1, int(top_k))
    sim_floor = float(threshold)

    # P17: pull only the callback-eligible kinds via a single locked
    # mirror walk (``iter_by_kinds``) instead of ``list_recent(10_000)``,
    # which copied the entire mirror and paid two O(n log n) sorts before
    # this loop discarded every non-allow-list row anyway. The kind
    # filter now happens in the store, so the cosine walk below only
    # touches eligible rows. Falls back to the legacy full-mirror path
    # for stores that don't expose ``iter_by_kinds`` (duck-typed doubles).
    try:
        if hasattr(memory_store, "iter_by_kinds"):
            candidates = memory_store.iter_by_kinds(kinds)
        else:
            candidates = memory_store.list_recent(limit=10_000)
    except Exception:
        log.debug("callback-detector: candidate fetch failed", exc_info=True)
        return []
    if not candidates:
        return []

    scored: list[CallbackHit] = []
    total_walked = 0
    for mem in candidates:
        total_walked += 1
        kind = (getattr(mem, "kind", "") or "").lower()
        if kind not in kinds:
            continue
        emb = getattr(mem, "embedding", None)
        if emb is None or getattr(emb, "size", 0) == 0:
            continue
        created = _parse_iso(getattr(mem, "created_at", None))
        if created is None:
            continue
        age_days = (now_ts - created).days
        if age_days < age_floor:
            continue
        metadata = getattr(mem, "metadata", None) or {}
        if _on_cooldown(metadata, now=now_ts, cooldown_hours=cooldown):
            continue
        try:
            mem_arr = np.asarray(emb, dtype=np.float32)
            sim = float((vec * mem_arr).sum())
        except Exception:
            continue
        if sim < sim_floor:
            continue
        prior = 0
        raw_count = metadata.get("callback_count")
        if raw_count is not None:
            try:
                prior = int(raw_count)
            except (TypeError, ValueError):
                prior = 0
        scored.append(
            CallbackHit(
                memory_id=int(getattr(mem, "id", 0)),
                kind=kind,
                similarity=round(sim, 4),
                age_days=int(age_days),
                prior_count=prior,
            )
        )

    if not scored:
        log.debug(
            "callback-detector: candidates=%d kept=0 threshold=%.2f",
            total_walked,
            sim_floor,
        )
        return []

    scored.sort(key=lambda h: h.similarity, reverse=True)
    hits = scored[:top_n]
    top = hits[0]
    log.info(
        "callback-detector: candidates=%d kept=%d top_sim=%.2f "
        "top_kind=%s top_id=%d",
        total_walked,
        len(hits),
        top.similarity,
        top.kind,
        top.memory_id,
    )
    return hits


def record(
    *,
    memory_store: Any,
    hits: list[CallbackHit],
    salience_bump: float,
    revival_bump: float,
    now: datetime | None = None,
    notify_memory_updated: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """Stamp callback metadata + salience/revival bumps for each hit.

    Per row:

    - ``metadata.callback_count`` increments by 1
    - ``metadata.last_callback_at`` set to ``now`` (ISO-8601 UTC)
    - ``metadata.last_callback_similarity`` round-tripped for debug
    - ``salience`` += ``salience_bump`` (clamped to ``[0, 1]`` by
      :meth:`MemoryStore.update`)
    - ``revival_score`` += ``revival_bump`` (also clamped by the
      store)

    Returns the number of rows actually mutated. Notifies the
    optional ``notify_memory_updated`` callback once per successful
    update so the frontend can re-render the row.
    """
    if not hits:
        return 0
    if memory_store is None:
        return 0
    now_ts = now or datetime.now(timezone.utc)
    now_iso = now_ts.isoformat()
    bump_s = max(0.0, float(salience_bump))
    bump_r = max(0.0, float(revival_bump))
    if bump_s == 0.0 and bump_r == 0.0:
        # No-op bumps would still increment the count, which is the
        # signal the retriever cares about, so we keep going. (Test
        # case ``test_record_zero_bumps_still_increments_count``
        # asserts this contract.)
        pass

    mutated = 0
    for hit in hits:
        try:
            mem = memory_store.get(int(hit.memory_id))
        except Exception:
            log.debug(
                "callback-detector: get(%s) raised",
                hit.memory_id, exc_info=True,
            )
            continue
        if mem is None:
            continue
        existing_meta = dict(getattr(mem, "metadata", None) or {})
        new_count = hit.prior_count + 1
        meta_patch = {
            "callback_count": new_count,
            "last_callback_at": now_iso,
            "last_callback_similarity": float(hit.similarity),
        }
        new_salience = float(getattr(mem, "salience", 0.0)) + bump_s
        new_revival = float(getattr(mem, "revival_score", 0.0)) + bump_r
        try:
            updated = memory_store.update(
                int(hit.memory_id),
                metadata=meta_patch,
                metadata_merge=True,
                salience=new_salience,
                revival_score=new_revival,
            )
        except Exception:
            log.debug(
                "callback-detector: update(%s) raised",
                hit.memory_id, exc_info=True,
            )
            continue
        if updated is None:
            continue
        mutated += 1
        log.info(
            "callback: id=%d kind=%s sim=%.2f count=%d->%d",
            hit.memory_id,
            hit.kind,
            hit.similarity,
            hit.prior_count,
            new_count,
        )
        # Don't fail the loop when the notify callback raises -- it's
        # frontend plumbing, not part of the persistence contract.
        if notify_memory_updated is not None:
            try:
                notify_memory_updated(updated.to_dict())
            except Exception:
                log.debug(
                    "callback-detector: notify failed",
                    exc_info=True,
                )
        # K22 also leaves a breadcrumb on the existing-meta path so a
        # caller inspecting the mirror right after sees the canonical
        # shape. The store has already persisted via update(); this
        # is just a sanity reference for tests that inspect the
        # in-memory object directly.
        existing_meta.update(meta_patch)

    return mutated
