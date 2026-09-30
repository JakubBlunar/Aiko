"""Live policy controller. Propose, arbitrate, execute nonverbal + micros."""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import replace
from typing import Any, Callable

from app.core.infra.settings import _DEFAULT_LIVE_POLICY_MAX_TOKENS
from app.core.live.actions import (
    LiveActionRecord,
    action_result_kind,
    advance_action,
    start_action,
)
from app.core.live.admission import (
    LiveAdmissionRecord,
    build_admission_record,
    cancelled_record,
    parse_error_record,
)
from app.core.live.arbiter import (
    NONVERBAL_OK_EMPTY_URGE,
    SPEECH_INTENTS,
    LiveArbiterResult,
    arbitrate_live_proposal,
)
from app.core.live.epochs import classify_epoch
from app.core.live.diagnostics import LiveDecisionTrace, code_digest
from app.core.live.fallback import (
    classify_execute_failure,
    legal_fallbacks,
    should_arm_fallback,
)
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.main_wake import (
    admit_main_wake,
    capped_urge_subject,
    lookup_urge,
    speech_act_from_proposal,
)
from app.core.live.policy_context import situation_summary
from app.core.live.presence import (
    STYLE_SPEECH_CAP,
    clamp_presence_style,
    hold_flags_from_proposal,
    presence_from_proposal,
    quieter_speech,
    user_interrupting,
)
from app.core.live.reaction import reaction_from_proposal
from app.core.live.vitality_posture import (
    apply_vitality_posture,
    vitality_from_proposal,
)
from app.core.live.micro_utterance import (
    MICRO_INTENTS,
    proposed_micro_text,
    validate_micro_utterance,
)
from app.core.live.prompt import (
    LIVE_POLICY_PROMPT_VERSION,
    _INSTRUCTIONS,
    LivePolicyPrompt,
    LivePolicyPromptAssembler,
)
from app.core.live.proposal import LIVE_POLICY_JSON_SCHEMA, LivePolicyProposal
from app.core.live.urge_menu import build_urge_menu
from app.core.live.wait import (
    DEFAULT_WAIT_MS,
    MAX_WAIT_MS,
    MIN_WAIT_MS,
    expand_wake_set,
    wait_horizon_from_proposal,
    wait_ms_for_horizon,
    wake_set_from_proposal,
)

log = logging.getLogger("app.session")
live_log = logging.getLogger("app.live")

USER_INTENT_KINDS = frozenset({"user.message_sent", "user.speech_final"})
_BUDGET_EXEMPT_KINDS = USER_INTENT_KINDS | frozenset({
    "user.typing_started",
    "user.voice_start",
})
_KEEP_ALIVE_ON = "30m"
_DEFAULT_WAIT_MS = DEFAULT_WAIT_MS
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
        on_action_result: Callable[[LiveActionRecord], None] | None = None,
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
        self._on_action_result = on_action_result
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
        self.last_action: LiveActionRecord | None = None
        self.last_admission: LiveAdmissionRecord | None = None
        self._current_action: LiveActionRecord | None = None
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
        self._pending_fallback: dict[str, Any] | None = None
        self._attempted_fallback_ids: set[str] = set()
        self._policy_failure_count = 0
        self._last_policy_failure: dict[str, Any] | None = None
        self._decision_trace = LiveDecisionTrace(
            code_sha256=code_digest(
                type(self)._infer_once, type(self)._maybe_execute,
                LivePolicyPromptAssembler.assemble,
                LivePolicyPromptAssembler._render_situation,
                LivePolicyPromptAssembler._render_tail, _INSTRUCTIONS,
            ),
            prompt_version=LIVE_POLICY_PROMPT_VERSION,
        )

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
            self._cancel_fallback()
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
        self._current_action = None

    def unload(self) -> None:
        self.cancel()
        self._set_keep_alive(0)
        self._loaded = False
        self.last_accepted_nonverbal = {}
        self._current_action = None
        self._pending_fallback = None
        self._attempted_fallback_ids.clear()
        self.last_admission = None

    def warm(self) -> None:
        self._set_keep_alive(_KEEP_ALIVE_ON)
        self._loaded = True

    def diagnostics(self) -> dict[str, Any]:
        return {
            "decision_trace": self._decision_trace.snapshot(),
            "live_policy_warm_requested": bool(self._loaded),
            "last_policy_proposal": dict(self.last_proposal),
            "last_user_intent_admit": dict(self.last_user_intent_admit),
            "last_accepted_nonverbal": dict(self.last_accepted_nonverbal),
            "last_action": (
                self.last_action.to_payload()
                if self.last_action is not None
                else None
            ),
            "last_admission": (
                self.last_admission.to_payload()
                if self.last_admission is not None
                else None
            ),
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
            "pending_fallback": (
                dict(self._pending_fallback) if self._pending_fallback else None
            ),
            "policy_failure_count": self._policy_failure_count,
            "last_policy_failure": (
                dict(self._last_policy_failure) if self._last_policy_failure else None
            ),
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
                result = self._infer(
                    frame,
                    trigger_kind=trigger_kind,
                    prompt_input=captured,
                    user_intent=user_intent,
                    started_generation=gen,
                    cancel=cancel,
                )
                if result is not None and result.reason == "no_client":
                    self._record_policy_failure("no_client", gen, trigger_kind)
            except Exception:
                self._record_policy_failure("model_failure", gen, trigger_kind)
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

    def _record_policy_failure(self, reason: str, generation: int, trigger_kind: str) -> None:
        with self._lock:
            self._policy_failure_count += 1
            self._last_policy_failure = {
                "reason": reason,
                "generation": generation,
                "trigger_kind": trigger_kind,
            }

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
        trace = self._decision_trace.begin(
            generation=int(frame.generation), trigger=trigger_kind,
        )
        trace["speech_budget"] = frame.constraints.speech_budget
        trace["floor_owner"] = frame.interaction.floor_owner
        trace["turn_active"] = frame.interaction.turn_active
        trace["typing_active"] = frame.interaction.typing_active
        started = time.monotonic()
        try:
            result = self._infer_once(
                frame, trigger_kind=trigger_kind, prompt_input=prompt_input,
                user_intent=user_intent, started_generation=started_generation,
                cancel=cancel, client=client, trace=trace,
            )
            trace["reason"] = result.reason if result is not None else "no_result"
            return result
        finally:
            trace["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            self._decision_trace.finish(trace)

    def _infer_once(
        self,
        frame: LiveSituationFrame,
        *,
        trigger_kind: str,
        prompt_input: dict[str, Any],
        user_intent: bool,
        started_generation: int,
        cancel: threading.Event,
        client: Any | None,
        trace: dict[str, Any],
    ) -> LiveArbiterResult | None:
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
        if self._pending_fallback:
            assemble_kwargs["fallback"] = dict(self._pending_fallback)
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
                trigger_kind=trigger_kind,
                context_window=context_window,
                max_tokens=max_tokens,
                prompt_ceiling=ceiling,
                **assemble_kwargs,
            )
        cue_ids = {
            urge.urge_id: urge.cue_id for urge in prompt_input.get("urges") or ()
        }
        trace["menu"] = None if callable(self._prompt_builder) else [
            {
                "urge_id": item.urge_id, "kind": item.kind, "purpose": item.purpose,
                "cue_id": cue_ids.get(item.urge_id),
            }
            for item in build_urge_menu(prompt_input.get("urges") or ())
        ]
        trace["prompt_regions"] = dict(prompt.region_tokens)
        runtime = self._inclination()
        eligible_ids = runtime.eligible_policy_urges(
            frame, menu_ids={item["urge_id"] for item in trace["menu"] or ()},
            now_mono_ms=time.monotonic() * 1000.0,
            unprompted_speech=self._unprompted_speech_enabled(), user_intent=user_intent,
        ) if runtime is not None else ()
        trace["eligible_urge_ids"] = list(eligible_ids)
        trace["evaluated_urge_ids"] = []
        trace["inference_submitted"] = True
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
        trace["inference_completed"] = True
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        if cancel.is_set() or started_generation != self._current_generation():
            result = LiveArbiterResult(False, "cancelled")
            extra = self._attach_admission(
                frame, None, result, {}, cancelled=True,
            )
            self._store_result(
                None, result, elapsed_ms, prompt_tokens, prompt.region_tokens,
                shadow=False, user_intent=user_intent, extra=extra,
            )
            return result
        try:
            proposal = LivePolicyProposal.model_validate(raw)
        except Exception as exc:
            result = LiveArbiterResult(False, "invalid_json")
            preview = str(raw or "").replace("\n", " ")[:240]
            log.info(
                "live policy proposal invalid: elapsed_ms=%.0f "
                "prompt_tokens=%s",
                elapsed_ms,
                prompt_tokens,
            )
            extra = self._attach_admission(
                frame, None, result,
                {"parse_error": str(exc), "raw_preview": preview},
            )
            self._store_result(
                None, result, elapsed_ms, prompt_tokens, prompt.region_tokens,
                shadow=False, user_intent=user_intent, extra=extra,
            )
            return result
        proposal = replace(proposal, snapshot_generation=int(frame.generation))
        trace["intent"] = proposal.intent
        trace["reason"] = "execution_failure"
        menu_ids = {item["urge_id"] for item in trace["menu"] or ()}
        if proposal.selected_urge_id in menu_ids:
            trace["selected_urge_id"] = proposal.selected_urge_id
        action = start_action(
            intent=proposal.intent,
            generation=int(frame.generation),
            reason=proposal.reason_code,
            urge_id=proposal.selected_urge_id,
        )
        self._current_action = action
        result = arbitrate_live_proposal(
            proposal,
            snapshot_generation=int(frame.generation),
            known_urge_ids=prompt_input.get("known_urge_ids") or (),
            known_context_refs=prompt_input.get("known_context_refs") or (),
            user_intent=user_intent,
            allowed_actions=frame.constraints.allowed_actions,
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
            trace["evaluated_urge_ids"] = list(eligible_ids)
            for urge_id in eligible_ids:
                runtime.urges.note_opportunity(
                    urge_id, defer=True, now_mono_ms=time.monotonic() * 1000.0,
                )
        action_record = self._finish_action(
            result,
            executed=executed,
            extra=extra,
            cancelled=(
                cancel.is_set()
                or started_generation != self._current_generation()
            ),
        )
        cancelled = (
            cancel.is_set()
            or started_generation != self._current_generation()
        )
        if action_record is not None:
            extra = dict(extra)
            extra["action_id"] = action_record.action_id
            extra["action_state"] = action_record.state
            self._maybe_arm_fallback(
                action_record, extra, proposal.intent, frame,
                user_intent=user_intent,
            )
        extra = self._attach_admission(
            frame, proposal, result, extra,
            executed=executed,
            cancelled=cancelled,
            action_id=str(
                extra.get("action_id")
                or getattr(action_record, "action_id", "")
                or ""
            ),
        )
        live_log.info(
            "live policy proposal: intent=%s reason_code=policy_choice "
            "arbiter_reason=%s accepted=%s executed=%s talk_about=%s "
            "elapsed_ms=%.0f prompt_tokens=%s generation=%s trigger=%s "
            "speech_budget=%s",
            proposal.intent,
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
        trace["executed"] = bool(executed)
        trace["main_wake_admitted"] = admitted
        trace["main_wake_reject_reason"] = str(extra.get("main_wake_rejected") or "")
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
        pending = self._pending_fallback
        if pending:
            extra["fallback_for"] = str(pending.get("action_id") or "")
            extra["fallback_failure"] = str(pending.get("failure") or "")
            legal = tuple(pending.get("legal") or ())
            self._consume_fallback()
            if intent not in legal:
                extra["fallback_coerced"] = "wait"
                proposal = replace(proposal, intent="wait")
                intent = "wait"
        if intent == "request_main_speech":
            return self._execute_main_wake(
                proposal, frame, extra=extra, user_intent=user_intent,
            )
        now_ms = time.monotonic() * 1000.0
        current = str(frame.attention.target or "none")
        keep_attention, keep_style = hold_flags_from_proposal(proposal.arguments)
        if user_intent or user_interrupting(frame):
            keep_attention = False
            keep_style = False
        held_style = ""
        held = self.last_accepted_nonverbal
        if (
            keep_style
            and held
            and int(held.get("generation", -1)) == int(frame.generation)
        ):
            held_style = str(held.get("presence_style") or "")
        elif keep_style:
            keep_style = False
        style, target = presence_from_proposal(
            proposal.arguments, intent=intent, frame=frame,
            keep_attention=keep_attention,
            keep_style=keep_style,
            current_style=held_style,
        )
        tone, intensity = reaction_from_proposal(
            proposal.arguments, frame=frame,
        )
        posture = vitality_from_proposal(proposal.arguments, frame=frame)
        style, tone, intensity = apply_vitality_posture(
            posture, style=style, tone=tone, intensity=intensity, frame=frame,
        )
        extra["presence_style"] = style
        extra["reaction_tone"] = tone
        extra["reaction_intensity"] = intensity
        extra["vitality_posture"] = posture
        extra["keep_attention"] = keep_attention
        extra["keep_style"] = keep_style
        horizon = wait_horizon_from_proposal(proposal.arguments)
        wake_set = wake_set_from_proposal(proposal.arguments)
        extra["wait_horizon"] = horizon
        extra["wake_set"] = wake_set
        horizon_ms = wait_ms_for_horizon(horizon)
        if intent in {"wait", "noop"}:
            ttl = horizon_ms
        else:
            ttl = int(frame.temporal.min_hold_remaining_ms or _DEFAULT_WAIT_MS)
        ttl = min(MAX_WAIT_MS, max(0, int(ttl)))
        if intent == "backchannel_user":
            return self._execute_micro(
                proposal, frame, extra=extra, now_ms=now_ms,
                ttl_ms=ttl, target=target, hold=True,
                user_intent=user_intent, trigger_kind=trigger_kind,
                presence_style=style,
                reaction_tone=tone,
                reaction_intensity=intensity,
            )
        if intent not in NONVERBAL_OK_EMPTY_URGE:
            extra["budget_class"] = ""
            return False, extra
        if self._repeat_holding(
            intent, target, now_ms, int(frame.generation), style,
            reaction_tone=tone, reaction_intensity=intensity,
        ):
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
            self._hold_nonverbal(
                frame, intent, target, ttl, now_ms, "",
                presence_style=style,
                reaction_tone=tone,
                reaction_intensity=intensity,
                keep_attention=keep_attention,
                keep_style=keep_style,
            )
            self._notify_executed(frame)
            return True, extra
        exempt = bool(user_intent) or trigger_kind in _BUDGET_EXEMPT_KINDS
        switched = False
        if target != current and not exempt:
            runtime = self._inclination()
            spent = True
            if runtime is not None:
                try:
                    spent = runtime.budget.consume(
                        "attention_switch", now_mono_ms=now_ms,
                    )
                except Exception:
                    log.debug("live budget consume failed", exc_info=True)
                    spent = True
            if spent:
                switched = True
                extra["attention_switch"] = True
            else:
                extra["attention_held"] = "budget"
                target = current
        budget_class = INTENT_BUDGET.get(intent, "")
        extra["budget_class"] = budget_class
        if budget_class and not exempt and not switched:
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
        self._hold_nonverbal(
            frame, intent, target, ttl, now_ms, budget_class,
            presence_style=style,
            reaction_tone=tone,
            reaction_intensity=intensity,
            keep_attention=keep_attention,
            keep_style=keep_style,
        )
        self._notify_executed(frame)
        if intent in MICRO_INTENTS:
            self._execute_micro(
                proposal, frame, extra=extra, now_ms=now_ms,
                ttl_ms=ttl, target=target, hold=False,
                user_intent=user_intent, trigger_kind=trigger_kind,
                presence_style=style,
                reaction_tone=tone,
                reaction_intensity=intensity,
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
        act = speech_act_from_proposal(proposal.arguments, frame=frame)
        subject = capped_urge_subject(urge)
        cue_id = getattr(urge, "cue_id", None)
        payload = {
            "generation": int(frame.generation),
            "urge_id": str(proposal.selected_urge_id or ""),
            "urge_kind": str(getattr(urge, "kind", "") or ""),
            "reason_code": str(proposal.reason_code or ""),
            "intent": proposal.intent,
            "speech_act": act,
            "cue_subject": subject,
            "cue_id": cue_id,
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
        if not enqueued:
            if runtime is not None:
                runtime.budget.refund("main_wake", now_mono_ms=now_ms)
            extra["main_wake_reason"] = "enqueue_failed"
            extra["main_wake_rejected"] = "enqueue_failed"
            self.note_main_wake_reject("enqueue_failed", int(frame.generation))
            return False, extra
        self._mark_main_wake_admitted(int(frame.generation))
        extra["main_wake_admitted"] = True
        extra["main_wake_enqueued"] = True
        self.proactive_enqueued += 1
        if runtime is not None and urge is not None:
            runtime.urges.consume(urge.urge_id)
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
        if str(reason or "") in {"floor_busy", "turn_or_tts"}:
            record = self.last_action
            action_id = str(getattr(record, "action_id", "") or "")
            intent = str(getattr(record, "intent", "") or "request_main_speech")
            if action_id:
                self._arm_fallback(
                    action_id=action_id,
                    failure="floor_preempted",
                    original_intent=intent,
                    generation=int(generation),
                    frame=None,
                )

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
        presence_style: str = "",
        reaction_tone: str = "",
        reaction_intensity: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        extra.setdefault("budget_class", "micro_speech")
        extra.setdefault("presence_style", presence_style)
        extra.setdefault("reaction_tone", reaction_tone)
        extra.setdefault("reaction_intensity", reaction_intensity)
        raw = proposed_micro_text(proposal.arguments)
        text = validate_micro_utterance(raw)
        if text is None:
            extra["micro_skipped"] = "invalid" if raw.strip() else "missing_text"
            return False, extra
        block = self._micro_block_reason(
            proposal, frame, now_ms=now_ms,
            user_intent=user_intent, trigger_kind=trigger_kind,
            presence_style=presence_style,
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
            self._hold_nonverbal(
                frame, proposal.intent, target, ttl_ms, now_ms, "micro_speech",
                presence_style=presence_style,
                reaction_tone=reaction_tone,
                reaction_intensity=reaction_intensity,
                keep_attention=bool(extra.get("keep_attention")),
                keep_style=bool(extra.get("keep_style")),
            )
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
        presence_style: str = "",
    ) -> str:
        if user_intent or trigger_kind in USER_INTENT_KINDS:
            return "user_turn"
        if not self._unprompted_speech_enabled():
            return "speech_forbidden"
        constraints = frame.constraints
        style = presence_style or clamp_presence_style(
            proposal.arguments.get("presence_style"), frame,
        )
        speech_budget = quieter_speech(
            str(constraints.speech_budget or "normal"),
            STYLE_SPEECH_CAP.get(style, "open"),
        )
        if speech_budget == "forbidden":
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
        if speech_budget == "rare" and not proposal.selected_urge_id:
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
        presence_style: str = "",
        reaction_tone: str = "",
        reaction_intensity: str = "",
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
        if str(held.get("presence_style") or "") != str(presence_style or ""):
            return False
        if str(held.get("reaction_tone") or "") != str(reaction_tone or ""):
            return False
        if str(held.get("reaction_intensity") or "") != str(reaction_intensity or ""):
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
        *,
        presence_style: str = "",
        reaction_tone: str = "",
        reaction_intensity: str = "",
        keep_attention: bool = False,
        keep_style: bool = False,
    ) -> None:
        ttl = max(0, min(MAX_WAIT_MS, int(ttl_ms)))
        self.last_accepted_nonverbal = {
            "generation": int(frame.generation),
            "intent": intent,
            "attention_target": target,
            "presence_style": str(presence_style or ""),
            "reaction_tone": str(reaction_tone or ""),
            "reaction_intensity": str(reaction_intensity or ""),
            "keep_attention": bool(keep_attention),
            "keep_style": bool(keep_style),
            "ttl_ms": ttl,
            "hold_until_ms": now_ms + ttl,
            "budget_class": budget_class,
            "action_id": str(
                getattr(self._current_action, "action_id", "") or ""
            ),
        }

    def _cancel_fallback(self) -> None:
        self._pending_fallback = None

    def _consume_fallback(self) -> dict[str, Any] | None:
        pending = self._pending_fallback
        self._pending_fallback = None
        return dict(pending) if pending else None

    def _maybe_arm_fallback(
        self,
        record: LiveActionRecord,
        extra: dict[str, Any],
        intent: str,
        frame: LiveSituationFrame,
        *,
        user_intent: bool,
    ) -> None:
        if user_intent or str(record.state or "") != "rejected":
            return
        failure = classify_execute_failure(record.reason, extra)
        if not should_arm_fallback(failure, intent=intent):
            return
        self._arm_fallback(
            action_id=record.action_id,
            failure=failure,
            original_intent=intent,
            generation=int(record.generation),
            frame=frame,
        )

    def _arm_fallback(
        self,
        *,
        action_id: str,
        failure: str,
        original_intent: str,
        generation: int,
        frame: LiveSituationFrame | None,
    ) -> bool:
        token = str(action_id or "").strip()
        if not token or token in self._attempted_fallback_ids:
            return False
        if not should_arm_fallback(failure, intent=original_intent):
            return False
        self._attempted_fallback_ids.add(token)
        self._pending_fallback = {
            "action_id": token,
            "failure": str(failure),
            "original_intent": str(original_intent or ""),
            "legal": list(legal_fallbacks(failure)),
            "generation": int(generation),
        }
        if frame is None:
            return True
        runtime = self._inclination()
        wait = getattr(runtime, "wait", None) if runtime is not None else None
        if wait is not None and getattr(wait, "current", None) is None:
            self._schedule_wait(
                frame,
                LivePolicyProposal(
                    snapshot_generation=int(frame.generation),
                    selected_urge_id="",
                    intent="wait",
                    reason_code="fallback",
                    context_refs=(),
                ),
                now_ms=time.monotonic() * 1000.0,
                ttl_ms=MIN_WAIT_MS,
                reason_code="fallback",
            )
        return True

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
        wake_on = expand_wake_set(
            wake_set_from_proposal(proposal.arguments),
            sleep_status=str(frame.constraints.sleep_status or ""),
        )
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

    def _emit_action(self, record: LiveActionRecord) -> None:
        self.last_action = record
        kind = action_result_kind(record.state)
        if not kind:
            return
        callback = self._on_action_result
        if not callable(callback):
            return
        try:
            callback(record)
        except Exception:
            log.debug("live action result notify failed", exc_info=True)

    def _finish_action(
        self,
        result: LiveArbiterResult,
        *,
        executed: bool,
        extra: dict[str, Any],
        cancelled: bool,
    ) -> LiveActionRecord | None:
        record = self._current_action
        if record is None:
            return None
        if cancelled and not executed:
            record = advance_action(record, "cancelled", reason="cancelled")
        elif not result.accepted:
            record = advance_action(record, "rejected", reason=result.reason)
        elif executed:
            record = advance_action(record, "executing", reason=result.reason)
            self._emit_action(record)
            record = advance_action(record, "completed", reason=result.reason)
        elif str(getattr(result.proposal, "intent", "") or "") == "request_main_speech":
            self._current_action = None
            return None
        else:
            reason = "not_executed"
            if extra.get("budget_exhausted"):
                reason = "budget_exhausted"
            elif extra.get("repeat_suppressed"):
                reason = "repeat_suppressed"
            elif extra.get("micro_skipped"):
                reason = str(extra.get("micro_skipped") or "not_executed")
            elif result.reason:
                reason = str(result.reason)
            record = advance_action(record, "rejected", reason=reason)
        self._current_action = None
        self.last_action = record
        self._emit_action(record)
        return record

    def _notify_executed(self, frame: LiveSituationFrame) -> None:
        callback = self._on_executed
        if not callable(callback):
            return
        try:
            callback(frame)
        except Exception:
            log.debug("live nonverbal execute notify failed", exc_info=True)

    def _attach_admission(
        self,
        frame: LiveSituationFrame,
        proposal: LivePolicyProposal | None,
        result: LiveArbiterResult,
        extra: dict[str, Any] | None,
        *,
        executed: bool = False,
        cancelled: bool = False,
        action_id: str = "",
    ) -> dict[str, Any]:
        payload = dict(extra or {})
        if result.reason == "invalid_json" or (
            proposal is None and result.reason not in {"cancelled", "ok"}
        ):
            rec = parse_error_record(
                generation=int(frame.generation),
                reason=str(result.reason or "invalid_json"),
            )
        elif cancelled or result.reason == "cancelled":
            rec = cancelled_record(
                generation=int(frame.generation),
                intent=str(getattr(proposal, "intent", "") or ""),
            )
        elif proposal is None:
            rec = parse_error_record(
                generation=int(frame.generation),
                reason=str(result.reason or "no_proposal"),
            )
        else:
            rec = build_admission_record(
                frame=frame,
                proposal=proposal,
                arbiter=result,
                extra=payload,
                executed=executed,
                cancelled=cancelled,
                action_id=action_id or str(payload.get("action_id") or ""),
            )
        self.last_admission = rec
        payload["admission"] = rec.to_payload()
        return payload

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
                    action_id=str(payload.get("action_id") or ""),
                    action_state=str(payload.get("action_state") or ""),
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
