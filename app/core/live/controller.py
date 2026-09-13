"""Live policy controller. Propose, arbitrate, execute nonverbal + micros."""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import replace
from typing import Any, Callable

from app.core.infra.settings import _DEFAULT_LIVE_POLICY_MAX_TOKENS
from app.core.live.arbiter import (
    NONVERBAL_OK_EMPTY_URGE,
    SPEECH_INTENTS,
    LiveArbiterResult,
    arbitrate_live_proposal,
)
from app.core.live.epochs import classify_epoch
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.main_wake import (
    admit_main_wake,
    lookup_urge,
)
from app.core.live.policy_context import situation_summary
from app.core.live.micro_utterance import (
    MICRO_INTENTS,
    proposed_micro_text,
    validate_micro_utterance,
)
from app.core.live.prompt import LivePolicyPrompt, LivePolicyPromptAssembler
from app.core.live.proposal import LIVE_POLICY_JSON_SCHEMA, LivePolicyProposal

log = logging.getLogger("app.session")
live_log = logging.getLogger("app.live")

USER_INTENT_KINDS = frozenset({"user.message_sent", "user.speech_final"})
_BUDGET_EXEMPT_KINDS = USER_INTENT_KINDS | frozenset({
    "user.typing_started",
    "user.speech_started",
    "user.voice_start",
})
_KEEP_ALIVE_ON = "30m"
_DEFAULT_WAIT_MS = 15_000
_DEFAULT_WAKE_ON = (
    "user.message_sent",
    "user.speech_final",
    "user.voice_start",
    "user.typing_started",
)
INTENT_BUDGET = {
    "attend": "attention_switch",
    "acknowledge_user": "expression",
    "react_affectively": "expression",
}


def _floor_busy(frame: LiveSituationFrame) -> bool:
    interaction = getattr(frame, "interaction", None)
    if interaction is None:
        return False
    return bool(
        getattr(interaction, "turn_active", False)
        or getattr(interaction, "tts_active", False)
    )


class LivePolicyController:
    """One in-flight 4B call. Late proposals do not execute."""

    def __init__(
        self,
        *,
        client_provider: Callable[[], Any] | None = None,
        model_provider: Callable[[], str] | None = None,
        route_provider: Callable[[], Any] | None = None,
        prompt_ceiling_provider: Callable[[], int] | None = None,
        prompt_builder: Callable[..., LivePolicyPrompt] | None = None,
        generation_provider: Callable[[], int] | None = None,
        ledger_recorder: Callable[..., None] | None = None,
        inclination_provider: Callable[[], Any] | None = None,
        on_executed: Callable[[LiveSituationFrame], None] | None = None,
        on_micro_utterance: Callable[..., bool] | None = None,
        on_main_wake: Callable[..., bool] | None = None,
        unprompted_speech_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._client_provider = client_provider
        self._model_provider = model_provider
        self._route_provider = route_provider
        self._prompt_ceiling_provider = prompt_ceiling_provider
        self._prompt_builder = prompt_builder
        self._generation_provider = generation_provider
        self._ledger_recorder = ledger_recorder
        self._inclination_provider = inclination_provider
        self._on_executed = on_executed
        self._on_micro_utterance = on_micro_utterance
        self._on_main_wake = on_main_wake
        self._unprompted_speech_provider = unprompted_speech_provider
        self._last_micro_ms = 0.0
        self._assembler = LivePolicyPromptAssembler()
        self._lock = threading.Lock()
        self._inflight = False
        self._cancel = threading.Event()
        self._loaded = False
        self.last_proposal: dict[str, Any] = {}
        self.last_user_intent_admit: dict[str, Any] = {}
        self.last_accepted_nonverbal: dict[str, Any] = {}
        self.proactive_enqueued = 0
        self._arbiter_reason_counts: Counter[str] = Counter()
        self._executed_counts: Counter[str] = Counter()
        self._main_wake_proposed = 0
        self._main_wake_admitted = 0
        self._main_wake_rejected = 0
        self._main_wake_silence = 0
        self._main_wake_reject_reasons: Counter[str] = Counter()
        self._main_wake_decided_generation: int | None = None
        self._main_wake_last_admit_generation: int | None = None
        self._main_wake_last_reject_generation: int | None = None

    def consider(
        self,
        frame: LiveSituationFrame,
        *,
        trigger_kind: str,
        prompt_input: dict[str, Any] | None = None,
        user_intent: bool | None = None,
    ) -> None:
        kind = str(trigger_kind or "")
        intent = bool(user_intent) if user_intent is not None else kind in USER_INTENT_KINDS
        if intent:
            self._admit_critical(frame)
        epoch = classify_epoch(kind)
        if epoch == "data_only":
            live_log.debug(
                "live consider skipped: trigger=%s epoch=data_only generation=%s",
                kind,
                int(getattr(frame, "generation", 0) or 0),
            )
            return
        if self._wait_holding(kind, frame):
            return
        self._spawn(frame, trigger_kind=kind, prompt_input=prompt_input or {}, user_intent=intent)

    def accepted_nonverbal_for(self, generation: int) -> dict[str, Any] | None:
        held = self.last_accepted_nonverbal
        if not held:
            return None
        if int(held.get("generation", -1)) != int(generation):
            return None
        until = float(held.get("hold_until_ms") or 0.0)
        if until and time.monotonic() * 1000.0 >= until:
            return None
        return dict(held)

    def cancel(self) -> None:
        self._cancel.set()

    def warm(self) -> None:
        self._set_keep_alive(_KEEP_ALIVE_ON)
        self._loaded = True

    def unload(self) -> None:
        self.cancel()
        self._set_keep_alive(0)
        self._loaded = False
        self.last_accepted_nonverbal = {}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "last_policy_proposal": dict(self.last_proposal),
            "last_user_intent_admit": dict(self.last_user_intent_admit),
            "last_accepted_nonverbal": dict(self.last_accepted_nonverbal),
            "live_policy_loaded": bool(self._loaded),
            "live_policy_inflight": bool(self._inflight),
            "proactive_enqueued": int(self.proactive_enqueued),
            "arbiter_reason_counts": dict(self._arbiter_reason_counts),
            "executed_counts": dict(self._executed_counts),
            "main_wake_proposed": int(self._main_wake_proposed),
            "main_wake_admitted": int(self._main_wake_admitted),
            "main_wake_rejected": int(self._main_wake_rejected),
            "main_wake_silence": int(self._main_wake_silence),
            "main_wake_reject_reasons": dict(self._main_wake_reject_reasons),
            "main_wake_last_admit_generation": self._main_wake_last_admit_generation,
            "main_wake_last_reject_generation": self._main_wake_last_reject_generation,
        }

    def _admit_critical(self, frame: LiveSituationFrame) -> None:
        proposal = LivePolicyProposal(
            snapshot_generation=int(frame.generation),
            selected_urge_id="",
            intent="request_main_speech",
            arguments={"source": "critical_user_intent"},
            reason_code="critical_user_intent",
            context_refs=(),
        )
        result = LiveArbiterResult(True, "critical_user_intent", proposal)
        self._store_result(
            proposal,
            result,
            elapsed_ms=0.0,
            prompt_tokens=0,
            region_tokens={},
            shadow=True,
            user_intent=True,
            slot="admit",
        )

    def _wait_holding(self, trigger_kind: str, frame: LiveSituationFrame) -> bool:
        runtime = self._inclination()
        if runtime is None:
            return False
        now_ms = time.monotonic() * 1000.0
        try:
            return bool(
                runtime.wait.should_suppress_inference(
                    trigger_kind,
                    now_mono_ms=now_ms,
                    generation=int(frame.generation),
                )
            )
        except Exception:
            log.debug("live wait gate failed", exc_info=True)
            return False

    def _spawn(
        self,
        frame: LiveSituationFrame,
        *,
        trigger_kind: str,
        prompt_input: dict[str, Any],
        user_intent: bool,
    ) -> None:
        with self._lock:
            if self._inflight:
                return
            self._inflight = True
            self._cancel = threading.Event()
            gen = self._current_generation()
            cancel = self._cancel
        captured = dict(prompt_input)

        def _run() -> None:
            try:
                if cancel.is_set():
                    return
                self._infer(
                    frame,
                    trigger_kind=trigger_kind,
                    prompt_input=captured,
                    user_intent=user_intent,
                    started_generation=gen,
                    cancel=cancel,
                )
            except Exception:
                log.debug("live policy inference failed", exc_info=True)
            finally:
                with self._lock:
                    self._inflight = False

        threading.Thread(target=_run, name="live-policy", daemon=True).start()
        inferred = ""
        try:
            inferred = str(getattr(frame.shared.inferred, "label", "") or "")
        except Exception:
            inferred = ""
        live_log.info(
            "live consider: trigger=%s epoch=%s speech_budget=%s "
            "inferred=%s generation=%s",
            trigger_kind,
            classify_epoch(trigger_kind),
            str(frame.constraints.speech_budget or ""),
            inferred or "-",
            int(frame.generation),
        )

    def _infer(
        self,
        frame: LiveSituationFrame,
        *,
        trigger_kind: str,
        prompt_input: dict[str, Any],
        user_intent: bool,
        started_generation: int,
        cancel: threading.Event,
        client: Any | None = None,
    ) -> LiveArbiterResult | None:
        """Run one inference. Tests may call this on the calling thread."""
        if cancel.is_set() or started_generation != self._current_generation():
            return LiveArbiterResult(False, "cancelled")
        client = client if client is not None else self._client()
        if client is None:
            return LiveArbiterResult(False, "no_client")
        route = self._route()
        context_window = int(getattr(route, "context_window", 0) or 40960)
        max_tokens = int(
            getattr(route, "max_tokens", 0) or _DEFAULT_LIVE_POLICY_MAX_TOKENS
        )
        temperature = getattr(route, "temperature", 0.0)
        if temperature is None:
            temperature = 0.0
        ceiling = 12000
        if callable(self._prompt_ceiling_provider):
            try:
                ceiling = int(self._prompt_ceiling_provider() or 12000)
            except Exception:
                ceiling = 12000
        assemble_kwargs = {
            key: value
            for key, value in prompt_input.items()
            if key in {
                "concepts",
                "transcript_rows",
                "impulses",
                "journal_entries",
                "ledger",
                "urges",
                "diet_tuning",
            }
        }
        if callable(self._prompt_builder):
            prompt = self._prompt_builder(
                frame=frame,
                context_window=context_window,
                max_tokens=max_tokens,
                prompt_ceiling=ceiling,
                **assemble_kwargs,
            )
        else:
            prompt = self._assembler.assemble(
                frame=frame,
                context_window=context_window,
                max_tokens=max_tokens,
                prompt_ceiling=ceiling,
                **assemble_kwargs,
            )
        model = self._model()
        options: dict[str, object] = {
            "temperature": float(temperature),
            "num_predict": max_tokens,
            "num_ctx": context_window,
        }
        t0 = time.monotonic()
        raw, usage = client.chat_json(
            prompt.messages,
            model=model,
            options=options,
            format_json=True,
            json_schema=LIVE_POLICY_JSON_SCHEMA,
            think=False,
            keep_alive=_KEEP_ALIVE_ON,
            surface="live_policy",
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        if cancel.is_set() or started_generation != self._current_generation():
            result = LiveArbiterResult(False, "cancelled")
            self._store_result(
                None, result, elapsed_ms, prompt_tokens, prompt.region_tokens,
                shadow=False, user_intent=user_intent,
            )
            return result
        try:
            proposal = LivePolicyProposal.model_validate(raw)
        except Exception as exc:
            result = LiveArbiterResult(False, "invalid_json")
            preview = str(raw or "").replace("\n", " ")[:240]
            log.info(
                "live policy proposal invalid: elapsed_ms=%.0f "
                "prompt_tokens=%s preview=%s err=%s",
                elapsed_ms,
                prompt_tokens,
                preview,
                exc,
            )
            self._store_result(
                None, result, elapsed_ms, prompt_tokens, prompt.region_tokens,
                shadow=False, user_intent=user_intent,
                extra={"parse_error": str(exc), "raw_preview": preview},
            )
            return result
        proposal = replace(proposal, snapshot_generation=int(frame.generation))
        result = arbitrate_live_proposal(
            proposal,
            snapshot_generation=int(frame.generation),
            known_urge_ids=prompt_input.get("known_urge_ids") or (),
            known_context_refs=prompt_input.get("known_context_refs") or (),
            user_intent=user_intent,
        )
        extra: dict[str, Any] = {}
        executed = False
        if (
            result.accepted
            and not cancel.is_set()
            and started_generation == self._current_generation()
        ):
            executed, extra = self._maybe_execute(
                proposal,
                frame,
                trigger_kind=trigger_kind,
                user_intent=user_intent,
            )
        live_log.info(
            "live policy proposal: intent=%s reason_code=%s "
            "arbiter_reason=%s accepted=%s executed=%s talk_about=%s "
            "elapsed_ms=%.0f prompt_tokens=%s generation=%s trigger=%s "
            "speech_budget=%s",
            proposal.intent,
            proposal.reason_code,
            result.reason,
            result.accepted,
            executed,
            result.talk_about,
            elapsed_ms,
            prompt_tokens,
            frame.generation,
            trigger_kind,
            str(frame.constraints.speech_budget or ""),
        )
        spoken = bool(extra.get("micro_spoken"))
        admitted = bool(extra.get("main_wake_admitted"))
        self._store_result(
            proposal, result, elapsed_ms, prompt_tokens, prompt.region_tokens,
            shadow=(
                (
                    proposal.intent == "request_main_speech"
                    and not admitted
                )
                or (
                    proposal.intent in SPEECH_INTENTS
                    and proposal.intent != "request_main_speech"
                    and not spoken
                )
            ),
            user_intent=user_intent,
            extra=extra,
            executed=executed,
        )
        return result

    def _maybe_execute(
        self,
        proposal: LivePolicyProposal,
        frame: LiveSituationFrame,
        *,
        trigger_kind: str,
        user_intent: bool,
    ) -> tuple[bool, dict[str, Any]]:
        extra: dict[str, Any] = {}
        intent = proposal.intent
        if intent == "request_main_speech":
            return self._execute_main_wake(
                proposal, frame, extra=extra, user_intent=user_intent,
            )
        now_ms = time.monotonic() * 1000.0
        target = str(frame.attention.target or "none")
        ttl = int(
            proposal.reconsider_after_ms
            if proposal.reconsider_after_ms is not None
            else (frame.temporal.min_hold_remaining_ms or _DEFAULT_WAIT_MS)
        )
        if intent == "backchannel_user":
            return self._execute_micro(
                proposal, frame, extra=extra, now_ms=now_ms,
                ttl_ms=ttl, target=target, hold=True,
                user_intent=user_intent, trigger_kind=trigger_kind,
            )
        if intent not in NONVERBAL_OK_EMPTY_URGE:
            extra["budget_class"] = ""
            return False, extra
        if self._repeat_holding(intent, target, now_ms, int(frame.generation)):
            extra["repeat_suppressed"] = True
            extra["budget_class"] = ""
            self._schedule_wait(
                frame,
                proposal,
                now_ms=now_ms,
                ttl_ms=ttl,
                reason_code="repeat_suppressed",
            )
            log.info(
                "live policy repeat_suppressed: intent=%s target=%s generation=%s",
                intent,
                target,
                frame.generation,
            )
            return False, extra
        if intent in {"wait", "noop"}:
            extra["budget_class"] = ""
            self._schedule_wait(
                frame,
                proposal,
                now_ms=now_ms,
                ttl_ms=ttl,
                reason_code=proposal.reason_code or intent,
            )
            if _floor_busy(frame):
                extra["overlay_skipped"] = "turn_or_tts"
                log.info(
                    "live policy overlay skipped: reason=turn_or_tts intent=%s",
                    intent,
                )
                return True, extra
            self._hold_nonverbal(frame, intent, target, ttl, now_ms, "")
            self._notify_executed(frame)
            return True, extra
        exempt = bool(user_intent) or trigger_kind in _BUDGET_EXEMPT_KINDS
        budget_class = INTENT_BUDGET.get(intent, "")
        extra["budget_class"] = budget_class
        if budget_class and not exempt:
            runtime = self._inclination()
            if runtime is not None:
                try:
                    spent = runtime.budget.consume(
                        budget_class, now_mono_ms=now_ms,
                    )
                except Exception:
                    log.debug("live budget consume failed", exc_info=True)
                    spent = True
                if not spent:
                    extra["budget_exhausted"] = True
                    replenish = 0
                    try:
                        replenish = int(
                            runtime.budget.snapshot(now_ms).next_replenish_ms or 0
                        )
                    except Exception:
                        replenish = 0
                    self._schedule_wait(
                        frame,
                        proposal,
                        now_ms=now_ms,
                        ttl_ms=max(_DEFAULT_WAIT_MS, replenish),
                        reason_code="budget_exhausted",
                    )
                    log.info(
                        "live policy budget exhausted: class=%s intent=%s",
                        budget_class,
                        intent,
                    )
                    return False, extra
        self._hold_nonverbal(frame, intent, target, ttl, now_ms, budget_class)
        self._notify_executed(frame)
        if intent in MICRO_INTENTS:
            self._execute_micro(
                proposal, frame, extra=extra, now_ms=now_ms,
                ttl_ms=ttl, target=target, hold=False,
                user_intent=user_intent, trigger_kind=trigger_kind,
            )
        return True, extra

    def _execute_main_wake(
        self,
        proposal: LivePolicyProposal,
        frame: LiveSituationFrame,
        *,
        extra: dict[str, Any],
        user_intent: bool,
    ) -> tuple[bool, dict[str, Any]]:
        extra["budget_class"] = "main_wake"
        self._main_wake_proposed += 1
        now_ms = time.monotonic() * 1000.0
        runtime = self._inclination()
        urges = ()
        remaining = 0
        if runtime is not None:
            try:
                urges = runtime.urges.active()
            except Exception:
                urges = ()
            try:
                remaining = int(
                    runtime.budget.remaining("main_wake", now_mono_ms=now_ms)
                )
            except Exception:
                remaining = 0
        reason = admit_main_wake(
            intent=proposal.intent,
            user_intent=user_intent,
            selected_urge_id=str(proposal.selected_urge_id or ""),
            urges=urges,
            frame=frame,
            decided_generation=self._main_wake_decided_generation,
            now_mono_ms=now_ms,
            budget_remaining=remaining,
            unprompted_speech=self._unprompted_speech_enabled(),
        )
        extra["main_wake_reason"] = reason or "admitted"
        if reason:
            self.note_main_wake_reject(reason, int(frame.generation))
            extra["main_wake_rejected"] = reason
            return False, extra
        spent = True
        if runtime is not None:
            try:
                spent = bool(
                    runtime.budget.consume("main_wake", now_mono_ms=now_ms)
                )
            except Exception:
                log.debug("live main-wake budget consume failed", exc_info=True)
                spent = True
        if not spent:
            extra["main_wake_reason"] = "budget_exhausted"
            extra["main_wake_rejected"] = "budget_exhausted"
            self.note_main_wake_reject("budget_exhausted", int(frame.generation))
            return False, extra
        urge = lookup_urge(urges, str(proposal.selected_urge_id or ""))
        concept_ids: list[int] = []
        if urge is not None:
            concept_ids.extend(int(cid) for cid in urge.concept_ids if cid)
        for ref in proposal.context_refs or ():
            token = str(ref or "")
            if token.startswith("concept:"):
                try:
                    concept_ids.append(int(token.split(":", 1)[1]))
                except ValueError:
                    continue
        unique_ids = tuple(dict.fromkeys(concept_ids))
        payload = {
            "generation": int(frame.generation),
            "urge_id": str(proposal.selected_urge_id or ""),
            "urge_kind": str(getattr(urge, "kind", "") or ""),
            "reason_code": str(proposal.reason_code or ""),
            "intent": proposal.intent,
            "situation_summary": situation_summary(frame),
            "concept_ids": unique_ids,
        }
        extra["main_wake_payload"] = dict(payload)
        extra["main_wake_payload"]["concept_ids"] = list(unique_ids)
        enqueued = False
        callback = self._on_main_wake
        if callable(callback):
            try:
                enqueued = bool(callback(payload))
            except Exception:
                log.debug("live main-wake enqueue failed", exc_info=True)
                enqueued = False
        self._mark_main_wake_admitted(int(frame.generation))
        extra["main_wake_admitted"] = True
        extra["main_wake_enqueued"] = bool(enqueued)
        if enqueued:
            self.proactive_enqueued += 1
        live_log.info(
            "live main-wake admitted: reason_code=%s generation=%s "
            "enqueued=%s",
            proposal.reason_code,
            frame.generation,
            enqueued,
        )
        return True, extra

    def note_main_wake_reject(self, reason: str, generation: int) -> None:
        self._main_wake_rejected += 1
        self._main_wake_reject_reasons[str(reason or "")] += 1
        self._main_wake_last_reject_generation = int(generation)
        self._main_wake_decided_generation = int(generation)

    def note_main_wake_silence(self, generation: int) -> None:
        self._main_wake_silence += 1
        self._main_wake_decided_generation = int(generation)

    def _mark_main_wake_admitted(self, generation: int) -> None:
        self._main_wake_admitted += 1
        self._main_wake_last_admit_generation = int(generation)
        self._main_wake_decided_generation = int(generation)

    def _execute_micro(
        self,
        proposal: LivePolicyProposal,
        frame: LiveSituationFrame,
        *,
        extra: dict[str, Any],
        now_ms: float,
        ttl_ms: int,
        target: str,
        hold: bool,
        user_intent: bool,
        trigger_kind: str,
    ) -> tuple[bool, dict[str, Any]]:
        extra.setdefault("budget_class", "micro_speech")
        raw = proposed_micro_text(proposal.arguments)
        text = validate_micro_utterance(raw)
        if text is None:
            extra["micro_skipped"] = "invalid" if raw.strip() else "missing_text"
            return False, extra
        block = self._micro_block_reason(
            proposal, frame, now_ms=now_ms,
            user_intent=user_intent, trigger_kind=trigger_kind,
        )
        if block:
            extra["micro_skipped"] = block
            return False, extra
        runtime = self._inclination()
        if runtime is not None:
            try:
                spent = runtime.budget.consume(
                    "micro_speech", now_mono_ms=now_ms,
                )
            except Exception:
                log.debug("live micro budget consume failed", exc_info=True)
                spent = True
            if not spent:
                extra["budget_exhausted"] = True
                extra["micro_skipped"] = "budget_exhausted"
                replenish = 0
                try:
                    replenish = int(
                        runtime.budget.snapshot(now_ms).next_replenish_ms or 0
                    )
                except Exception:
                    replenish = 0
                self._schedule_wait(
                    frame,
                    proposal,
                    now_ms=now_ms,
                    ttl_ms=max(_DEFAULT_WAIT_MS, replenish),
                    reason_code="budget_exhausted",
                )
                return False, extra
        callback = self._on_micro_utterance
        delivered = False
        if callable(callback):
            try:
                delivered = bool(callback(text, proposal, frame))
            except Exception:
                log.debug("live micro deliver failed", exc_info=True)
                delivered = False
        if not delivered:
            extra["micro_skipped"] = "delivery_failed"
            return False, extra
        extra["micro_spoken"] = True
        extra["micro_words"] = len(text.split())
        self._last_micro_ms = now_ms
        if hold:
            self._hold_nonverbal(frame, proposal.intent, target, ttl_ms, now_ms, "micro_speech")
            self._notify_executed(frame)
        live_log.info(
            "live micro: reason_code=%s chars=%s generation=%s",
            str(proposal.reason_code or ""),
            len(text),
            frame.generation,
        )
        return True, extra

    def _micro_block_reason(
        self,
        proposal: LivePolicyProposal,
        frame: LiveSituationFrame,
        *,
        now_ms: float,
        user_intent: bool,
        trigger_kind: str,
    ) -> str:
        if user_intent or trigger_kind in USER_INTENT_KINDS:
            return "user_turn"
        if not self._unprompted_speech_enabled():
            return "speech_forbidden"
        constraints = frame.constraints
        if str(constraints.speech_budget or "") == "forbidden":
            return "speech_forbidden"
        if str(constraints.sleep_status or "") in SLEEP_SPEECH_FORBID:
            return "sleep"
        if bool(constraints.dnd):
            return "dnd"
        if _floor_busy(frame):
            return "turn_or_tts"
        floor = str(frame.interaction.floor_owner or "")
        if floor in {"user", "aiko", "transition"}:
            return "floor_busy"
        if frame.interaction.speech_active or frame.interaction.typing_active:
            return "user_active"
        if (
            str(constraints.speech_budget or "") == "rare"
            and not proposal.selected_urge_id
        ):
            return "speech_rare"
        gap = int(constraints.min_gap_after_speech_ms or 0)
        if gap > 0:
            since = int(frame.interaction.silence_since_aiko_speech_ms or 0)
            if since <= 0 and self._last_micro_ms > 0:
                since = int(now_ms - self._last_micro_ms)
            if 0 < since < gap:
                return "speech_gap"
        return ""

    def _repeat_holding(
        self,
        intent: str,
        target: str,
        now_ms: float,
        generation: int,
    ) -> bool:
        held = self.last_accepted_nonverbal
        if not held:
            return False
        if int(held.get("generation", -1)) != int(generation):
            return False
        if held.get("intent") != intent:
            return False
        if str(held.get("attention_target") or "none") != target:
            return False
        until = float(held.get("hold_until_ms") or 0.0)
        return bool(until) and now_ms < until

    def _hold_nonverbal(
        self,
        frame: LiveSituationFrame,
        intent: str,
        target: str,
        ttl_ms: int,
        now_ms: float,
        budget_class: str,
    ) -> None:
        ttl = max(0, int(ttl_ms))
        self.last_accepted_nonverbal = {
            "generation": int(frame.generation),
            "intent": intent,
            "attention_target": target,
            "ttl_ms": ttl,
            "hold_until_ms": now_ms + ttl,
            "budget_class": budget_class,
        }

    def _schedule_wait(
        self,
        frame: LiveSituationFrame,
        proposal: LivePolicyProposal,
        *,
        now_ms: float,
        ttl_ms: int,
        reason_code: str,
    ) -> None:
        runtime = self._inclination()
        if runtime is None:
            return
        wake_on = proposal.wake_on or _DEFAULT_WAKE_ON
        try:
            runtime.wait.schedule(
                generation=int(frame.generation),
                urge_id=proposal.selected_urge_id,
                reconsider_after_ms=max(1, int(ttl_ms or _DEFAULT_WAIT_MS)),
                now_mono_ms=now_ms,
                wake_on=wake_on,
                reason_code=reason_code,
            )
        except Exception:
            log.debug("live wait schedule failed", exc_info=True)

    def _notify_executed(self, frame: LiveSituationFrame) -> None:
        callback = self._on_executed
        if not callable(callback):
            return
        try:
            callback(frame)
        except Exception:
            log.debug("live nonverbal execute notify failed", exc_info=True)

    def _store_result(
        self,
        proposal: LivePolicyProposal | None,
        result: LiveArbiterResult,
        elapsed_ms: float,
        prompt_tokens: int,
        region_tokens: dict[str, int],
        *,
        shadow: bool,
        user_intent: bool,
        extra: dict[str, Any] | None = None,
        slot: str = "proposal",
        executed: bool = False,
    ) -> None:
        payload = {
            "proposal": proposal.to_payload() if proposal is not None else None,
            "arbiter": result.to_payload(),
            "elapsed_ms": round(float(elapsed_ms), 1),
            "prompt_tokens": int(prompt_tokens),
            "region_tokens": dict(region_tokens),
            "shadow": bool(shadow),
            "user_intent": bool(user_intent),
            "executed": bool(executed),
        }
        if extra:
            payload.update(extra)
        if slot == "admit":
            self.last_user_intent_admit = payload
        else:
            self.last_proposal = payload
            self._arbiter_reason_counts[str(result.reason or "")] += 1
            if executed and proposal is not None:
                self._executed_counts[proposal.intent] += 1
        recorder = self._ledger_recorder
        used_main = bool(
            proposal is not None
            and proposal.intent == "request_main_speech"
            and (user_intent or payload.get("main_wake_admitted"))
        )
        if callable(recorder):
            try:
                recorder(
                    proposed_action=(
                        proposal.intent if proposal is not None else ""
                    ),
                    arbiter_result=result.reason,
                    main_model_used=used_main,
                    executed=bool(executed),
                )
            except TypeError:
                try:
                    recorder(
                        proposed_action=(
                            proposal.intent if proposal is not None else ""
                        ),
                        arbiter_result=result.reason,
                        main_model_used=used_main,
                    )
                except Exception:
                    log.debug("live policy ledger record failed", exc_info=True)
            except Exception:
                log.debug("live policy ledger record failed", exc_info=True)

    def _unprompted_speech_enabled(self) -> bool:
        provider = self._unprompted_speech_provider
        if not callable(provider):
            return True
        try:
            return bool(provider())
        except Exception:
            return True

    def _inclination(self) -> Any:
        provider = self._inclination_provider
        if not callable(provider):
            return None
        try:
            return provider()
        except Exception:
            return None

    def _current_generation(self) -> int:
        provider = self._generation_provider
        if not callable(provider):
            return 0
        try:
            return int(provider() or 0)
        except Exception:
            return 0

    def _client(self) -> Any:
        provider = self._client_provider
        if not callable(provider):
            return None
        try:
            return provider()
        except Exception:
            return None

    def _model(self) -> str:
        provider = self._model_provider
        if not callable(provider):
            return "qwen3.5:4b"
        try:
            return str(provider() or "").strip() or "qwen3.5:4b"
        except Exception:
            return "qwen3.5:4b"

    def _route(self) -> Any:
        provider = self._route_provider
        if not callable(provider):
            return None
        try:
            return provider()
        except Exception:
            return None

    def _set_keep_alive(self, keep_alive: str | int) -> None:
        client = self._client()
        inner = getattr(client, "_inner", client)
        fn = getattr(inner, "set_model_keep_alive", None)
        if not callable(fn):
            return
        try:
            fn(self._model(), keep_alive)
        except Exception:
            log.debug("live policy keep_alive failed", exc_info=True)


__all__ = ["LivePolicyController", "USER_INTENT_KINDS", "INTENT_BUDGET"]
