"""MCP diagnostics and one-shot controls for the persisted sleep lifecycle."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from app.core.world.sleep_state import VALID_ACTIONS

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_sleep_state(episode_limit: int = 10) -> str:
        """Sleep snapshot, propensity inputs, episodes, and worker suppression."""
        try:
            return json.dumps(
                session.sleep_diagnostics(episode_limit=episode_limit),
                indent=2,
                default=str,
            )
        except Exception as exc:
            log.debug("get_sleep_state failed", exc_info=True)
            return json.dumps({"error": str(exc)})

    @mcp.tool()
    def force_sleep_transition(
        action: str,
        sleep_kind: str = "overnight",
        reason_text: str = "",
    ) -> str:
        """Arm and immediately run one legal sleep action for deterministic repro."""
        proposal = str(action or "").strip().lower()
        if proposal not in VALID_ACTIONS:
            return json.dumps(
                {"error": "invalid action", "valid_actions": sorted(VALID_ACTIONS)}
            )
        try:
            session.debug_overrides.arm(
                "sleep_force_transition",
                {
                    "action": proposal,
                    "sleep_kind": sleep_kind,
                    "reason_text": reason_text,
                },
            )
            run = session.run_sleep_lifecycle_debug()
            return json.dumps(
                {
                    "armed": not bool(run["ran"]),
                    **run,
                    "snapshot": session.sleep_snapshot(),
                },
                indent=2,
                default=str,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @mcp.tool()
    def force_sleep_interruption(text: str = "Aiko?") -> str:
        """Record one synthetic user interruption while Aiko is asleep."""
        try:
            session.debug_overrides.arm(
                "sleep_force_interruption",
                {"session_id": session.session_key, "text": str(text or "Aiko?")},
            )
            run = session.run_sleep_lifecycle_debug()
            return json.dumps(
                {**run, "snapshot": session.sleep_snapshot()},
                indent=2,
                default=str,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

