"""K10 persona regression — orchestration mixin for SessionController.

Glue between the pure scorer (:mod:`app.core.persona.persona_regression`)
and the live runtime: load the golden-turn fixture, build each turn's
prompt via :meth:`PromptAssembler.build_eval_messages`, run it through the
background-worker LLM client, score the reply, persist the aggregated
snapshot to ``kv_meta``, and return it for REST / MCP / the diagnostics
panel.

Like every other ``app.core.session`` mixin this is *not* a standalone
class — it only works as a base of ``SessionController`` and reads the
``self.*`` attributes set up in ``SessionController.__init__``
(``_settings``, ``_prompt_assembler``, ``_maintenance_client``,
``_chat_db``, ``_effective_worker_model``, ``context_window_size``).

On-demand only: nothing here registers with the IdleWorkerScheduler. A
periodic auto-eval worker is a deferred backlog follow-up (K10-followup).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.persona import persona_regression as pr

log = logging.getLogger("app.persona_regression")

_KV_KEY = "aiko.persona_regression.last_run"
# Per-turn worker token budget. Golden replies are short; this keeps the
# eval cheap and bounded.
_RESPONSE_BUDGET = 512


class PersonaRegressionMixin:
    """On-demand persona-drift eval (golden-turn replay + scoring)."""

    def run_persona_regression(self) -> dict[str, Any]:
        """Replay the golden-turn fixture and persist a scored snapshot.

        Returns the snapshot dict (also written to ``kv_meta``). Best
        effort: an enable-flag / dependency / fixture problem returns an
        empty-ish snapshot with an ``error`` field rather than raising,
        and a single turn that errors is recorded as a failed result
        without aborting the run.
        """
        settings = getattr(self, "_settings", None)
        agent = getattr(settings, "agent", None)
        if agent is not None and not getattr(
            agent, "persona_regression_enabled", True,
        ):
            return pr.build_snapshot([], error="disabled")

        assembler = getattr(self, "_prompt_assembler", None)
        client = getattr(self, "_maintenance_client", None)
        if assembler is None or client is None:
            return pr.build_snapshot([], error="unavailable")

        fixture_path = "data/persona/golden_turns.jsonl"
        if agent is not None:
            fixture_path = getattr(
                agent, "persona_regression_fixture_path", fixture_path,
            )
        turns = pr.load_golden_turns(fixture_path)
        if not turns:
            return self._persist_persona_regression(
                pr.build_snapshot([], error="no_fixture"),
            )

        model = str(getattr(self, "_effective_worker_model", "") or "")
        context_window = int(getattr(self, "context_window_size", 8192) or 8192)
        started = time.perf_counter()
        results: list[pr.GoldenResult] = []

        for turn in turns:
            try:
                messages = assembler.build_eval_messages(
                    turn.user,
                    full_context=(turn.scope == pr.SCOPE_FULL),
                    session_key=getattr(self, "session_key", "persona_regression"),
                    context_window=context_window,
                    response_budget=_RESPONSE_BUDGET,
                )
                reply = client.chat(
                    messages,
                    options={"num_predict": _RESPONSE_BUDGET},
                    model=model or None,
                    surface="persona_regression",
                )
                results.append(pr.score_reply(reply or "", turn))
            except Exception as exc:  # noqa: BLE001 — per-turn isolation
                log.warning(
                    "persona-regression: turn %r failed: %s", turn.id, exc,
                    exc_info=True,
                )
                results.append(
                    pr.GoldenResult(
                        id=turn.id,
                        scope=turn.scope,
                        passed=False,
                        failures=(f"error: {exc}",),
                        reply_preview="",
                    ),
                )

        ran_ms = (time.perf_counter() - started) * 1000.0
        snapshot = pr.build_snapshot(results, model=model, ran_ms=ran_ms)
        log.info(
            "persona-regression: passed=%d/%d model=%s ran_ms=%.0f",
            snapshot["passed"], snapshot["total"], model or "-", ran_ms,
        )
        return self._persist_persona_regression(snapshot)

    def persona_regression_snapshot(self) -> dict[str, Any]:
        """Return the last persisted snapshot (``{}`` when never run)."""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return {}
        try:
            raw = chat_db.kv_get(_KV_KEY)
        except Exception:
            log.debug("persona-regression: kv_get failed", exc_info=True)
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _persist_persona_regression(
        self, snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is not None:
            try:
                chat_db.kv_set(_KV_KEY, json.dumps(snapshot))
            except Exception:
                log.debug(
                    "persona-regression: kv_set failed", exc_info=True,
                )
        return snapshot


__all__ = ["PersonaRegressionMixin"]
