"""K96 debug facade: the post-reply think pass, read and forced by hand.

The pass itself is invisible by construction -- it runs in a background
job after the reply is already out, and most of the time it correctly
decides to do nothing. So the two questions a debugger actually has are
"did it run, and what did it decide" and "run it now so I can watch",
and neither is answerable from the outside without reaching for the
worker, the settings and the transcript at once.

That is what this file exists to prevent. It mirrors
:mod:`app.core.session.hypothesis_debug_mixin`: the MCP tool holds a
bound method rather than a store, so renaming ``_second_thought_worker``
stays a local edit instead of a debug tool that silently reports
``worker_registered: false`` forever.

State ownership (``self._second_thought_worker``, ``self._settings``,
``self._chat_db``) lives in ``SessionController.__init__`` and the worker
init mixin -- do not move it here.
"""
from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("app.session")

#: How far back to look for the pair to reflect on. A forced draft wants
#: the newest user/assistant exchange; anything deeper is a different
#: turn and would reflect on the wrong thing.
_REPLAY_LOOKBACK = 8


class SecondThoughtDebugMixin:
    """Reads and one-shot forcing for the K96 think pass."""

    def second_thought_state(self) -> dict[str, Any]:
        """Settings, worker funnel, and the shelf, in one read.

        ``stats`` is a funnel and reads best as one: ``scheduled`` counts
        calls actually spent, ``declined`` is the pass saying this turn
        needed none (**the designed majority** -- a high ratio is health,
        not failure), and ``unparsed`` is the bug signal. The ``skipped_*``
        counters never spent a call at all.

        ``queued`` against the pool's ``used`` is the real verdict: a
        thought drafted and never spoken is one she was handed and
        dropped. Never raises.
        """
        from app.core.proactive.cue_accounting import policy_for

        agent = getattr(self._settings, "agent", None)
        worker = getattr(self, "_second_thought_worker", None)
        policy = policy_for("second_thought")
        out: dict[str, Any] = {
            "enabled": bool(getattr(agent, "second_thought_enabled", False)),
            "max_tokens": int(getattr(agent, "second_thought_max_tokens", 160)),
            "min_gap_seconds": int(
                getattr(agent, "second_thought_min_gap_seconds", 180)
            ),
            "min_user_chars": int(
                getattr(agent, "second_thought_min_user_chars", 80)
            ),
            "min_reply_chars": int(
                getattr(agent, "second_thought_min_reply_chars", 120)
            ),
            "worker_registered": worker is not None,
            # The pass runs on the CHAT model on purpose: its input is the
            # prefix the turn just cached, so the worker model would be
            # cheaper per token and blind to the context this feature
            # exists to re-read.
            "model": getattr(worker, "_model", None),
            "stats": worker.stats() if worker is not None else {},
            "inventory_target": getattr(policy, "inventory_target", None),
            "ttl_hours": getattr(policy, "ttl_hours", None),
            "force_next": bool(
                self.debug_overrides.peek("second_thought_force_next", False)
            ),
        }
        out["cadence"] = self.cue_pool_cadence("second_thought")
        try:
            page = self.list_cue_pool(cue_type="second_thought", limit=10)
            out["pool"] = page.get("cues") or []
        except Exception:
            log.debug("second-thought pool read failed", exc_info=True)
            out["pool"] = []
        return out

    def force_second_thought_draft(self) -> dict[str, Any]:
        """Run the pass once, now, against the turn that just happened.

        Bypasses the clock, the character floors and the stock check, but
        not ``agent.second_thought_enabled`` -- the worker owns that
        refusal, because a master switch a debug path can talk past is not
        a master switch.

        The exchange is replayed from the captured prompt snapshot plus
        the transcript tail rather than re-assembled, so a forced draft
        sees what the live job would see. One caveat when reading latency
        off this path: the snapshot keeps the prompt *string* and not the
        breakpoint offsets, so the input is a full send rather than the
        cache read the live job gets.

        Returns a dict with an ``error`` key when there is nothing to
        reflect on yet, rather than raising -- the caller is a debug tool
        and "no turn has happened" is an answer, not a fault.
        """
        worker = getattr(self, "_second_thought_worker", None)
        if worker is None:
            return {"error": "worker not registered"}
        system_prompt = str(
            (self.get_last_system_prompt() or {}).get("prompt") or ""
        )
        if not system_prompt:
            return {"error": "no system prompt captured yet — send a turn"}
        user_text, assistant_text = self._last_exchange()
        if not assistant_text:
            return {"error": "no assistant turn found to reflect on"}
        thought = worker.maybe_run(
            system_prompt=system_prompt,
            user_text=user_text,
            assistant_text=assistant_text,
            session_key=self.session_key,
            force=True,
        )
        return {
            "ran": True,
            "drafted": thought is not None,
            "subject": thought.subject if thought else "",
            "thought": thought.thought if thought else "",
            "stats": worker.stats(),
        }

    def _last_exchange(self) -> tuple[str, str]:
        """The newest user line and the newest assistant line, or blanks."""
        try:
            rows = self._chat_db.get_messages(
                self.session_key, limit=_REPLAY_LOOKBACK,
            )
        except Exception:
            log.debug("second-thought replay read failed", exc_info=True)
            return "", ""

        def newest(role: str) -> str:
            return next(
                (
                    str(row.content or "")
                    for row in reversed(rows)
                    if row.role == role
                ),
                "",
            )

        return newest("user"), newest("assistant")


__all__ = ["SecondThoughtDebugMixin"]
