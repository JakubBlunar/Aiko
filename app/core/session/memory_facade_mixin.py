"""Memory facade mixin for :class:`app.core.session.session_controller.SessionController`.

Houses the thin pass-through methods the UI / REST / MCP layers use to
read, create, edit, pin, and listen for changes on long-term memory
rows. The underlying storage is :class:`app.core.memory.memory_store.MemoryStore`;
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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any


def _now_iso_for_conflict() -> str:
    return datetime.now(timezone.utc).isoformat()

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from app.core.memory.memory_extractor import MemoryExtractor
    from app.core.memory.memory_store import MemoryStore


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
        confidence: float | None = None,
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
            confidence=confidence,
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
        confidence: float | None = None,
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

        ``confidence`` defaults to ``1.0`` for manual creates (the user
        explicitly anchored the row in the editor — strongest signal
        we have). Pass an explicit value to override.
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
            confidence=1.0 if confidence is None else float(confidence),
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

    # K2 personality backlog: belief CRUD listener hooks ─────────────
    # The web layer subscribes to these so the Beliefs sub-tab can
    # broadcast belief_added / belief_updated / belief_deleted over
    # WebSocket without polling.

    def add_belief_added_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        listeners = getattr(self, "_belief_added_listeners", None)
        if listeners is None:
            listeners = []
            self._belief_added_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def add_belief_updated_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        listeners = getattr(self, "_belief_updated_listeners", None)
        if listeners is None:
            listeners = []
            self._belief_updated_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def add_belief_deleted_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        listeners = getattr(self, "_belief_deleted_listeners", None)
        if listeners is None:
            listeners = []
            self._belief_deleted_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def _notify_belief_added(self, payload: dict[str, Any]) -> None:
        listeners = getattr(self, "_belief_added_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("belief added listener raised", exc_info=True)

    def _notify_belief_updated(self, payload: dict[str, Any]) -> None:
        listeners = getattr(self, "_belief_updated_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("belief updated listener raised", exc_info=True)

    def _notify_belief_deleted(self, payload: dict[str, Any]) -> None:
        listeners = getattr(self, "_belief_deleted_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("belief deleted listener raised", exc_info=True)

    def _notify_memory_added(self, memory: Any) -> None:
        for listener in list(self._memory_listeners):
            try:
                listener(memory)
            except Exception:
                log.debug("memory listener raised", exc_info=True)
        # F1: opportunistic fact-check enqueue. Cheap (regex over a
        # short content string) and absorbing failure here keeps the
        # ordinary memory-write path safe.
        queue = getattr(self, "_fact_check_queue", None)
        if queue is None:
            return
        try:
            self._maybe_enqueue_claims(memory)
        except Exception:
            log.debug("fact-check enqueue failed", exc_info=True)

    def _maybe_enqueue_claims(self, memory: Any) -> None:
        """Pull claims out of ``memory.content`` (or its gap question)
        and append to the fact-check queue.

        The privacy gate (:mod:`app.core.memory.fact_check_privacy`) runs
        before anything is queued so personal memories never leak to
        the outbound search path. Knowledge-gap questions still go
        through the gate too — most gap questions are public-facing,
        but a question like "what's Jacob's birthday" should not be
        sent to DuckDuckGo.
        """
        queue = getattr(self, "_fact_check_queue", None)
        if queue is None or memory is None:
            return
        memory_id = getattr(memory, "id", None)
        if memory_id is None:
            return

        from app.core.memory.fact_check_privacy import (
            classify_memory_for_fact_check,
            scrub_claim_for_search,
        )

        user_names = self._fact_check_user_names()
        assistant_name = self._fact_check_assistant_name()
        kind = (getattr(memory, "kind", "") or "").lower()
        if kind == "knowledge_gap":
            meta = getattr(memory, "metadata", None) or {}
            question = (
                str(meta.get("question") or "").strip()
                if isinstance(meta, dict)
                else ""
            )
            if not question:
                question = (getattr(memory, "content", "") or "").strip()
            if not question:
                return
            # Knowledge gaps run through the *claim* scrubber (not the
            # memory classifier) because the kind itself isn't
            # personal — the question is. If the scrubbed version
            # would lose meaning the gap simply doesn't get a queue
            # entry; the user can still resolve it manually.
            safe = scrub_claim_for_search(
                question,
                user_names=user_names,
                assistant_name=assistant_name,
            )
            if safe is None:
                # The privacy module already logged the reason at INFO;
                # we add the enqueue-side context so the audit trail
                # threads from "memory written" -> "scrub blocked".
                log.info(
                    "fact-check enqueue skip: knowledge_gap memory_id=%s "
                    "scrub returned None",
                    memory_id,
                )
                return
            queue.enqueue(
                memory_id=int(memory_id),
                claim_text=question,
                claim_kind="knowledge_gap",
            )
            log.info(
                "fact-check enqueued: kind=knowledge_gap memory_id=%s",
                memory_id,
            )
            return

        content = (getattr(memory, "content", "") or "").strip()
        if not content:
            return

        decision = classify_memory_for_fact_check(
            kind=kind,
            content=content,
            user_names=user_names,
            assistant_name=assistant_name,
        )
        if decision.personal:
            # The classify call already logged the BLOCK at INFO with
            # reason + preview; this line gives the enqueue-side
            # context (memory_id) so an audit can correlate the two
            # in ``data/app.log``.
            log.info(
                "fact-check enqueue skip: personal memory_id=%s reason=%s",
                memory_id,
                decision.reason,
            )
            return

        from app.core.memory.claim_extractor import find_claims

        enqueued = 0
        skipped = 0
        for claim in find_claims(content):
            # Belt-and-braces: even after the memory cleared the
            # classifier, individual claim spans (especially
            # proper_noun) can still be personal. Scrub once more and
            # drop the ones that come back ``None``.
            safe = scrub_claim_for_search(
                claim.text,
                user_names=user_names,
                assistant_name=assistant_name,
            )
            if safe is None:
                skipped += 1
                continue
            queue.enqueue(
                memory_id=int(memory_id),
                claim_text=claim.text,
                claim_kind=claim.kind,
            )
            enqueued += 1
        if enqueued or skipped:
            log.info(
                "fact-check enqueue done: memory_id=%s kind=%s enqueued=%d "
                "skipped_by_scrub=%d",
                memory_id,
                kind,
                enqueued,
                skipped,
            )

    def _fact_check_user_names(self) -> list[str]:
        """User name + any user-aware aliases the worker should scrub."""
        out: list[str] = []
        try:
            name = self.user_display_name  # property on SessionController
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
        except Exception:
            pass
        # Fall back to ``settings.assistant.user_display_name`` so the
        # mixin still works in tests that don't expose the property.
        try:
            settings_obj = getattr(self, "_settings", None)
            assistant_cfg = getattr(settings_obj, "assistant", None)
            cfg_name = getattr(assistant_cfg, "user_display_name", "") or ""
            if isinstance(cfg_name, str) and cfg_name.strip():
                out.append(cfg_name.strip())
        except Exception:
            pass
        # Dedupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for n in out:
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(n)
        return deduped

    def _fact_check_assistant_name(self) -> str | None:
        try:
            settings_obj = getattr(self, "_settings", None)
            assistant_cfg = getattr(settings_obj, "assistant", None)
            name = getattr(assistant_cfg, "name", "") or ""
            return name.strip() or None
        except Exception:
            return None

    def _notify_memory_updated(self, snapshot: dict[str, Any]) -> None:
        listeners = getattr(self, "_memory_updated_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(snapshot)
            except Exception:
                log.debug("memory updated listener raised", exc_info=True)

    # ── Knowledge gaps (F2 personality backlog) ──────────────────────

    def list_knowledge_gaps(
        self,
        *,
        include_resolved: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ``knowledge_gap`` rows for the Memory tab panel."""
        store = getattr(self, "_knowledge_gap_store", None)
        if store is None:
            return []
        try:
            rows = store.list_all(include_resolved=include_resolved)
        except Exception:
            log.debug("knowledge gap list failed", exc_info=True)
            return []
        return [m.to_dict() for m in rows]

    def delete_knowledge_gap(self, gap_id: int) -> bool:
        store = getattr(self, "_knowledge_gap_store", None)
        if store is None:
            return False
        ok = store.delete(int(gap_id))
        if ok:
            self._notify_knowledge_gap({"deleted_gap_id": int(gap_id)})
        return ok

    def resolve_knowledge_gap(
        self,
        gap_id: int,
        *,
        answer: str | None = None,
    ) -> dict[str, Any] | None:
        """Mark a gap resolved.

        If ``answer`` is provided, also writes a sibling ``fact`` memory
        with the answer text (so the answer ends up in retrieval) and
        backlinks the gap via ``resolved_by_memory_id``. Returns the
        updated gap snapshot, or ``None`` on failure.
        """
        store = getattr(self, "_knowledge_gap_store", None)
        memory_store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        if store is None or memory_store is None:
            return None
        answer_memory_id: int | None = None
        if answer is not None and embedder is not None:
            cleaned = answer.strip()
            if cleaned:
                try:
                    emb = embedder.embed(cleaned)
                    mem = memory_store.add(
                        content=cleaned,
                        kind="fact",
                        embedding=emb,
                        salience=0.7,
                        confidence=0.85,
                        tier="long_term",
                    )
                    if mem is not None:
                        answer_memory_id = int(mem.id)
                        self._notify_memory_added(mem)
                except Exception:
                    log.warning("gap resolve answer write failed", exc_info=True)
        ok = store.mark_resolved(int(gap_id), answer_memory_id=answer_memory_id)
        if not ok:
            return None
        # Re-fetch the resolved gap snapshot for the response payload.
        try:
            gap = memory_store.get(int(gap_id))
        except Exception:
            gap = None
        snapshot = gap.to_dict() if gap is not None else None
        if snapshot is not None:
            self._notify_knowledge_gap({"gap": snapshot})
        return snapshot

    # ── Fact-checker status (F1 personality backlog) ─────────────────

    def fact_checker_status(self) -> dict[str, Any]:
        """Return a snapshot for the Memory tab footer.

        Shape::

            {
              "enabled": bool,
              "pending": int,            # claims awaiting verification
              "queue_total": int,        # alias for ``pending``
              "last_verified_at": str|None,
              "hour_used": int,
              "hour_cap": int,
              "day_used": int,
              "day_cap": int
            }

        Pulls counters from the persisted :class:`FactCheckRateLimiter`
        and the claim queue. When the worker is disabled (or the
        web-search tool isn't available) the counts still render so
        the user can see what *would* be processed.
        """
        agent_settings = getattr(self, "_settings", None)
        agent = getattr(agent_settings, "agent", None) if agent_settings else None
        enabled = bool(getattr(agent, "fact_checker_enabled", False)) if agent else False
        queue = getattr(self, "_fact_check_queue", None)
        pending = 0
        if queue is not None:
            try:
                pending = len(queue.peek_all())
            except Exception:
                pending = 0
        limiter = getattr(self, "_fact_check_rate_limiter", None)
        if limiter is not None:
            try:
                buckets = limiter.snapshot()
            except Exception:
                buckets = {
                    "hour_used": 0,
                    "hour_cap": int(getattr(agent, "fact_checker_per_hour_cap", 10)),
                    "day_used": 0,
                    "day_cap": int(getattr(agent, "fact_checker_per_day_cap", 50)),
                }
        else:
            buckets = {
                "hour_used": 0,
                "hour_cap": int(getattr(agent, "fact_checker_per_hour_cap", 10)),
                "day_used": 0,
                "day_cap": int(getattr(agent, "fact_checker_per_day_cap", 50)),
            }
        last_verified_at: str | None = None
        memory_store = getattr(self, "_memory_store", None)
        if memory_store is not None:
            try:
                last_verified_at = self._last_verified_at_from_store(memory_store)
            except Exception:
                last_verified_at = None
        return {
            "enabled": enabled,
            "pending": int(pending),
            "queue_total": int(pending),
            "last_verified_at": last_verified_at,
            **buckets,
        }

    @staticmethod
    def _last_verified_at_from_store(memory_store: Any) -> str | None:
        """Return the most recent ``metadata.last_verified_at`` ISO string.

        Walks the mirror once (cheap; in-memory) and picks the max
        timestamp. Returns ``None`` when no memory has been verified
        yet (typical right after F1 ships).
        """
        latest: str | None = None
        try:
            mirror = getattr(memory_store, "_mirror", None) or {}
            items = list(mirror.values()) if hasattr(mirror, "values") else list(mirror)
        except Exception:
            items = []
        for mem in items:
            metadata = getattr(mem, "metadata", None) or {}
            stamp = metadata.get("last_verified_at") if isinstance(metadata, dict) else None
            if isinstance(stamp, str) and (latest is None or stamp > latest):
                latest = stamp
        return latest

    # ── Memory conflicts (F5 personality backlog) ────────────────────

    def list_memory_conflicts(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        include_recently_resolved: bool = True,
    ) -> dict[str, Any]:
        """Return open conflicts plus a recently-auto-resolved tail.

        Shape::

            {
              "open": [pair_dict, ...],
              "recently_auto_resolved": [pair_dict, ...],  # if asked
              "counts": {"open": int, "auto_resolved": int, ...}
            }

        ``pair_dict`` includes the two memory snapshots inline so the
        UI can render the side-by-side card without a second round
        trip. Returns empty lists when the store hasn't booted (e.g.
        the worker / config is disabled).
        """
        store = getattr(self, "_memory_conflict_store", None)
        memory_store = getattr(self, "_memory_store", None)
        if store is None or memory_store is None:
            return {
                "open": [],
                "recently_auto_resolved": [],
                "counts": {
                    "open": 0,
                    "auto_resolved": 0,
                    "user_resolved": 0,
                    "dismissed": 0,
                },
            }

        def _to_payload(pair: Any) -> dict[str, Any] | None:
            try:
                mem_a = memory_store.get(int(pair.memory_a_id))
                mem_b = memory_store.get(int(pair.memory_b_id))
            except Exception:
                mem_a = None
                mem_b = None
            return {
                "id": int(pair.id),
                "memory_a_id": int(pair.memory_a_id),
                "memory_b_id": int(pair.memory_b_id),
                "memory_a": mem_a.to_dict() if mem_a is not None else None,
                "memory_b": mem_b.to_dict() if mem_b is not None else None,
                "similarity": float(pair.similarity),
                "confidence_delta": float(pair.confidence_delta),
                "heuristic_label": str(pair.heuristic_label),
                "heuristic_signals": list(pair.heuristic_signals),
                "llm_verdict": pair.llm_verdict,
                "llm_reason": pair.llm_reason,
                "status": str(pair.status),
                "winner_id": pair.winner_id,
                "loser_id": pair.loser_id,
                "resolution_action": pair.resolution_action,
                "flagged_by": str(pair.flagged_by),
                "detected_at": str(pair.detected_at),
                "resolved_at": pair.resolved_at,
            }

        try:
            if status is None:
                open_pairs = store.list_open(limit=limit, offset=offset)
            else:
                open_pairs = store.list_recent(
                    limit=limit, offset=offset, status=status,
                )
        except Exception:
            log.debug("memory conflicts list failed", exc_info=True)
            open_pairs = []

        recently_resolved: list[dict[str, Any]] = []
        if include_recently_resolved and status is None:
            try:
                rr = store.list_recently_auto_resolved(limit=10)
                recently_resolved = [
                    p for p in (_to_payload(x) for x in rr) if p is not None
                ]
            except Exception:
                log.debug(
                    "memory conflicts recent list failed", exc_info=True,
                )

        try:
            counts = store.count_by_status()
        except Exception:
            counts = {
                "open": 0,
                "auto_resolved": 0,
                "user_resolved": 0,
                "dismissed": 0,
            }

        open_payload = [
            p for p in (_to_payload(x) for x in open_pairs) if p is not None
        ]
        return {
            "open": open_payload,
            "recently_auto_resolved": recently_resolved,
            "counts": counts,
        }

    def topic_graph_snapshot(self) -> dict[str, Any]:
        """K9: serialise the topic-cluster graph for the browser surface.

        Backs ``GET /api/topic-graph`` + the ``get_topic_graph`` MCP
        tool. Delegates to the pure
        :func:`app.core.conversation.topic_graph.build_topic_graph_snapshot`
        helper, which returns an empty-but-valid shape (``enabled=False``)
        when the graph is disabled / failed to init or the memory store
        is absent. Best-effort: any failure collapses to the same
        disabled shape rather than raising into the request handler.
        """
        topic_graph = getattr(self, "_topic_graph", None)
        memory_store = getattr(self, "_memory_store", None)
        try:
            from app.core.conversation.topic_graph import (
                build_topic_graph_snapshot,
            )

            return build_topic_graph_snapshot(topic_graph, memory_store)
        except Exception:
            log.debug("topic graph snapshot failed", exc_info=True)
            return {
                "enabled": False,
                "total_memories": 0,
                "total_clusters": 0,
                "clustered_memories": 0,
                "similarity": 0.0,
                "min_cluster_size": 0,
                "filter_threshold": 0.0,
                "clusters": [],
            }

    def resolve_memory_conflict(
        self,
        pair_id: int,
        *,
        winner_id: int,
        action: str = "demote",
    ) -> dict[str, Any] | None:
        """Apply a user-chosen resolution to a conflict pair.

        ``action`` is ``'demote'`` (clamp loser confidence to 0.20,
        archive tier) or ``'delete'`` (remove the loser memory). The
        cascade-cleanup hook on ``MemoryStore.delete`` keeps the
        ``memory_conflicts`` row coherent in the delete case --
        ``mark_user_resolved`` is still called first so the resolved
        row carries the correct winner/action stamp before the
        cascade-cleanup might wipe it.
        """
        store = getattr(self, "_memory_conflict_store", None)
        memory_store = getattr(self, "_memory_store", None)
        if store is None or memory_store is None:
            return None
        action_norm = str(action or "").strip().lower()
        if action_norm not in {"demote", "delete"}:
            raise ValueError(
                f"invalid action {action!r} (use 'demote' or 'delete')"
            )
        pair = store.get(int(pair_id))
        if pair is None:
            return None
        winner_int = int(winner_id)
        if winner_int not in (pair.memory_a_id, pair.memory_b_id):
            raise ValueError(
                "winner_id must equal memory_a_id or memory_b_id of the pair",
            )
        loser_int = (
            pair.memory_b_id if winner_int == pair.memory_a_id
            else pair.memory_a_id
        )
        store.mark_user_resolved(
            int(pair_id),
            winner_id=winner_int,
            loser_id=loser_int,
            action=action_norm,
        )
        try:
            if action_norm == "demote":
                memory_store.update(
                    loser_int,
                    confidence=0.20,
                    tier="archive",
                    metadata={
                        "superseded_by": int(winner_int),
                        "superseded_at": _now_iso_for_conflict(),
                        "superseded_reason": "user_resolved_conflict",
                    },
                    metadata_merge=True,
                )
                self._notify_memory_updated({"memory_id": int(loser_int)})
            else:  # delete
                memory_store.delete(loser_int)
                # cascade-cleanup deletes the conflict row, so we can't
                # re-fetch it. Return the snapshot we already have.
                self._notify_memory_updated({"deleted_memory_id": int(loser_int)})
                return {
                    "pair_id": int(pair_id),
                    "winner_id": winner_int,
                    "loser_id": loser_int,
                    "action": action_norm,
                    "deleted": True,
                }
        except Exception:
            log.debug(
                "resolve_memory_conflict apply failed pair_id=%s",
                pair_id,
                exc_info=True,
            )
            return None
        snap = store.get(int(pair_id))
        return {
            "pair_id": int(pair_id),
            "winner_id": winner_int,
            "loser_id": loser_int,
            "action": action_norm,
            "status": snap.status if snap is not None else "user_resolved",
        }

    def dismiss_memory_conflict(self, pair_id: int) -> bool:
        store = getattr(self, "_memory_conflict_store", None)
        if store is None:
            return False
        return bool(store.dismiss(int(pair_id)))

    # ── K2 theory-of-mind belief facade ──────────────────────────────

    def list_beliefs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        kind: str | None = None,
        status: str | None = None,
        include_counts: bool = True,
    ) -> dict[str, Any]:
        """Return a paginated belief list + per-status counts.

        Mirrors the shape of ``list_memory_conflicts`` so the
        Beliefs sub-tab can render counts + paginated rows from a
        single REST call.
        """
        store = getattr(self, "_belief_store", None)
        if store is None:
            return {
                "beliefs": [],
                "counts": {
                    "active": 0,
                    "confirmed": 0,
                    "contradicted": 0,
                    "stale": 0,
                },
                "enabled": False,
            }
        rows = store.list_recent(
            user_id=self._user_id,
            kind=kind,
            status=status,
            limit=int(limit),
            offset=int(offset),
        )
        payload: dict[str, Any] = {
            "beliefs": [r.to_payload() for r in rows],
            "enabled": True,
        }
        if include_counts:
            payload["counts"] = store.count_by_status(user_id=self._user_id)
        return payload

    def add_belief(
        self,
        *,
        kind: str,
        topic: str,
        predicted_state: str,
        confidence: float | None = None,
    ) -> dict[str, Any] | None:
        """Manual belief create (REST POST /api/beliefs)."""
        store = getattr(self, "_belief_store", None)
        if store is None:
            return None
        embedder = getattr(self, "_embedder", None)
        embedding = None
        if embedder is not None:
            try:
                embedding = embedder.embed(topic)
            except Exception:
                log.debug("belief manual embed failed", exc_info=True)
        belief = store.upsert(
            user_id=self._user_id,
            kind=kind,
            topic=topic,
            predicted_state=predicted_state,
            confidence=confidence,
            source="manual",
            topic_embedding=embedding,
        )
        if belief is None:
            return None
        payload = belief.to_payload()
        try:
            self._notify_belief_added(payload)
        except Exception:
            log.debug("manual belief notify failed", exc_info=True)
        return payload

    def update_belief(
        self,
        belief_id: int,
        *,
        predicted_state: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """Apply a partial belief edit (REST PATCH /api/beliefs/{id})."""
        store = getattr(self, "_belief_store", None)
        if store is None:
            return None
        belief = store.update(
            int(belief_id),
            predicted_state=predicted_state,
            confidence=confidence,
            status=status,
        )
        if belief is None:
            return None
        payload = belief.to_payload()
        try:
            self._notify_belief_updated(payload)
        except Exception:
            log.debug("belief update notify failed", exc_info=True)
        return payload

    def delete_belief(self, belief_id: int) -> bool:
        store = getattr(self, "_belief_store", None)
        if store is None:
            return False
        ok = bool(store.delete(int(belief_id)))
        if ok:
            try:
                self._notify_belief_deleted({"id": int(belief_id)})
            except Exception:
                log.debug("belief delete notify failed", exc_info=True)
        return ok
