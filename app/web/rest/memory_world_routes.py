from __future__ import annotations

import logging
from typing import Any
from fastapi import HTTPException
from fastapi.responses import JSONResponse


log = logging.getLogger("app.web.server")


def register(app, session, hub, _broadcast_context_window, live_session) -> None:
    """REST routes: memory world routes."""
    def _on_memory_updated(snapshot: dict[str, Any]) -> None:
        hub.broadcast({"type": "memory_updated", "memory": snapshot})

    try:
        session.add_memory_updated_listener(_on_memory_updated)
    except Exception:
        log.debug("memory updated listener subscription failed", exc_info=True)

    @app.get("/api/memories")
    def list_memories(
        limit: int = 50,
        order: str = "recent",
        offset: int = 0,
        kind: str | None = None,
        tier: str | None = None,
    ) -> JSONResponse:
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        order_norm = "top" if str(order).strip().lower() == "top" else "recent"
        kind_norm = (kind or "").strip().lower() or None
        tier_norm = (tier or "").strip().lower() or None
        items = session.list_memories(
            limit=clamped_limit,
            order=order_norm,
            offset=clamped_offset,
            kind=kind_norm,
            tier=tier_norm,
        )
        return JSONResponse({
            "memories": items,
            "count": len(items),
            "total": session.memory_count(kind=kind_norm, tier=tier_norm),
            "cap": session.memory_cap(),
            "enabled": session.memory_store is not None,
        })

    @app.get("/api/diary")
    def list_diary(
        limit: int = 50,
        offset: int = 0,
        kind: str | None = None,
    ) -> JSONResponse:
        """H9 — Aiko's diary: a read-only window into her inner life.

        Paginated, newest-first view over the journal-flavoured memory
        kinds (reflections / dreams / mindmap noticings / shared moments
        / open questions). Reuses the ``/api/memories`` pagination shape.
        The ``kind`` filter is clamped to the journal allow-list inside
        the facade, so this surface can never leak factual rows.
        """
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        kind_norm = (kind or "").strip().lower() or None
        items = session.list_diary(
            limit=clamped_limit,
            offset=clamped_offset,
            kind=kind_norm,
        )
        return JSONResponse({
            "entries": items,
            "count": len(items),
            "total": session.diary_count(kind=kind_norm),
            "enabled": session.memory_store is not None,
        })

    @app.get("/api/memories/counts")
    def memory_counts() -> JSONResponse:
        """Per-tier memory totals (schema v8). Drives the Memory tab header."""
        store = session.memory_store
        if store is None:
            return JSONResponse(
                {"scratchpad": 0, "long_term": 0, "archive": 0, "total": 0},
            )
        try:
            counts = store.count_by_tier()
        except Exception:
            counts = {"scratchpad": 0, "long_term": 0, "archive": 0, "total": 0}
        return JSONResponse(counts)

    @app.delete("/api/memories/{memory_id}")
    def delete_memory(memory_id: int) -> JSONResponse:
        ok = session.delete_memory(int(memory_id))
        if not ok:
            raise HTTPException(404, "memory not found")
        hub.broadcast({"type": "memory_deleted", "id": int(memory_id)})
        return JSONResponse({"deleted": int(memory_id)})

    @app.patch("/api/memories/{memory_id}")
    async def patch_memory(memory_id: int, payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        content = payload.get("content")
        kind = payload.get("kind")
        salience = payload.get("salience")
        tier = payload.get("tier")
        confidence = payload.get("confidence")
        if (
            content is None
            and kind is None
            and salience is None
            and tier is None
            and confidence is None
        ):
            raise HTTPException(
                400,
                "patch must include at least one of content, kind, salience, "
                "tier, confidence",
            )
        # Type-checks before reaching into the store: clearer than letting the
        # mutator silently coerce arbitrary input.
        if content is not None and not isinstance(content, str):
            raise HTTPException(400, "content must be a string")
        if kind is not None and not isinstance(kind, str):
            raise HTTPException(400, "kind must be a string")
        if salience is not None and not isinstance(salience, (int, float)):
            raise HTTPException(400, "salience must be a number")
        if tier is not None and not isinstance(tier, str):
            raise HTTPException(400, "tier must be a string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        try:
            updated = session.update_memory(
                int(memory_id),
                content=content,
                kind=kind,
                salience=float(salience) if salience is not None else None,
                tier=tier,
                confidence=float(confidence) if confidence is not None else None,
            )
        except Exception as exc:
            raise HTTPException(500, f"update failed: {exc}") from exc
        if updated is None:
            raise HTTPException(404, "memory not found")
        return JSONResponse({"memory": updated})

    @app.post("/api/memories")
    async def create_memory(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        content = payload.get("content")
        kind = payload.get("kind", "fact")
        salience = payload.get("salience", 0.6)
        tier = payload.get("tier", "long_term")
        confidence = payload.get("confidence")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(400, "content must be a non-empty string")
        if not isinstance(kind, str):
            raise HTTPException(400, "kind must be a string")
        if not isinstance(salience, (int, float)):
            raise HTTPException(400, "salience must be a number")
        if not isinstance(tier, str):
            raise HTTPException(400, "tier must be a string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        result = session.add_memory(
            content,
            kind=kind,
            salience=float(salience),
            tier=tier,
            confidence=float(confidence) if confidence is not None else None,
        )
        if result is None:
            raise HTTPException(503, "memory store unavailable or content too short")
        return JSONResponse(result)

    # ── REST: knowledge gaps (F2) ────────────────────────────────────

    @app.get("/api/knowledge-gaps")
    def list_knowledge_gaps(include_resolved: bool = False) -> JSONResponse:
        rows = session.list_knowledge_gaps(include_resolved=include_resolved)
        return JSONResponse({"gaps": rows, "total": len(rows)})

    @app.delete("/api/knowledge-gaps/{gap_id}")
    def delete_knowledge_gap(gap_id: int) -> JSONResponse:
        ok = session.delete_knowledge_gap(int(gap_id))
        if not ok:
            raise HTTPException(404, "knowledge gap not found")
        return JSONResponse({"deleted": int(gap_id)})

    @app.post("/api/knowledge-gaps/{gap_id}/resolve")
    async def resolve_knowledge_gap(
        gap_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        answer: str | None = None
        if isinstance(payload, dict):
            raw_answer = payload.get("answer")
            if raw_answer is not None and not isinstance(raw_answer, str):
                raise HTTPException(400, "answer must be a string")
            if isinstance(raw_answer, str):
                answer = raw_answer
        snapshot = session.resolve_knowledge_gap(int(gap_id), answer=answer)
        if snapshot is None:
            raise HTTPException(404, "knowledge gap not found")
        return JSONResponse({"gap": snapshot})

    # ── REST: curiosity seeds (K9) ───────────────────────────────────

    @app.post("/api/curiosity-seeds/run")
    async def run_curiosity_seed_worker() -> JSONResponse:
        """Force a single ``CuriositySeedWorker.run()`` and return the result.

        Used by the Memory tab "Regenerate now" button so a tester
        can verify the worker's output without waiting for the next
        idle window. Mirrors the cooperative shape of the other
        on-demand worker hooks: the call runs synchronously inside
        the request handler since the worker is already designed to
        be quick (one LLM call + a handful of embeds).
        """
        worker = getattr(session, "_curiosity_seed_worker", None)
        if worker is None:
            raise HTTPException(503, "curiosity seed worker unavailable")
        try:
            result = worker.run()
        except Exception as exc:
            raise HTTPException(500, f"worker run failed: {exc}") from exc
        return JSONResponse({"result": result or {}})

    # ── REST: long-term goals (K1) ───────────────────────────────────

    @app.post("/api/goals/run")
    async def run_goal_worker() -> JSONResponse:
        """Force a single ``GoalWorker.run()`` and return the result.

        Mirrors the cooperative shape of ``/api/curiosity-seeds/run``: the
        Memory tab's "Regenerate now" / "Reflect now" button posts here so
        a tester can verify the worker's output (bootstrap on a cold ring,
        or one reflection note on an existing goal) without waiting for
        the next idle tick. Bypasses the idle-window gate but still
        respects the worker's own rate limiter, so calling this in a
        loop won't blow past ``agent.goal_worker_per_*_cap``.
        """
        worker = getattr(session, "_goal_worker", None)
        if worker is None:
            raise HTTPException(503, "goal worker unavailable")
        try:
            result = worker.run()
        except Exception as exc:
            raise HTTPException(500, f"worker run failed: {exc}") from exc
        return JSONResponse({"result": result or {}})

    # ── REST: memory conflicts (F5) ──────────────────────────────────

    @app.get("/api/memory-conflicts")
    def list_memory_conflicts(
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        include_recent: bool = True,
    ) -> JSONResponse:
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        status_norm = (status or "").strip().lower() or None
        snapshot = session.list_memory_conflicts(
            limit=clamped_limit,
            offset=clamped_offset,
            status=status_norm,
            include_recently_resolved=bool(include_recent),
        )
        return JSONResponse(snapshot)

    @app.get("/api/topic-graph")
    def get_topic_graph() -> JSONResponse:
        """K9: read-only snapshot of the memory topic-cluster graph.

        Advisory + lazily rebuilt from the in-process memory mirror, so
        there's no body and no WS event -- the Memory-tab panel fetches
        on open and on manual refresh.
        """
        return JSONResponse(session.topic_graph_snapshot())

    @app.patch("/api/topic-graph/clusters/{cluster_id}")
    async def rename_topic_cluster(
        cluster_id: int, payload: dict[str, Any]
    ) -> JSONResponse:
        """F10l: override a cluster's label (sticky across refits)."""
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        label = payload.get("label")
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(400, "label must be a non-empty string")
        try:
            result = session.rename_topic_cluster(int(cluster_id), label)
        except Exception as exc:
            raise HTTPException(500, f"rename failed: {exc}") from exc
        if result is None:
            raise HTTPException(404, "cluster not found (or topic graph off)")
        return JSONResponse(result)

    @app.post("/api/topic-graph/clusters/{cluster_id}/pin")
    async def pin_topic_cluster(
        cluster_id: int, payload: dict[str, Any]
    ) -> JSONResponse:
        """F10l: pin / unpin every member of a cluster."""
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        pinned = payload.get("pinned")
        if not isinstance(pinned, bool):
            raise HTTPException(400, "pinned must be a boolean")
        try:
            result = session.set_topic_cluster_pinned(int(cluster_id), pinned)
        except Exception as exc:
            raise HTTPException(500, f"pin failed: {exc}") from exc
        if result is None:
            raise HTTPException(404, "cluster not found (or topic graph off)")
        return JSONResponse(result)

    @app.post("/api/topic-graph/clusters/{cluster_id}/forget")
    def forget_topic_cluster(cluster_id: int) -> JSONResponse:
        """F10l: bulk-archive a topic (skips pinned members)."""
        try:
            result = session.forget_topic_cluster(int(cluster_id))
        except Exception as exc:
            raise HTTPException(500, f"forget failed: {exc}") from exc
        if result is None:
            raise HTTPException(404, "cluster not found (or topic graph off)")
        return JSONResponse(result)

    @app.get("/api/persona-drift")
    def get_persona_drift() -> JSONResponse:
        """K10: last persona-regression snapshot (``{}`` until first run).

        Pull-only — the Diagnostics panel fetches on open and after a
        manual "Run check", so there's no WS event.
        """
        return JSONResponse(session.persona_regression_snapshot())

    @app.post("/api/persona-drift/run")
    def run_persona_drift() -> JSONResponse:
        """K10: replay the golden-turn fixture and return a fresh snapshot.

        Synchronous handler — FastAPI runs it in the threadpool, so the
        blocking worker-LLM calls stay off the event loop.
        """
        return JSONResponse(session.run_persona_regression())

    @app.post("/api/memory-conflicts/{pair_id}/resolve")
    async def resolve_memory_conflict(
        pair_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        winner_id = payload.get("winner_id")
        action = payload.get("action", "demote")
        if not isinstance(winner_id, int):
            raise HTTPException(400, "winner_id must be an integer")
        if not isinstance(action, str):
            raise HTTPException(400, "action must be a string")
        try:
            result = session.resolve_memory_conflict(
                int(pair_id),
                winner_id=int(winner_id),
                action=action,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "conflict pair not found")
        hub.broadcast({
            "type": "memory_conflict_resolved",
            "pair_id": int(pair_id),
        })
        return JSONResponse(result)

    @app.post("/api/memory-conflicts/{pair_id}/dismiss")
    async def dismiss_memory_conflict(pair_id: int) -> JSONResponse:
        ok = session.dismiss_memory_conflict(int(pair_id))
        if not ok:
            raise HTTPException(404, "conflict pair not found")
        hub.broadcast({
            "type": "memory_conflict_dismissed",
            "pair_id": int(pair_id),
        })
        return JSONResponse({"dismissed": int(pair_id)})

    # ── REST: theory-of-mind beliefs (K2) ────────────────────────────

    @app.get("/api/beliefs")
    def list_beliefs(
        limit: int = 50,
        offset: int = 0,
        kind: str | None = None,
        status: str | None = None,
    ) -> JSONResponse:
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        kind_norm = (kind or "").strip().lower() or None
        status_norm = (status or "").strip().lower() or None
        snapshot = session.list_beliefs(
            limit=clamped_limit,
            offset=clamped_offset,
            kind=kind_norm,
            status=status_norm,
        )
        return JSONResponse(snapshot)

    @app.post("/api/beliefs")
    async def create_belief(
        payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kind = payload.get("kind")
        topic = payload.get("topic")
        state = payload.get("predicted_state")
        confidence = payload.get("confidence")
        if not isinstance(kind, str) or kind.strip().lower() not in ("mood", "opinion"):
            raise HTTPException(400, "kind must be 'mood' or 'opinion'")
        if not isinstance(topic, str) or not topic.strip():
            raise HTTPException(400, "topic must be a non-empty string")
        if not isinstance(state, str) or not state.strip():
            raise HTTPException(400, "predicted_state must be a non-empty string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        belief = session.add_belief(
            kind=kind.strip().lower(),
            topic=topic,
            predicted_state=state,
            confidence=float(confidence) if confidence is not None else None,
        )
        if belief is None:
            raise HTTPException(503, "belief tracking unavailable")
        return JSONResponse({"belief": belief})

    @app.patch("/api/beliefs/{belief_id}")
    async def patch_belief(
        belief_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        predicted_state = payload.get("predicted_state")
        confidence = payload.get("confidence")
        status = payload.get("status")
        if predicted_state is not None and not isinstance(predicted_state, str):
            raise HTTPException(400, "predicted_state must be a string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        if status is not None and not isinstance(status, str):
            raise HTTPException(400, "status must be a string")
        # Reject empty PATCH (mirrors PATCH /api/memories behaviour).
        if predicted_state is None and confidence is None and status is None:
            raise HTTPException(
                400, "expected at least one of predicted_state/confidence/status",
            )
        try:
            belief = session.update_belief(
                int(belief_id),
                predicted_state=predicted_state,
                confidence=float(confidence) if confidence is not None else None,
                status=status,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if belief is None:
            raise HTTPException(404, "belief not found")
        return JSONResponse({"belief": belief})

    @app.delete("/api/beliefs/{belief_id}")
    async def delete_belief(belief_id: int) -> JSONResponse:
        ok = session.delete_belief(int(belief_id))
        if not ok:
            raise HTTPException(404, "belief not found")
        return JSONResponse({"deleted": int(belief_id)})

    # ── REST: fact-checker status (F1) ───────────────────────────────

    @app.get("/api/fact-checker/status")
    def fact_checker_status() -> JSONResponse:
        snapshot = session.fact_checker_status()
        return JSONResponse(snapshot)

    @app.post("/api/memories/{memory_id}/pin")
    async def pin_memory(memory_id: int, payload: dict[str, Any] | None = None) -> JSONResponse:
        # ``pinned`` defaults to True (toggle-on); the editor sends an
        # explicit ``{pinned: false}`` to un-pin.
        target = True
        if isinstance(payload, dict) and "pinned" in payload:
            value = payload.get("pinned")
            if not isinstance(value, bool):
                raise HTTPException(400, "pinned must be a boolean")
            target = value
        updated = session.set_memory_pinned(int(memory_id), target)
        if updated is None:
            raise HTTPException(404, "memory not found")
        return JSONResponse({"memory": updated})

    # ── REST: Aiko's room (virtual world) ───────────────────────────

    def _on_world(patch: dict[str, Any]) -> None:
        # Single typed event broadcast over WS. The frontend reducer
        # surgically merges {state} / {location} / {item} /
        # {deleted_*_id} / {snapshot} into its store slice.
        try:
            hub.broadcast({"type": "world_updated", "patch": dict(patch)})
        except Exception:
            log.debug("world updated broadcast failed", exc_info=True)

    try:
        session.add_world_listener(_on_world)
    except Exception:
        log.debug("world listener subscription failed", exc_info=True)

    def _on_thread_note(payload: dict[str, Any]) -> None:
        # K21: fresh-eyes note upserted. The sidebar refetches its
        # session list on this event to pick up the new title.
        try:
            hub.broadcast({"type": "thread_note_updated", "payload": dict(payload)})
        except Exception:
            log.debug("thread note broadcast failed", exc_info=True)

    try:
        session.add_thread_note_listener(_on_thread_note)
    except Exception:
        log.debug("thread note listener subscription failed", exc_info=True)

    @app.get("/api/world")
    def get_world() -> JSONResponse:
        return JSONResponse(session.world_snapshot())

    @app.patch("/api/world/state")
    async def patch_world_state(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        if "location_id" in payload:
            value = payload["location_id"]
            if value is not None and not isinstance(value, int):
                raise HTTPException(400, "location_id must be an integer or null")
            kwargs["location_id"] = value
        for field_name in ("posture", "activity", "mood_note"):
            if field_name in payload:
                value = payload[field_name]
                if value is not None and not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if not kwargs:
            raise HTTPException(
                400,
                "patch must include at least one of location_id, posture, activity, mood_note",
            )
        result = session.update_world_state(**kwargs)
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse({"state": result})

    @app.post("/api/world/locations")
    async def create_world_location(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "name must be a non-empty string")
        slug = payload.get("slug")
        if slug is not None and not isinstance(slug, str):
            raise HTTPException(400, "slug must be a string")
        description = payload.get("description", "") or ""
        if not isinstance(description, str):
            raise HTTPException(400, "description must be a string")
        result = session.add_world_location(
            slug=slug if isinstance(slug, str) and slug.strip() else None,
            name=name,
            description=description,
        )
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse({"location": result})

    @app.patch("/api/world/locations/{location_id}")
    async def patch_world_location(
        location_id: int, payload: dict[str, Any],
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        for field_name in ("name", "description"):
            if field_name in payload:
                value = payload[field_name]
                if not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if "position" in payload:
            value = payload["position"]
            if not isinstance(value, int):
                raise HTTPException(400, "position must be an integer")
            kwargs["position"] = value
        if not kwargs:
            raise HTTPException(400, "patch must include at least one field")
        result = session.update_world_location(int(location_id), **kwargs)
        if result is None:
            raise HTTPException(404, "location not found")
        return JSONResponse({"location": result})

    @app.delete("/api/world/locations/{location_id}")
    def delete_world_location(location_id: int) -> JSONResponse:
        ok = session.delete_world_location(int(location_id))
        if not ok:
            raise HTTPException(404, "location not found")
        return JSONResponse({"deleted_location_id": int(location_id)})

    @app.post("/api/world/items")
    async def create_world_item(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "name must be a non-empty string")
        kind = payload.get("kind", "other")
        if not isinstance(kind, str):
            raise HTTPException(400, "kind must be a string")
        slug = payload.get("slug")
        if slug is not None and not isinstance(slug, str):
            raise HTTPException(400, "slug must be a string")
        description = payload.get("description", "") or ""
        if not isinstance(description, str):
            raise HTTPException(400, "description must be a string")
        location_id = payload.get("location_id")
        if location_id is not None and not isinstance(location_id, int):
            raise HTTPException(400, "location_id must be an integer or null")
        consumable = payload.get("consumable", False)
        if not isinstance(consumable, bool):
            raise HTTPException(400, "consumable must be a boolean")
        quantity = payload.get("quantity", 1)
        if not isinstance(quantity, int) or quantity < 1:
            raise HTTPException(400, "quantity must be a positive integer")
        state = payload.get("state")
        if state is not None and not isinstance(state, dict):
            raise HTTPException(400, "state must be an object or null")
        given_by = payload.get("given_by")
        if given_by is not None and not isinstance(given_by, str):
            raise HTTPException(400, "given_by must be a string")
        result = session.add_world_item(
            name=name,
            kind=kind,
            slug=slug if isinstance(slug, str) and slug.strip() else None,
            description=description,
            location_id=location_id,
            consumable=consumable,
            quantity=quantity,
            state=state,
            given_by=given_by,
        )
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse({"item": result})

    @app.patch("/api/world/items/{item_id}")
    async def patch_world_item(
        item_id: int, payload: dict[str, Any],
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        for field_name in ("name", "description", "kind"):
            if field_name in payload:
                value = payload[field_name]
                if not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if "location_id" in payload:
            value = payload["location_id"]
            if value is not None and not isinstance(value, int):
                raise HTTPException(400, "location_id must be an integer or null")
            kwargs["location_id"] = value
        if "quantity" in payload:
            value = payload["quantity"]
            if not isinstance(value, int) or value < 0:
                raise HTTPException(400, "quantity must be a non-negative integer")
            kwargs["quantity"] = value
        if "state" in payload:
            value = payload["state"]
            if not isinstance(value, dict):
                raise HTTPException(400, "state must be an object")
            kwargs["state"] = value
        if not kwargs:
            raise HTTPException(400, "patch must include at least one field")
        result = session.update_world_item(int(item_id), **kwargs)
        if result is None:
            raise HTTPException(404, "item not found")
        return JSONResponse({"item": result})

    @app.delete("/api/world/items/{item_id}")
    def delete_world_item(item_id: int) -> JSONResponse:
        ok = session.delete_world_item(int(item_id))
        if not ok:
            raise HTTPException(404, "item not found")
        return JSONResponse({"deleted_item_id": int(item_id)})

    @app.post("/api/world/items/{item_id}/consume")
    async def consume_world_item(
        item_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        amount = 1
        if isinstance(payload, dict) and "amount" in payload:
            value = payload["amount"]
            if not isinstance(value, int) or value < 1:
                raise HTTPException(400, "amount must be a positive integer")
            amount = value
        result = session.consume_world_item(int(item_id), amount=amount)
        if result is None:
            raise HTTPException(404, "item not found")
        return JSONResponse(result)

    @app.post("/api/world/seed")
    async def seed_world(force: bool = False) -> JSONResponse:
        result = session.reseed_world(force=bool(force))
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse(result)

    # ── REST + WS bridge: Background tasks (chunk 13) ───────────────
    #
    # ``/api/tasks`` is read-mostly: paginated history, single-row
    # snapshot, cancel, and answer. There's deliberately NO
    # ``POST /api/tasks`` to spawn new tasks — spawning is exclusively
    # Aiko's job (``start_*`` LLM tools) or system code's job (idle
    # workers, MCP debug). The frontend's role is observation +
    # cancel + answer. See ``docs/brain-orchestration.md`` for the
    # rationale.
    #
    # The WS listener bridge fans every orchestrator event out as a
    # JSON frame so the frontend can keep its local task cache in
    # sync without polling. ``visible_to_user=false`` rows are
    # filtered at the bridge — system-internal tasks never reach the
    # wire.


