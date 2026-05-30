"""Long-term goals journal (K1 personality backlog).

Aiko keeps a small ring of *her own* long-term personal goals — things
she wants to grow into, explore, or become better at over time. They
are not user-facing TODOs (that's the ``agenda`` kind) and not one-shot
self-memories (the ``self`` kind). They evolve slowly across many
sessions, get periodically reflected on by the :class:`GoalWorker`
idle worker, and surface as a quiet inner-life bullet in the prompt.

Two memory kinds collaborate:

* ``goal`` — the goal itself. One row per goal, written by the
  ``[[goal:summary]]`` self-tag, the worker cold-start bootstrap,
  manual REST/UI adds, or the ``add_goal`` agent tool. Metadata::

      {
        "summary": str,
        "added_at": iso,
        "last_reflected_at": iso | None,
        "last_reflection_id": int | None,   # last goal_progress id
        "last_progress_note": str | None,   # mirror for cheap prompt
        "reflection_count": int,
        "archived_at": iso | None,
        "source": "self_tag" | "worker_bootstrap" | "user" | "tool",
      }

* ``goal_progress`` — one row per reflection moment on a goal,
  written by :class:`GoalWorker` (or the ``update_goal_progress``
  tool / REST endpoint). Metadata::

      {
        "goal_id": int,
        "note": str,
        "noted_at": iso,
        "source": "worker" | "self_tag" | "tool" | "user",
      }

Inline self-tag grammar::

    [[goal:summary text]]

The body is 4-200 chars, no square brackets or newlines. Parsed by
:func:`app.core.services.response_text_service.extract_goal_tags`
and dispatched in
:meth:`app.core.session.post_turn_mixin.PostTurnMixin._post_turn_inner_life`.

The store enforces:

* ``max_active`` (default 5) — oldest *unpinned* active goal is
  pruned on overflow.
* ``max_progress_per_goal`` (default 12) — oldest progress row is
  pruned when a new one is added, so the history stays bounded.

Pinned goal rows are immune to both caps (mirrors the
``KnowledgeGapStore`` posture); archived rows ``tier=archive`` are
kept for audit but never count toward ``max_active``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import numpy as np

from app.llm.embedder import cosine_similarity

if TYPE_CHECKING:
    from app.core.memory_store import Memory, MemoryStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.goal_store")


_MIN_SUMMARY_CHARS = 4
_MAX_SUMMARY_CHARS = 200
_DEFAULT_MAX_ACTIVE = 5
_DEFAULT_MAX_PROGRESS_PER_GOAL = 12
_DEFAULT_SIMILARITY_THRESHOLD = 0.5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GoalCandidate:
    """One ``[[goal:summary]]`` tag pulled from raw assistant text."""

    summary: str


def _clean_summary(summary: str) -> str | None:
    """Normalise the body of a goal tag / payload.

    Returns ``None`` when the body is too short or otherwise invalid;
    callers can use the ``None`` to skip the row without further checks.
    """
    text = (summary or "").strip()
    if not text or len(text) < _MIN_SUMMARY_CHARS:
        return None
    if len(text) > _MAX_SUMMARY_CHARS:
        text = text[:_MAX_SUMMARY_CHARS].rstrip()
    return text


def _is_archived(memory: "Memory") -> bool:
    """Return True when the goal row has been retired."""
    meta = getattr(memory, "metadata", None) or {}
    if not isinstance(meta, dict):
        return False
    if meta.get("archived_at"):
        return True
    return str(getattr(memory, "tier", "") or "").lower() == "archive"


class GoalStore:
    """Thin wrapper over :class:`MemoryStore` for the K1 goal kinds.

    Holds no state of its own — the underlying memory store is the
    source of truth. The wrapper centralises:

    * metadata shape for both ``goal`` and ``goal_progress`` rows,
    * cap enforcement (active goals + per-goal progress history),
    * reflection bookkeeping (``last_reflected_at`` / count / mirror),
    * goal-aware retrieval helpers (``pick_relevant`` /
      ``pick_for_reflection``).
    """

    def __init__(
        self,
        memory_store: "MemoryStore",
        embedder: "Embedder | None",
        *,
        max_active: int = _DEFAULT_MAX_ACTIVE,
        max_progress_per_goal: int = _DEFAULT_MAX_PROGRESS_PER_GOAL,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._max_active = max(1, int(max_active))
        self._max_progress_per_goal = max(1, int(max_progress_per_goal))
        self._similarity_threshold = float(similarity_threshold)

    @property
    def max_active(self) -> int:
        return self._max_active

    @property
    def max_progress_per_goal(self) -> int:
        return self._max_progress_per_goal

    # ── writes ────────────────────────────────────────────────────────

    def add_goal(
        self,
        *,
        summary: str,
        source: str = "self_tag",
        source_session: str | None = None,
        source_turn_id: int | None = None,
    ) -> "Memory | None":
        """Persist a new ``goal`` row and prune overflow afterward.

        Dedupe is delegated to :meth:`MemoryStore.add` (cosine cap on
        the underlying mirror); a near-duplicate goal will refresh
        the existing row's salience instead of inserting twice.

        Returns the inserted :class:`Memory` (or ``None`` on
        validation/embed/dedupe failure).
        """
        cleaned = _clean_summary(summary)
        if cleaned is None:
            return None
        if self._embedder is None:
            log.debug("no embedder configured; cannot persist goal")
            return None
        try:
            emb = self._embedder.embed(cleaned)
        except Exception:
            log.debug("goal embed failed", exc_info=True)
            return None
        source_norm = (source or "self_tag").strip().lower() or "self_tag"
        meta: dict[str, Any] = {
            "summary": cleaned,
            "added_at": _now_iso(),
            "last_reflected_at": None,
            "last_reflection_id": None,
            "last_progress_note": None,
            "reflection_count": 0,
            "archived_at": None,
            "source": source_norm,
        }
        try:
            mem = self._memory_store.add(
                content=cleaned,
                kind="goal",
                embedding=emb,
                salience=0.55,
                source_session=source_session,
                source_message_id=source_turn_id,
                metadata=meta,
                tier="long_term",
                confidence=0.85,
            )
        except Exception:
            log.debug("goal insert failed", exc_info=True)
            return None
        if mem is not None:
            self.prune_overflow()
        return mem

    def add_progress(
        self,
        *,
        goal_id: int,
        note: str,
        source: str = "worker",
        source_session: str | None = None,
        source_turn_id: int | None = None,
    ) -> "Memory | None":
        """Append a ``goal_progress`` row and mirror the latest note on the goal.

        Bumps ``reflection_count`` and stamps ``last_reflected_at`` /
        ``last_progress_note`` / ``last_reflection_id`` on the parent
        goal row. Per-goal history is capped by
        :meth:`prune_progress`. Returns the inserted progress
        :class:`Memory` or ``None`` on failure.
        """
        goal = self._memory_store.get(int(goal_id))
        if goal is None or goal.kind != "goal":
            return None
        cleaned = _clean_summary(note)
        if cleaned is None:
            return None
        if self._embedder is None:
            return None
        try:
            emb = self._embedder.embed(cleaned)
        except Exception:
            log.debug("goal_progress embed failed", exc_info=True)
            return None
        source_norm = (source or "worker").strip().lower() or "worker"
        meta: dict[str, Any] = {
            "goal_id": int(goal_id),
            "note": cleaned,
            "noted_at": _now_iso(),
            "source": source_norm,
        }
        try:
            progress = self._memory_store.add(
                content=cleaned,
                kind="goal_progress",
                embedding=emb,
                salience=0.4,
                source_session=source_session,
                source_message_id=source_turn_id,
                metadata=meta,
                tier="long_term",
                confidence=0.8,
                skip_dedupe=True,
            )
        except Exception:
            log.debug("goal_progress insert failed", exc_info=True)
            return None
        if progress is None:
            return None
        existing_meta = dict(goal.metadata or {})
        prior_count = 0
        try:
            prior_count = int(existing_meta.get("reflection_count", 0) or 0)
        except (TypeError, ValueError):
            prior_count = 0
        merge_meta: dict[str, Any] = {
            "last_reflected_at": meta["noted_at"],
            "last_reflection_id": int(progress.id),
            "last_progress_note": cleaned,
            "reflection_count": prior_count + 1,
        }
        try:
            self._memory_store.update(
                int(goal.id),
                metadata=merge_meta,
                metadata_merge=True,
            )
        except Exception:
            log.debug("goal mirror update failed", exc_info=True)
        self.prune_progress(int(goal_id))
        return progress

    def archive_goal(self, goal_id: int) -> bool:
        """Mark ``goal_id`` as archived (``tier=archive`` + ``archived_at``).

        Returns True when the row existed and the update succeeded.
        Archived rows survive (audit trail) but no longer count
        toward ``max_active`` and are dropped from the active prompt
        block.
        """
        goal = self._memory_store.get(int(goal_id))
        if goal is None or goal.kind != "goal":
            return False
        if _is_archived(goal):
            return True
        try:
            self._memory_store.update(
                int(goal_id),
                metadata={"archived_at": _now_iso()},
                metadata_merge=True,
                tier="archive",
            )
        except Exception:
            log.debug("archive_goal failed", exc_info=True)
            return False
        return True

    def unarchive_goal(self, goal_id: int) -> bool:
        """Inverse of :meth:`archive_goal` — pulls a row back to ``long_term``.

        Used by the REST/UI restore action. Clears
        ``metadata.archived_at`` and bumps the row back to the
        ``long_term`` tier (pinned rows are coerced there anyway).
        """
        goal = self._memory_store.get(int(goal_id))
        if goal is None or goal.kind != "goal":
            return False
        if not _is_archived(goal):
            return True
        try:
            self._memory_store.update(
                int(goal_id),
                metadata={"archived_at": None},
                metadata_merge=True,
                tier="long_term",
            )
        except Exception:
            log.debug("unarchive_goal failed", exc_info=True)
            return False
        return True

    def update_summary(
        self,
        goal_id: int,
        *,
        summary: str,
    ) -> bool:
        """Rewrite the goal's text + refresh the embedding to match.

        Used by the manual REST/UI edit path and the ``update_goal``
        agent tool. Returns True on success, False when the goal
        doesn't exist or the new text fails validation.
        """
        goal = self._memory_store.get(int(goal_id))
        if goal is None or goal.kind != "goal":
            return False
        cleaned = _clean_summary(summary)
        if cleaned is None:
            return False
        embedding = None
        if self._embedder is not None:
            try:
                embedding = self._embedder.embed(cleaned)
            except Exception:
                log.debug("goal summary embed failed", exc_info=True)
                embedding = None
        try:
            self._memory_store.update(
                int(goal_id),
                content=cleaned,
                embedding=embedding,
                metadata={"summary": cleaned},
                metadata_merge=True,
            )
        except Exception:
            log.debug("update_summary failed", exc_info=True)
            return False
        return True

    # ── reads ─────────────────────────────────────────────────────────

    def list_active(self) -> list["Memory"]:
        """Return active (non-archived) goals, newest first."""
        rows = self._all_goal_rows()
        active = [m for m in rows if not _is_archived(m)]
        active.sort(key=lambda m: m.created_at, reverse=True)
        return active

    def list_all(self, *, include_archived: bool = True) -> list["Memory"]:
        rows = self._all_goal_rows()
        if not include_archived:
            rows = [m for m in rows if not _is_archived(m)]
        rows.sort(key=lambda m: m.created_at, reverse=True)
        return rows

    def list_progress(self, goal_id: int) -> list["Memory"]:
        """Progress rows for ``goal_id``, newest first."""
        gid = int(goal_id)
        rows = self._all_progress_rows()
        matching = []
        for mem in rows:
            meta = getattr(mem, "metadata", None) or {}
            if not isinstance(meta, dict):
                continue
            try:
                if int(meta.get("goal_id", -1)) == gid:
                    matching.append(mem)
            except (TypeError, ValueError):
                continue
        matching.sort(key=lambda m: m.created_at, reverse=True)
        return matching

    def pick_for_reflection(self) -> "Memory | None":
        """Pick the next active goal to reflect on (oldest-touched first).

        Selection key is the most recent of ``last_reflected_at`` /
        ``added_at``; the goal whose key sits furthest in the past
        wins so the worker drifts evenly across the ring.

        Returns ``None`` when no active goals exist.
        """
        active = self.list_active()
        if not active:
            return None

        def _last_touched(mem: "Memory") -> str:
            meta = mem.metadata or {}
            if isinstance(meta, dict):
                touched = meta.get("last_reflected_at") or meta.get("added_at")
                if isinstance(touched, str) and touched:
                    return touched
            return mem.created_at or ""

        active.sort(key=_last_touched)
        return active[0]

    def pick_relevant(
        self,
        query_text: str,
        *,
        threshold: float | None = None,
    ) -> "Memory | None":
        """Top-1 active goal by cosine similarity to ``query_text``.

        Mirrors :meth:`KnowledgeGapStore.pick_relevant`. Returns
        ``None`` when no active goals exist, no embedder is
        configured, or the best match falls below ``threshold``
        (default ``similarity_threshold``).
        """
        if self._embedder is None:
            return None
        query = (query_text or "").strip()
        if not query:
            return None
        thr = self._similarity_threshold if threshold is None else float(threshold)
        try:
            q_emb = self._embedder.embed(query)
        except Exception:
            log.debug("goal pick_relevant embed failed", exc_info=True)
            return None
        q_arr = np.asarray(q_emb, dtype=np.float32)
        norm = float(np.linalg.norm(q_arr))
        if norm > 0.0:
            q_arr = q_arr / norm
        best_score = -1.0
        best_row: "Memory | None" = None
        for mem in self.list_active():
            try:
                score = float(cosine_similarity(q_arr, mem.embedding))
            except Exception:
                continue
            if score > best_score:
                best_score = score
                best_row = mem
        if best_row is None or best_score < thr:
            return None
        return best_row

    def active_goal_vectors(self) -> list[np.ndarray]:
        """Normalised embeddings for every active goal.

        Used by :class:`RagRetriever` to apply a small per-hit score
        bonus on goal-aligned memories: the retriever pulls this list
        once per call and, for each hit, computes ``max(cosine(hit,
        goal))`` against the cached vectors. ``MemoryStore.add``
        normalises before persisting so the returned vectors are unit
        length by construction. Rows missing an embedding (rare;
        primarily test stubs) are skipped.
        """
        out: list[np.ndarray] = []
        for mem in self.list_active():
            emb = getattr(mem, "embedding", None)
            if emb is None:
                continue
            try:
                if emb.size > 0:
                    out.append(emb)
            except AttributeError:
                continue
        return out

    # ── maintenance ───────────────────────────────────────────────────

    def prune_overflow(self) -> int:
        """Archive the oldest unpinned active goal above ``max_active``.

        We *archive* rather than delete so the history stays for
        audit. Pinned goals are immune (the user explicitly
        anchored them).
        """
        active = [m for m in self.list_active() if not m.pinned]
        if len(active) <= self._max_active:
            return 0
        active.sort(key=lambda m: m.created_at)
        overflow = len(active) - self._max_active
        pruned = 0
        for victim in active[:overflow]:
            if self.archive_goal(int(victim.id)):
                pruned += 1
        return pruned

    def prune_progress(self, goal_id: int) -> int:
        """Drop the oldest progress rows above ``max_progress_per_goal``."""
        rows = self.list_progress(int(goal_id))
        if len(rows) <= self._max_progress_per_goal:
            return 0
        rows.sort(key=lambda m: m.created_at)
        overflow = len(rows) - self._max_progress_per_goal
        pruned = 0
        for victim in rows[:overflow]:
            try:
                if self._memory_store.delete(int(victim.id)):
                    pruned += 1
            except Exception:
                log.debug("goal_progress prune failed", exc_info=True)
        return pruned

    def has_any_active(self) -> bool:
        """Cheap "is the ring empty?" check used by the worker bootstrap."""
        return bool(self.list_active())

    # ── internals ─────────────────────────────────────────────────────

    def _all_goal_rows(self) -> list["Memory"]:
        try:
            mirror = getattr(self._memory_store, "_mirror", None)
            if mirror is not None:
                return [m for m in mirror.values() if m.kind == "goal"]
        except Exception:
            pass
        try:
            recent = self._memory_store.list_recent(limit=10_000)
            return [m for m in recent if m.kind == "goal"]
        except Exception:
            return []

    def _all_progress_rows(self) -> list["Memory"]:
        try:
            mirror = getattr(self._memory_store, "_mirror", None)
            if mirror is not None:
                return [m for m in mirror.values() if m.kind == "goal_progress"]
        except Exception:
            pass
        try:
            recent = self._memory_store.list_recent(limit=10_000)
            return [m for m in recent if m.kind == "goal_progress"]
        except Exception:
            return []
