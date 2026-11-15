"""Read-only time-retained behavior evidence, separate from force/debug actions."""
import json


def register(mcp, session):
    def call(method, **kwargs):
        store = getattr(session, "_idle_life_store", None)
        if store is None:
            return json.dumps({"error": "Idle-life history is unavailable"})
        return json.dumps(getattr(store, method)(**kwargs), ensure_ascii=True)

    @mcp.tool()
    def get_idle_life_history(hours: float = 72, before_id: int | None = None,
                              worker: str = "", actor: str = "", kind: str = "",
                              limit: int = 50) -> str:
        """Inspect up to 14 days of decisions, holds and effects; cursor-paginated.

        This does not run workers. Recorded time filters remain real even if the
        debug narrative clock is shifted. Missing history is unknown, not inactivity.
        """
        return call("history", hours=hours, before_id=before_id, worker=worker,
                    actor=actor, kind=kind, limit=limit)

    @mcp.tool()
    def get_idle_life_decision(run_id: str) -> str:
        """Inspect linked choices, context source IDs and outcomes for one run.

        Attribution is model-reported, not proof of causality. Step receipts and
        observed state deltas have distinct evidence strength. No private reasoning.
        """
        return call("decision", run_id=run_id)

    @mcp.tool()
    def get_idle_life_summary(hours: float = 72) -> str:
        """Count runs, effects, admission intervals and review flags over a real-time window.

        Rest, progress and household upkeep are not a productivity score. Consult
        telemetry coverage and actual event evidence before diagnosing a loop.
        """
        return call("summary", hours=hours)
