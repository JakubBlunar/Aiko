from __future__ import annotations

import logging
import threading
from typing import Any
from fastapi import File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from app.core.infra.settings import OUTFIT_MODES


log = logging.getLogger("app.web.server")


def register(app, session, hub, _broadcast_context_window, live_session) -> None:
    """REST routes: tasks files routes."""
    def _resolve_task_user_id() -> str:
        """Resolve the task-row user id for REST task queries.

        Lifts ``session._user_id`` (set by the identity layer) so
        REST + background task rows land on the same user. Falls back
        to ``"default"`` so a brand-new install before onboarding
        still has a coherent user_id stamp.
        """
        return str(getattr(session, "_user_id", "default") or "default")

    # Tracks task IDs whose ``task_started`` was suppressed because
    # ``visible_to_user=false``. ``task_progress`` events for those
    # IDs must also be filtered (the orchestrator dispatches them
    # regardless of visibility); ``task_completed`` clears the entry.
    # The set lives in the bridge closure so it doesn't survive a
    # listener resubscribe — that's intentional since the orchestrator
    # itself doesn't persist anything we don't want to lose.
    _hidden_task_ids: set[int] = set()
    _hidden_lock = threading.Lock()

    def _on_task_event(kind: str, payload: dict[str, Any]) -> None:
        """Broadcast every task lifecycle event to connected WS clients.

        Runs on the orchestrator's worker thread (or the caller's
        thread for ``task_started`` / cancel). Must stay cheap —
        ``hub.broadcast`` queues to each client's send loop.

        ``visible_to_user=false`` snapshots are dropped here so the
        wire only ever carries user-visible tasks. The orchestrator
        still fans the event out so future metric / audit listeners
        can opt in to system-internal traffic.
        """
        try:
            if kind == "task_progress":
                task_id = int(payload.get("task_id", 0) or 0)
                with _hidden_lock:
                    if task_id in _hidden_task_ids:
                        return
                hub.broadcast(
                    {
                        "type": "task_progress",
                        "task_id": task_id,
                        "patch": dict(payload.get("patch", {}) or {}),
                    }
                )
                return
            task = payload.get("task") if isinstance(payload, dict) else None
            if not isinstance(task, dict):
                return
            visible = bool(task.get("visible_to_user", True))
            task_id = int(task.get("id", 0) or 0)
            if kind == "task_started" and not visible and task_id:
                with _hidden_lock:
                    _hidden_task_ids.add(task_id)
            if kind == "task_completed" and task_id:
                # Always clear so the set doesn't grow unbounded
                # across long sessions; visibility filter still
                # blocks the broadcast below for hidden rows.
                with _hidden_lock:
                    _hidden_task_ids.discard(task_id)
            if not visible:
                return
            hub.broadcast({"type": kind, "task": dict(task)})
        except Exception:
            log.debug("task event broadcast failed: kind=%s", kind, exc_info=True)

    try:
        orchestrator = getattr(session, "_task_orchestrator", None)
        if orchestrator is not None:
            orchestrator.add_task_listener(_on_task_event)
    except Exception:
        log.debug("task listener subscribe failed", exc_info=True)

    @app.get("/api/tasks")
    def list_tasks(
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        roots_only: bool = False,
    ) -> JSONResponse:
        """Paginated task history for the current user.

        Filters ``visible_to_user=false`` rows. ``status`` accepts
        one of the canonical task statuses (``running``,
        ``awaiting_input``, ``paused``, ``done``, ``failed``,
        ``cancelled``, ``interrupted``) or omitted for all. ``limit``
        is clamped to ``[1, 200]``. ``roots_only=true`` restricts the
        page (and ``total``) to top-level tasks so the Tasks tab can
        render parents only and fetch each parent's children on
        demand via ``GET /api/tasks/{id}/children``.
        """
        from app.core.tasks import task_snapshot as _snapshot
        from app.core.tasks.task_handler import VALID_STATUSES

        store = getattr(session, "_task_store", None)
        if store is None:
            return JSONResponse(
                {"tasks": [], "count": 0, "total": 0, "enabled": False}
            )
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        status_norm: str | None = None
        if status is not None:
            candidate = str(status).strip().lower()
            if candidate:
                if candidate not in VALID_STATUSES:
                    raise HTTPException(
                        400,
                        f"status must be one of {sorted(VALID_STATUSES)}",
                    )
                status_norm = candidate
        user_id = _resolve_task_user_id()
        try:
            rows = store.list_for_user(
                user_id,
                status=status_norm,
                limit=clamped_limit,
                offset=clamped_offset,
                visible_only=True,
                roots_only=bool(roots_only),
            )
            total = store.count_for_user(
                user_id,
                status=status_norm,
                visible_only=True,
                roots_only=bool(roots_only),
            )
        except Exception as exc:
            log.exception("list_tasks failed: status=%s", status_norm)
            raise HTTPException(500, f"list failed: {exc}") from exc
        items = [_snapshot(r) for r in rows]
        return JSONResponse(
            {
                "tasks": items,
                "count": len(items),
                "total": int(total),
                "enabled": True,
            }
        )

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> JSONResponse:
        """Single-row snapshot. 404 when row missing or hidden."""
        from app.core.tasks import task_snapshot as _snapshot

        store = getattr(session, "_task_store", None)
        if store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        return JSONResponse({"task": _snapshot(row)})

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task_rest(task_id: int) -> JSONResponse:
        """User-initiated cancel.

        Idempotent: cancelling an already-terminal task returns 200
        with ``cancelled=False`` so the UI can render "already done"
        without a noisy error.
        """
        orch = getattr(session, "_task_orchestrator", None)
        if orch is None:
            raise HTTPException(503, "task subsystem unavailable")
        store = getattr(session, "_task_store", None)
        if store is not None:
            row = store.get(int(task_id))
            if row is None or not bool(row.visible_to_user):
                raise HTTPException(404, "task not found")
        try:
            cancelled = orch.cancel(int(task_id))
        except Exception as exc:
            log.exception("task cancel failed: task=%d", task_id)
            raise HTTPException(500, f"cancel failed: {exc}") from exc
        return JSONResponse(
            {"task_id": int(task_id), "cancelled": bool(cancelled)}
        )

    @app.post("/api/tasks/{task_id}/answer")
    async def answer_task_rest(
        task_id: int, payload: dict[str, Any]
    ) -> JSONResponse:
        """Resolve an ``awaiting_input`` task with a user-supplied answer.

        Body shape: ``{"input": str}``. Mirrors the
        :class:`TaskInputNeeded` -> ``answer`` semantics in
        :class:`TaskOrchestrator`. Returns 409 when the task is not
        currently ``awaiting_input`` so the UI can refresh and show
        the new state.
        """
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        answer = payload.get("input")
        if answer is None:
            answer = payload.get("answer")  # forgiving alias
        if not isinstance(answer, str) or not answer.strip():
            raise HTTPException(400, "input must be a non-empty string")
        orch = getattr(session, "_task_orchestrator", None)
        if orch is None:
            raise HTTPException(503, "task subsystem unavailable")
        store = getattr(session, "_task_store", None)
        if store is not None:
            row = store.get(int(task_id))
            if row is None or not bool(row.visible_to_user):
                raise HTTPException(404, "task not found")
        try:
            accepted = orch.answer(int(task_id), answer)
        except Exception as exc:
            log.exception("task answer failed: task=%d", task_id)
            raise HTTPException(500, f"answer failed: {exc}") from exc
        if not accepted:
            raise HTTPException(
                409,
                "task did not accept the answer (wrong status or handler "
                "unregistered)",
            )
        return JSONResponse({"task_id": int(task_id), "accepted": True})

    @app.get("/api/tasks/{task_id}/events")
    def list_task_events(
        task_id: int,
        limit: int = 100,
        offset: int = 0,
        order: str = "asc",
    ) -> JSONResponse:
        """Paginated event log for a single task (schema v17).

        Returns the audit trail the orchestrator + handlers appended
        via the event-log path. ``order`` is ``asc`` (default,
        chronological replay) or ``desc`` (newest first).
        Clamped to ``[1, 1000]`` per page.
        """
        store = getattr(session, "_task_store", None)
        event_store = getattr(session, "_task_event_store", None)
        if store is None or event_store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        order_norm = str(order or "").strip().lower()
        if order_norm not in ("asc", "desc"):
            order_norm = "asc"
        clamped_limit = max(1, min(int(limit), 1000))
        clamped_offset = max(0, int(offset))
        try:
            events = event_store.list_for_task(
                int(task_id),
                limit=clamped_limit,
                offset=clamped_offset,
                ascending=order_norm == "asc",
            )
            total = event_store.count_for_task(int(task_id))
        except Exception as exc:
            log.exception("list_task_events failed: task=%d", task_id)
            raise HTTPException(500, f"list failed: {exc}") from exc
        return JSONResponse(
            {
                "task_id": int(task_id),
                "events": [
                    {
                        "id": int(e.id),
                        "task_id": int(e.task_id),
                        "type": str(e.type),
                        "data": (dict(e.data) if e.data is not None else None),
                        "created_at": str(e.created_at),
                    }
                    for e in events
                ],
                "count": len(events),
                "total": int(total),
            }
        )

    @app.get("/api/tasks/{task_id}/children")
    def list_task_children(task_id: int) -> JSONResponse:
        """Child tasks of a parent (schema v17 task tree).

        Used by the Tasks tab to lazily expand a parent into its
        workflow steps. Returns visible children only, ascending by
        id (spawn order). No pagination — the per-parent fan-out is
        bounded. 404 mirrors ``get_task`` when the parent row is
        missing or hidden.
        """
        from app.core.tasks import task_snapshot as _snapshot

        store = getattr(session, "_task_store", None)
        if store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        try:
            children = [
                c
                for c in store.list_children(int(task_id))
                if bool(c.visible_to_user)
            ]
        except Exception as exc:
            log.exception("list_task_children failed: task=%d", task_id)
            raise HTTPException(500, f"list failed: {exc}") from exc
        return JSONResponse(
            {
                "task_id": int(task_id),
                "children": [_snapshot(c) for c in children],
                "count": len(children),
            }
        )

    @app.get("/api/tasks/{task_id}/inputs")
    def list_task_inputs(task_id: int) -> JSONResponse:
        """Full input/answer history for a task (schema v17).

        Returns one row per question the handler asked (pending /
        answered / superseded / cancelled). Chronological. No
        pagination — the per-task volume is bounded.
        """
        store = getattr(session, "_task_store", None)
        input_store = getattr(session, "_task_input_store", None)
        if store is None or input_store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        try:
            inputs = input_store.list_for_task(int(task_id), ascending=True)
        except Exception as exc:
            log.exception("list_task_inputs failed: task=%d", task_id)
            raise HTTPException(500, f"list failed: {exc}") from exc
        return JSONResponse(
            {
                "task_id": int(task_id),
                "inputs": [
                    {
                        "id": int(inp.id),
                        "task_id": int(inp.task_id),
                        "prompt": str(inp.prompt),
                        "kind": inp.kind,
                        "options": (
                            list(inp.options) if inp.options is not None else None
                        ),
                        "status": str(inp.status),
                        "response": inp.response,
                        "created_at": str(inp.created_at),
                        "answered_at": inp.answered_at,
                    }
                    for inp in inputs
                ],
                "count": len(inputs),
            }
        )

    # ── REST: Shared moments + Together (schema v7) ─────────────────

    def _on_shared_moment(patch: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "shared_moment_updated", "patch": dict(patch)})
        except Exception:
            log.debug("shared moment broadcast failed", exc_info=True)

    def _on_relationship_axes(state: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "relationship_axes_updated", "axes": dict(state)})
        except Exception:
            log.debug("axes broadcast failed", exc_info=True)

    def _on_vitality(patch: dict[str, Any]) -> None:
        # K68: body-energy update -> avatar gesture/breath amplitude.
        try:
            hub.broadcast({"type": "vitality_changed", **dict(patch)})
        except Exception:
            log.debug("vitality broadcast failed", exc_info=True)

    try:
        session.add_shared_moment_listener(_on_shared_moment)
    except Exception:
        log.debug("shared moment listener subscribe failed", exc_info=True)
    try:
        session.add_relationship_axes_listener(_on_relationship_axes)
    except Exception:
        log.debug("axes listener subscribe failed", exc_info=True)
    try:
        session.add_vitality_listener(_on_vitality)
    except Exception:
        log.debug("vitality listener subscribe failed", exc_info=True)

    def _on_knowledge_gap(patch: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "knowledge_gap_updated", "patch": dict(patch)})
        except Exception:
            log.debug("knowledge gap broadcast failed", exc_info=True)

    try:
        session.add_knowledge_gap_listener(_on_knowledge_gap)
    except Exception:
        log.debug("knowledge gap listener subscribe failed", exc_info=True)

    @app.get("/api/together")
    def get_together() -> JSONResponse:
        return JSONResponse(session.get_together_summary())

    @app.get("/api/shared-moments")
    def list_shared_moments(
        offset: int = 0,
        limit: int = 20,
        vibe: str | None = None,
    ) -> JSONResponse:
        result = session.list_shared_moments(
            offset=max(0, int(offset)),
            limit=max(1, min(int(limit), 100)),
            vibe=vibe,
        )
        return JSONResponse(result)

    @app.post("/api/shared-moments")
    async def create_shared_moment(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise HTTPException(400, "summary must be a non-empty string")
        vibe = payload.get("vibe", "general")
        if not isinstance(vibe, str):
            raise HTTPException(400, "vibe must be a string")
        when = payload.get("when")
        if when is not None and not isinstance(when, str):
            raise HTTPException(400, "when must be an ISO8601 string or null")
        result = session.add_shared_moment(
            summary=summary,
            vibe=vibe,
            when=when,
        )
        if result is None:
            raise HTTPException(503, "shared moments unavailable")
        return JSONResponse({"moment": result})

    @app.patch("/api/shared-moments/{moment_id}")
    async def patch_shared_moment(
        moment_id: int, payload: dict[str, Any],
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        for field_name in ("summary", "vibe", "when"):
            if field_name in payload:
                value = payload[field_name]
                if value is not None and not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if "pinned" in payload:
            value = payload["pinned"]
            if not isinstance(value, bool):
                raise HTTPException(400, "pinned must be a boolean")
            kwargs["pinned"] = value
        if not kwargs:
            raise HTTPException(400, "patch must include at least one field")
        result = session.update_shared_moment(int(moment_id), **kwargs)
        if result is None:
            raise HTTPException(404, "shared moment not found")
        return JSONResponse({"moment": result})

    @app.delete("/api/shared-moments/{moment_id}")
    def delete_shared_moment(moment_id: int) -> JSONResponse:
        ok = session.delete_shared_moment(int(moment_id))
        if not ok:
            raise HTTPException(404, "shared moment not found")
        return JSONResponse({"deleted_moment_id": int(moment_id)})

    @app.post("/api/chat/messages/{message_id}/mark-moment")
    async def mark_message_as_moment(
        message_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        vibe = "general"
        if isinstance(payload, dict) and "vibe" in payload:
            value = payload["vibe"]
            if value is not None and not isinstance(value, str):
                raise HTTPException(400, "vibe must be a string")
            if isinstance(value, str) and value.strip():
                vibe = value
        result = session.mark_message_as_moment(int(message_id), vibe=vibe)
        if result is None:
            raise HTTPException(404, "message not found")
        return JSONResponse({"moment": result})

    # ── REST: K32 user reactions on Aiko bubbles ────────────────────

    @app.post("/api/chat/messages/{message_id}/reactions")
    async def add_user_reaction(
        message_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """K32: register one reaction click on an assistant bubble.

        Body: ``{"kind": "heart" | "hug" | "laugh" | "thumbs" |
        "rose" | "surprise"}``. Returns the new full reactions
        map. Side-effects (axes nudge + inner-life cue) live in
        :meth:`SessionController.apply_user_reaction`.
        """
        if not bool(
            getattr(
                session._settings.agent, "user_reactions_enabled", True,
            ),
        ):
            raise HTTPException(503, "user reactions feature disabled")
        if not isinstance(payload, dict) or "kind" not in payload:
            raise HTTPException(400, "kind is required")
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise HTTPException(400, "kind must be a non-empty string")
        from app.core.relationship.user_reactions import is_valid_kind

        if not is_valid_kind(kind):
            raise HTTPException(
                400, f"unknown reaction kind: {kind}",
            )
        result = session.apply_user_reaction(int(message_id), kind)
        if result is None:
            raise HTTPException(
                404, "message not found or not an assistant bubble",
            )
        return JSONResponse(result)

    @app.delete("/api/chat/messages/{message_id}/reactions/{kind}")
    async def remove_user_reaction(
        message_id: int, kind: str,
    ) -> JSONResponse:
        """K32: undo one reaction click (decrements the counter).

        Symmetric with the POST endpoint for persistence + WS
        broadcast; axes are NOT subtracted on undo.
        """
        if not bool(
            getattr(
                session._settings.agent, "user_reactions_enabled", True,
            ),
        ):
            raise HTTPException(503, "user reactions feature disabled")
        from app.core.relationship.user_reactions import is_valid_kind

        if not is_valid_kind(kind):
            raise HTTPException(
                400, f"unknown reaction kind: {kind}",
            )
        result = session.remove_user_reaction(int(message_id), kind)
        if result is None:
            raise HTTPException(404, "message not found")
        return JSONResponse(result)

    # ── REST: Live2D avatar (fixed Alexia bundle) ───────────────────

    @app.get("/api/avatar")
    def get_avatar() -> JSONResponse:
        return JSONResponse({"avatar": session.avatar_payload()})

    @app.patch("/api/avatar")
    async def patch_avatar(payload: dict[str, Any]) -> JSONResponse:
        scale = payload.get("scale_multiplier")
        outfit = payload.get("auto_outfit")
        expressiveness = payload.get("expressiveness")
        mood_inertia_damping = payload.get("mood_inertia_damping")
        if mood_inertia_damping is not None:
            mood_inertia_damping = bool(mood_inertia_damping)
        if scale is not None:
            try:
                scale_value = float(scale)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    400, "scale_multiplier must be a number",
                ) from exc
            scale = scale_value
        if outfit is not None:
            outfit_normalized = str(outfit).strip().lower()
            if outfit_normalized not in OUTFIT_MODES:
                raise HTTPException(
                    400,
                    "auto_outfit must be one of: "
                    + ", ".join(sorted(OUTFIT_MODES)),
                )
            outfit = outfit_normalized
        if expressiveness is not None:
            try:
                expressiveness_value = float(expressiveness)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    400, "expressiveness must be a number",
                ) from exc
            if expressiveness_value < 0.0 or expressiveness_value > 1.5:
                raise HTTPException(
                    400, "expressiveness must be between 0.0 and 1.5",
                )
            expressiveness = expressiveness_value
        snapshot = session.update_avatar_settings(
            scale_multiplier=scale,
            auto_outfit=outfit,
            expressiveness=expressiveness,
            mood_inertia_damping=mood_inertia_damping,
        )
        hub.broadcast({
            "type": "avatar_settings_changed",
            "settings": dict(snapshot),
        })
        return JSONResponse({"avatar": session.avatar_payload()})

    @app.get("/api/avatar/accessories")
    def get_avatar_accessories() -> JSONResponse:
        """Phase 4 (expression overhaul): per-accessory catalogue.

        Returns ``{accessories: [...], active_outfit: "..."}`` where
        each catalogue entry carries the current value, the rig's
        availability flag, and the outfit gate (if any). The
        SettingsDrawer renders one row per entry and disables rows
        whose ``allowed_outfits`` doesn't include the current
        ``active_outfit``.
        """
        return JSONResponse(session.avatar_accessories_catalogue())

    @app.patch("/api/avatar/accessories")
    async def patch_avatar_accessories(payload: dict[str, Any]) -> JSONResponse:
        """Phase 4 (expression overhaul): merge accessory toggles.

        Validates each key against the known accessory catalogue
        (lollipop / eyeglasses / head_sunglasses / eye_color /
        crossed_arms) and the enum allow-list. Unknown keys or bad
        enum values return 400. Successful patches broadcast an
        ``avatar_settings_changed`` event over the WS hub so the
        renderer's accessory layer re-syncs on the next frame.
        """
        try:
            snapshot = session.update_avatar_accessories(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        hub.broadcast({
            "type": "avatar_settings_changed",
            "settings": dict(snapshot),
        })
        return JSONResponse(session.avatar_accessories_catalogue())

    # ── REST: documents (RAG corpus) ────────────────────────────────

    _MAX_DOCUMENT_UPLOAD_BYTES = 16 * 1024 * 1024  # 16 MB

    @app.get("/api/documents")
    def list_documents() -> JSONResponse:
        ingestor = session.document_ingestor
        if ingestor is None:
            raise HTTPException(503, "RAG document store unavailable")
        return JSONResponse({"documents": ingestor.list_documents()})

    @app.post("/api/documents/upload")
    async def upload_document(file: UploadFile = File(...)) -> JSONResponse:
        ingestor = session.document_ingestor
        if ingestor is None:
            raise HTTPException(503, "RAG document store unavailable")
        if not file.filename:
            raise HTTPException(400, "missing filename")
        body = await file.read()
        if len(body) == 0:
            raise HTTPException(400, "uploaded file is empty")
        if len(body) > _MAX_DOCUMENT_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"upload too large (limit {_MAX_DOCUMENT_UPLOAD_BYTES // (1024 * 1024)} MB)",
            )
        try:
            result = ingestor.ingest(filename=file.filename, data=body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            log.exception("document ingestion failed")
            raise HTTPException(500, f"ingestion failed: {exc}") from exc
        return JSONResponse({
            "document": {
                "document_id": result.document_id,
                "title": result.title,
                "chunk_count": result.chunk_count,
                "bytes_indexed": result.bytes_indexed,
            },
            "documents": ingestor.list_documents(),
        })

    @app.delete("/api/documents/{document_id}")
    def delete_document(document_id: str) -> JSONResponse:
        ingestor = session.document_ingestor
        if ingestor is None:
            raise HTTPException(503, "RAG document store unavailable")
        ok = ingestor.delete_document(document_id)
        if not ok:
            raise HTTPException(404, "document not found")
        return JSONResponse({"deleted": document_id, "documents": ingestor.list_documents()})

    # ── REST: in-chat attachments (D2 Part B) ───────────────────────
    #
    # Images + text files dropped into the chat composer. They land in
    # the managed ``data/attachments/`` root (auto-registered read-only
    # sandbox root) so Aiko can resolve ``Attachments:<file>`` through
    # the describe_image (vision) workflow skill. No bytes are sent to
    # the cloud chat model — the worker (local) model reads them.

    @app.post("/api/chat/attachments")
    async def upload_attachment(file: UploadFile = File(...)) -> JSONResponse:
        from app.core.tasks.attachments import (
            DEFAULT_MAX_ATTACHMENT_BYTES,
            save_attachment,
        )

        if not file.filename:
            raise HTTPException(400, "missing filename")
        body = await file.read()
        if len(body) == 0:
            raise HTTPException(400, "uploaded file is empty")
        # Image allow-list mirrors the live vision config; text set uses
        # the module default. Byte cap rides the vision cap when set.
        vision_cfg = getattr(session._settings.agent, "vision", None)
        image_exts = tuple(
            getattr(vision_cfg, "allowed_extensions", ()) or ()
        ) or None
        max_bytes = int(
            getattr(vision_cfg, "max_bytes", DEFAULT_MAX_ATTACHMENT_BYTES)
            or DEFAULT_MAX_ATTACHMENT_BYTES
        )
        try:
            saved = save_attachment(
                data=body,
                filename=file.filename,
                image_extensions=image_exts,
                max_bytes=max_bytes,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            log.exception("attachment save failed")
            raise HTTPException(500, f"attachment save failed: {exc}") from exc
        return JSONResponse({"attachment": saved.as_dict()})

    @app.delete("/api/chat/attachments/{stored_name}")
    def delete_attachment_endpoint(stored_name: str) -> JSONResponse:
        from app.core.tasks.attachments import delete_attachment

        ok = delete_attachment(stored_name)
        return JSONResponse({"deleted": bool(ok), "stored_name": stored_name})


