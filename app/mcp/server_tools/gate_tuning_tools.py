"""L45 -- what the self-tuning concept gates have learned, and forcing a run.

Every concept threshold used to be a constant somebody chose once. The L45
worker measures the live distribution daily and solves each calibratable gate
against a declared intent instead, writing ``data/tuning/concept_gates.json``.

These two tools are how you check it from the live app. Read
``get_gate_tuning`` for what a gate became and why; the fields worth looking
at first are ``clamped_by`` (a gate pinned to ``floor`` every run has a wrong
spec, and one reporting ``max_step`` is still walking) and the disagreements
between ``gates[name].value`` and ``live[name]``, which is what an
apply-path failure looks like.

Most gates are deliberately ``observe`` mode: they are recorded and never
written. Those are the ones that *write* to the store (promotion, retirement,
taste synthesis), where a bad value would leave a persistent trace and also
move the distribution the next run measures. The read-side gates, which only
decide what enters one prompt, are the ones cleared to apply.

Offline equivalents, for when the app is not running:
``python scripts/concept_gate_report.py`` (dry run) and ``--trend``.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_gate_tuning(include_history: bool = False) -> str:
        """L45 — dump the learned concept-gate thresholds and the tuner state.

        Returns a JSON dict with:

        - ``enabled`` / ``last_run_at`` / ``cadence_seconds`` /
          ``heartbeat_seconds``: whether the worker registered and where it is
          in its daily cadence. The heartbeat is deliberately shorter than the
          cadence (see the worker's docstring); ``last_run_at`` tracks
          completed *tuning runs*, not scheduler ticks.
        - ``graph_mature``: False means the graph is still too young or too
          small to calibrate against, so every gate is holding its default.
        - ``gates``: per gate — the solved ``value``, its ``mode``, whether it
          was ``applied``, the ``objective`` and ``why``, the unclamped
          ``raw``, which rail had the last word in ``clamped_by``, the
          distribution ``stats`` behind it, and ``unapplied_because`` when it
          did not land.
        - ``user_overridden``: gates pinned by ``config/user.json``. Those are
          measured and their drift recorded, but never applied — the config
          always wins, and nothing in the background edits it.
        - ``live``: the value each gate actually has on the running settings
          object right now. A gate marked ``applied`` whose ``live`` value
          differs from its ``value`` means the apply path missed it.

        ``include_history`` adds each gate's recorded walk. Leave it off for
        a first read; the payload roughly doubles.
        """
        try:
            state = session.gate_tuning_state()
            if not include_history:
                state["gates"] = {
                    name: {
                        key: value
                        for key, value in entry.items()
                        if key != "history"
                    }
                    for name, entry in (state.get("gates") or {}).items()
                }
            return json.dumps(state, indent=2, default=str)
        except Exception as exc:
            return f"get_gate_tuning raised: {exc}"

    @mcp.tool()
    def force_gate_tuning() -> str:
        """L45 — run one gate-tuning pass right now.

        Clears the internal cadence stamp first, so this works on a day the
        tuner already ran (otherwise ``run()`` is a no-op and a working tuner
        would look broken). The pass reads the whole concept store, appends a
        population snapshot line, rewrites the gates file, and applies the
        read-side gates to the *live* settings — those are all read per turn,
        so the effect is immediate with no restart.

        Writes nothing to ``concepts`` and makes no LLM call. Call
        ``get_gate_tuning`` afterwards to see what moved.
        """
        try:
            return json.dumps(session.run_gate_tuning(), indent=2, default=str)
        except Exception as exc:
            return f"force_gate_tuning raised: {exc}"
