"""Read-only bounded diagnostics; querying never probes or runs a worker."""
from fastapi import HTTPException


def register(app, session):
    def store():
        value = getattr(session, "_idle_life_store", None)
        if value is None:
            raise HTTPException(503, "Idle-life history is unavailable")
        return value

    @app.get("/api/world/idle-life")
    def history(hours: float = 72, before_id: int | None = None, worker: str | None = None,
                actor: str | None = None, kind: str | None = None, limit: int = 50):
        return store().history(hours=hours, before_id=before_id, worker=worker,
                               actor=actor, kind=kind, limit=limit)

    @app.get("/api/world/idle-life/events/{event_id}")
    def event(event_id: int):
        row = store().event(event_id)
        if row is None:
            raise HTTPException(404, "Event unavailable or outside retention")
        return row

    @app.get("/api/world/idle-life/summary")
    def summary(hours: float = 72):
        return store().summary(hours=hours)

    @app.get("/api/world/idle-life/decisions/{run_id}/replay")
    def replay(run_id: str):
        from app.core.world.idle_life_replay import replay_manifest

        world = getattr(session, "_world_store", None)
        if world is None:
            raise HTTPException(503, "World is unavailable")
        try:
            return replay_manifest(store().decision(run_id)["events"], world)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/world/idle-life/decisions/{run_id}")
    def decision(run_id: str):
        return store().decision(run_id)
