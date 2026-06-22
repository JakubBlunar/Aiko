"""WS listeners + metrics mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
WebSocket listener registries (message / tool-event / tts-state /
metrics / tts-amplitude) and their notify helpers, the TTS-state
bridge, plus the per-turn latency metrics + decision-trace surface.
State ownership stays on ``SessionController.__init__``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.listeners_metrics_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from typing import Any
from collections.abc import Callable
import time


log = logging.getLogger("app.session")


class ListenersMetricsMixin:
    """WS listeners + TTS-state bridge + latency metrics + decision trace."""

    def add_message_listener(
        self, callback: Callable[..., None],
    ) -> None:
        if callback and callback not in self._message_listeners:
            self._message_listeners.append(callback)

    def _notify_message(
        self, speaker: str, text: str, message_id: int | None = None,
    ) -> None:
        """Fan a chat line out to listeners.

        ``message_id`` is the persisted SQLite ``messages.id`` when the
        caller has it (proactive turns pass it so the client can enable
        reactions on the new bubble); it stays ``None`` for callers that
        don't (the streamed-turn path carries the id on ``turn_done``
        instead). Listeners may accept two or three positional args; the
        two-arg ones are called without the id for back-compat.
        """
        for listener in list(self._message_listeners):
            try:
                try:
                    listener(speaker, text, message_id)
                except TypeError:
                    # Legacy two-arg listener — call without the id.
                    listener(speaker, text)
            except Exception:
                log.debug("message listener raised", exc_info=True)

    def add_tool_event_listener(
        self, callback: Callable[[str, dict[str, Any]], None],
    ) -> None:
        listeners = getattr(self, "_tool_event_listeners", None)
        if listeners is None:
            listeners = []
            self._tool_event_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def _notify_tool_event(self, event: str, payload: dict[str, Any]) -> None:
        listeners = getattr(self, "_tool_event_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(event, payload)
            except Exception:
                log.debug("tool event listener raised", exc_info=True)

    def add_tts_state_listener(self, callback: Callable[..., None]) -> None:
        if callback and callback not in self._tts_state_listeners:
            self._tts_state_listeners.append(callback)

    def add_metrics_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to retroactive metrics updates (e.g. tts_ms back-fill)."""
        if callback and callback not in self._metrics_listeners:
            self._metrics_listeners.append(callback)

    def _notify_metrics_updated(self) -> None:
        snapshot = dict(self._last_metrics)
        for listener in list(self._metrics_listeners):
            try:
                listener(snapshot)
            except Exception:
                log.debug("metrics listener raised", exc_info=True)

    def _on_tts_state(self, event: str, payload: dict[str, Any]) -> None:
        # Carry the last assistant reaction over to the next turn so the
        # mood doesn't reset to "neutral" every time. Phase E mood-carryover.
        if event == "start":
            reaction = (payload or {}).get("reaction")
            try:
                self._prompt_assembler.set_last_reaction(reaction)
            except Exception:
                log.debug("set_last_reaction failed", exc_info=True)
            # First "start" after the LLM finished marks audible-from time;
            # subsequent chunk starts in the same turn don't reset it.
            if (
                self._tts_turn_start_at is not None
                and self._tts_turn_first_start_at is None
            ):
                self._tts_turn_first_start_at = time.monotonic()
            # Open the speaking window so background workers can drain.
            try:
                self._scheduler.on_tts_state("start")
            except Exception:
                log.debug("scheduler.on_tts_state(start) failed", exc_info=True)
        elif event == "end":
            # Queue is drained for this turn. Compute total tts_ms (LLM done
            # → audio fully played) and back-fill the last metrics record.
            if self._tts_turn_start_at is not None:
                tts_ms = round(
                    (time.monotonic() - self._tts_turn_start_at) * 1000.0, 1,
                )
                # ``total_ms`` was capture+stt+llm at the time of the LLM
                # turn; add the freshly-measured TTS span on top.
                base_total = float(self._last_metrics.get("total_ms", 0.0) or 0.0)
                base_total -= float(self._last_metrics.get("tts_ms", 0.0) or 0.0)
                self._last_metrics["tts_ms"] = tts_ms
                self._last_metrics["total_ms"] = round(base_total + tts_ms, 1)
                # Mirror into the history tail so averages reflect tts_ms too.
                if self._metrics_history:
                    self._metrics_history[-1]["tts_ms"] = tts_ms
                    self._metrics_history[-1]["total_ms"] = self._last_metrics["total_ms"]
                self._tts_turn_start_at = None
                self._tts_turn_first_start_at = None
                # Re-broadcast metrics so the badge picks up the final tts_ms.
                self._notify_metrics_updated()
            # Close the scheduler window cooperatively.
            try:
                self._scheduler.on_tts_state("end")
            except Exception:
                log.debug("scheduler.on_tts_state(end) failed", exc_info=True)
        for listener in list(self._tts_state_listeners):
            try:
                listener(event, **payload)
            except Exception:
                log.debug("tts state listener raised", exc_info=True)

    def add_tts_amplitude_listener(self, callback: Callable[[float], None]) -> None:
        if callback and callback not in self._tts_amplitude_listeners:
            self._tts_amplitude_listeners.append(callback)

    def _on_tts_amplitude(self, level: float) -> None:
        for listener in list(self._tts_amplitude_listeners):
            try:
                listener(float(level))
            except Exception:
                log.debug("tts amplitude listener raised", exc_info=True)

    def get_decision_trace(self, max_entries: int = 300) -> list[dict[str, str]]:
        items = list(self._decision_trace)
        if max_entries >= len(items):
            return items
        return items[-max_entries:]

    def clear_decision_trace(self) -> None:
        self._decision_trace.clear()

    @staticmethod
    def _zero_metrics() -> dict[str, Any]:
        return {
            "mode": "idle",
            "capture_ms": 0.0,
            "stt_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
            # Token totals (combined streaming + tool-pass).
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            # Ollama timing breakdown (full-precision).
            "total_duration_ms": 0.0,
            "eval_duration_ms": 0.0,
            "prompt_eval_duration_ms": 0.0,
            "tokens_per_second": 0.0,
            # Context fill.
            "context_window": 0,
            "context_source": "fallback",
            "prompt_pct": 0.0,
            # Prompt-assembly telemetry.
            "system_tokens": 0,
            "summary_tokens": 0,
            "rag_tokens": 0,
            "history_tokens": 0,
            "user_tokens": 0,
            "tool_tokens": 0,
            "history_messages_kept": 0,
            "history_dropped_count": 0,
            "summary_active": False,
            "summary_messages": 0,
            # Compaction state.
            "compaction_triggered": False,
            "compactions_total": 0,
            # Phase 1c: time-to-first-stream-delta + filler injection.
            "first_token_ms": 0.0,
            "filler_emitted": False,
            # P1 (perf backlog): per-turn embed budget. Surfaced via
            # ``get_last_response_detail`` so MCP can grep regressions
            # over time. Zero on the idle frame.
            "embed_calls": 0,
            "embed_ms": 0.0,
            # P2 (perf backlog): prompt-build phase telemetry. Per-
            # provider wall time so a regression in a single provider
            # can be attributed without instrumenting it by hand.
            "provider_ms": {},
            "rag_lookup_ms": 0.0,
            "assemble_ms": 0.0,
        }

    def get_last_metrics(self) -> dict[str, Any]:
        return dict(self._last_metrics)

    def get_average_metrics(self) -> dict[str, float | str | int]:
        if not self._metrics_history:
            return {
                "window": 0,
                "capture_ms": 0.0, "stt_ms": 0.0, "llm_ms": 0.0,
                "tts_ms": 0.0, "total_ms": 0.0,
                "prompt_tokens": 0.0, "completion_tokens": 0.0,
                "tokens_per_second": 0.0, "prompt_pct": 0.0,
            }

        def avg(key: str) -> float:
            values = [float(item.get(key, 0.0) or 0.0) for item in self._metrics_history]
            return round(sum(values) / max(1, len(values)), 1)

        return {
            "window": len(self._metrics_history),
            "capture_ms": avg("capture_ms"),
            "stt_ms": avg("stt_ms"),
            "llm_ms": avg("llm_ms"),
            "tts_ms": avg("tts_ms"),
            "total_ms": avg("total_ms"),
            "prompt_tokens": avg("prompt_tokens"),
            "completion_tokens": avg("completion_tokens"),
            "tokens_per_second": avg("tokens_per_second"),
            "prompt_pct": round(avg("prompt_pct"), 4),
        }

    def reset_latency_metrics(self) -> None:
        self._last_metrics = self._zero_metrics()
        self._metrics_history.clear()
