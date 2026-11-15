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

    @app.get("/api/world/idle-life/summary")
    def summary(hours: float = 72):
        return store().summary(hours=hours)

    @app.get("/api/world/idle-life/decisions/{run_id}")
    def decision(run_id: str):
        return store().decision(run_id)
