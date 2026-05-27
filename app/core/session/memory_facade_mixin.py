"""Memory facade mixin for :class:`app.core.session_controller.SessionController`.

Houses the thin pass-through methods the UI / REST / MCP layers use to
read, create, edit, pin, and listen for changes on long-term memory
rows. The underlying storage is :class:`app.core.memory_store.MemoryStore`;
this mixin just adapts the call sites that historically lived directly
on ``SessionController``.

State ownership (``self._memory_store``, ``self._memory_extractor``,
``self._embedder``, ``self._memory_listeners``,
``self._memory_updated_listeners``, ``self._settings``) lives in
``SessionController.__init__`` -- do not move it here.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from app.core.memory_extractor import MemoryExtractor
    from app.core.memory_store import MemoryStore


log = logging.getLogger("app.session")


class MemoryFacadeMixin:
    """Memory CRUD + listener surface, peeled out of ``SessionController``."""

    @property
    def memory_store(self) -> "MemoryStore | None":
        return self._memory_store

    @property
    def memory_extractor(self) -> "MemoryExtractor | None":
        return self._memory_extractor

    def list_memories(
        self,
        *,
        limit: int = 50,
        order: str = "recent",
        offset: int = 0,
        kind: str | None = None,
        tier: str | None = None,
    ) -> list[dict[str, Any]]:
        store = self._memory_store
        if store is None:
            return []
        if order == "top":
            mems = store.list_top(limit=limit, offset=offset, kind=kind)
        else:
            mems = store.list_recent(limit=limit, offset=offset, kind=kind)
        # Tier filtering is applied here (rather than as a kwarg on
        # ``list_top`` / ``list_recent``) so the existing pinned-first
        # ordering survives. With per-tier caps capped at ~1000 the
        # post-filter walk is cheap.
        if tier:
            tier_norm = tier.strip().lower()
            mems = [m for m in mems if getattr(m, "tier", "long_term") == tier_norm]
        return [m.to_dict() for m in mems]

    def memory_count(
        self,
        kind: str | None = None,
        *,
        tier: str | None = None,
    ) -> int:
        store = self._memory_store
        if store is None:
            return 0
        return store.count_memories(kind=kind, tier=tier)

    def memory_cap(self) -> int:
        """Return the current ``memory.max_memories`` cap (UI hint)."""
        return int(getattr(self._settings.memory, "max_memories", 500))

    def delete_memory(self, memory_id: int) -> bool:
        if self._memory_store is None:
            return False
        return self._memory_store.delete(int(memory_id))

    def update_memory(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        kind: str | None = None,
        salience: float | None = None,
        tier: str | None = None,
    ) -> dict[str, Any] | None:
        """Patch fields on a memory and notify listeners.

        Re-embeds the row when ``content`` is provided so retrieval picks up
        the edit on the next turn. Returns the new ``to_dict()`` snapshot,
        or ``None`` when the row doesn't exist or the embedder is offline
        and content was changed.
        """
        store = self._memory_store
        if store is None:
            return None
        new_embedding = None
        if content is not None and self._embedder is not None:
            try:
                new_embedding = self._embedder.embed(str(content))
            except Exception:
                log.warning(
                    "memory update: re-embedding failed for id=%s",
                    memory_id,
                    exc_info=True,
                )
                # Refuse to silently keep the stale embedding -- the editor
                # surfaces a real error and the user can retry.
                return None
        updated = store.update(
            int(memory_id),
            content=content,
            kind=kind,
            salience=salience,
            embedding=new_embedding,
            tier=tier,
        )
        if updated is None:
            return None
        snapshot = updated.to_dict()
        self._notify_memory_updated(snapshot)
        return snapshot

    def add_memory(
        self,
        content: str,
        *,
        kind: str = "fact",
        salience: float = 0.6,
        tier: str = "long_term",
    ) -> dict[str, Any] | None:
        """Manually insert a memory through the editor surface.

        Mirrors the dedupe semantics of :meth:`MemoryStore.add` -- if the new
        row collapses into an existing near-duplicate, the existing memory's
        salience is bumped and we return its snapshot under the
        ``"deduped_into"`` key so the UI can toast "merged into memory #N".
        Returns ``None`` when the embedder is offline or content is empty.

        ``tier`` defaults to ``long_term`` (manual additions are
        user-confirmed). Pass ``"scratchpad"`` from the UI to test the
        probationary lane manually.
        """
        store = self._memory_store
        if store is None or self._embedder is None:
            return None
        cleaned = (content or "").strip()
        if len(cleaned) < 4:
            return None
        try:
            embedding = self._embedder.embed(cleaned)
        except Exception:
            log.warning("memory add: embed failed", exc_info=True)
            return None
        before_ids = set()
        try:
            before_ids = {m.id for m in store.list_recent(limit=store.count() or 1)}
        except Exception:
            before_ids = set()
        memory = store.add(
            cleaned,
            kind,
            embedding,
            salience=salience,
            tier=tier,
        )
        if memory is None:
            # Dedupe path: find which existing row absorbed this one. We
            # search by content equality because cosine dedupe doesn't
            # surface the matched id directly.
            existing = next(
                (m for m in store.list_recent(limit=store.count() or 1)
                 if m.content == cleaned),
                None,
            )
            if existing is None:
                return None
            return {"deduped_into": existing.to_dict()}
        snapshot = memory.to_dict()
        # Reuse the existing memory_added pipeline so other listeners (the
        # WS hub, in particular) emit the same shape they already do for
        # extractor-driven inserts.
        if memory.id not in before_ids:
            self._notify_memory_added(memory)
        return {"memory": snapshot}

    def set_memory_pinned(
        self,
        memory_id: int,
        pinned: bool,
    ) -> dict[str, Any] | None:
        store = self._memory_store
        if store is None:
            return None
        updated = store.set_pinned(int(memory_id), bool(pinned))
        if updated is None:
            return None
        snapshot = updated.to_dict()
        self._notify_memory_updated(snapshot)
        return snapshot

    def add_memory_listener(self, callback: Callable[[Any], None]) -> None:
        if callback and callback not in self._memory_listeners:
            self._memory_listeners.append(callback)

    def add_memory_updated_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        listeners = getattr(self, "_memory_updated_listeners", None)
        if listeners is None:
            listeners = []
            self._memory_updated_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def _notify_memory_added(self, memory: Any) -> None:
        for listener in list(self._memory_listeners):
            try:
                listener(memory)
            except Exception:
                log.debug("memory listener raised", exc_info=True)

    def _notify_memory_updated(self, snapshot: dict[str, Any]) -> None:
        listeners = getattr(self, "_memory_updated_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(snapshot)
            except Exception:
                log.debug("memory updated listener raised", exc_info=True)
