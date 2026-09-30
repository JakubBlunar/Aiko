"""Live mode Pass 26: inspectable 12-gate admission record (L0 leftover)."""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import patch

from app.core.live.admission import (
    ADMISSION_GATES,
    admission_contains_forbidden,
    build_admission_record,
    parse_error_record,
)
from app.core.live.arbiter import LiveArbiterResult, arbitrate_live_proposal
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.proposal import LivePolicyProposal
from tests.test_live_mode_pass7 import (
    _FakePolicyClient,
    _Usage,
    _controller,
    _frame,
    _proposal,
)


def _gate_names(payload: dict) -> tuple[str, ...]:
    return tuple(str(gate["name"]) for gate in payload.get("gates") or ())


def _by_name(payload: dict) -> dict[str, dict]:
    return {
        str(gate["name"]): gate
        for gate in payload.get("gates") or ()
        if isinstance(gate, dict)
    }


class AdmissionRecordShapeTests(unittest.TestCase):
    def test_decision_trace_records_menu_without_content(self) -> None:
        frame = _frame()
        runtime = LiveInclinationRuntime()
        urge = runtime.urges.propose(
            kind="share_observation", subject="private subject", source="cue_pool",
            source_ids=("cue:123",), repetition_key="cue:123", cue_id=123,
            purpose="share", now_mono_ms=time.monotonic() * 1000.0,
        )
        assert urge is not None
        client = _FakePolicyClient({
            "snapshot_generation": frame.generation,
            "selected_urge_id": urge.urge_id,
            "intent": "attend", "arguments": {"reasoning": "private reasoning"},
            "reason_code": "private_reason", "context_refs": [],
        })
        controller = _controller(generation_provider=lambda: int(frame.generation))
        with self.assertLogs("app.live", level="INFO") as logs:
            controller._infer(
                frame, trigger_kind="idle.reconsider",
                prompt_input={"urges": (urge,), "known_urge_ids": (urge.urge_id,)},
                user_intent=False, started_generation=int(frame.generation),
                cancel=threading.Event(), client=client,
            )
        trace = controller.diagnostics()["decision_trace"]
        row = trace["decisions"][0]
        self.assertTrue(row["inference_submitted"])
        self.assertTrue(row["inference_completed"])
        self.assertEqual(row["menu"], [{
            "urge_id": urge.urge_id, "kind": "share_observation", "purpose": "share",
            "cue_id": 123,
        }])
        self.assertEqual(row["selected_urge_id"], urge.urge_id)
        self.assertEqual(row["intent"], "attend")
        self.assertEqual(row["reason"], "ok")
        self.assertEqual(len(trace["identity"]["decision_code_sha256"]), 64)
        self.assertFalse(controller.diagnostics()["live_policy_warm_requested"])
        blob = json.dumps(trace) + " ".join(logs.output)
        for forbidden in ("private subject", "private reasoning", "private_reason"):
            self.assertNotIn(forbidden, blob)

    def test_decision_trace_retains_failure_and_bounds_history(self) -> None:
        frame = _frame()
        controller = _controller(generation_provider=lambda: int(frame.generation))
        for _ in range(70):
            controller._infer(
                frame, trigger_kind="idle.reconsider", prompt_input={},
                user_intent=False, started_generation=int(frame.generation),
                cancel=threading.Event(),
            )
        rows = controller.diagnostics()["decision_trace"]["decisions"]
        self.assertEqual(len(rows), 64)
        self.assertEqual(rows[-1]["reason"], "no_client")
        self.assertFalse(rows[-1]["inference_submitted"])
        self.assertEqual(len({row["decision_id"] for row in rows}), 64)

    def test_trace_distinguishes_model_failure_from_completed_cancellation(self) -> None:
        frame = _frame()
        controller = _controller(generation_provider=lambda: int(frame.generation))
        client = _FakePolicyClient()
        with patch.object(client, "chat_json", side_effect=RuntimeError("private failure")):
            with self.assertRaises(RuntimeError):
                controller._infer(
                    frame, trigger_kind="idle.reconsider", prompt_input={},
                    user_intent=False, started_generation=int(frame.generation),
                    cancel=threading.Event(), client=client,
                )
        failed = controller.diagnostics()["decision_trace"]["decisions"][-1]
        self.assertEqual(failed["reason"], "model_failure")
        self.assertTrue(failed["inference_submitted"])
        self.assertFalse(failed["inference_completed"])
        self.assertNotIn("private failure", json.dumps(failed))
        cancel = threading.Event()

        def finish_cancelled(*args, **kwargs):
            cancel.set()
            return json.dumps(client.payload), _Usage()

        with patch.object(client, "chat_json", side_effect=finish_cancelled):
            controller._infer(
                frame, trigger_kind="idle.reconsider", prompt_input={},
                user_intent=False, started_generation=int(frame.generation),
                cancel=cancel, client=client,
            )
        cancelled = controller.diagnostics()["decision_trace"]["decisions"][-1]
        self.assertEqual(cancelled["reason"], "cancelled")
        self.assertTrue(cancelled["inference_completed"])
        self.assertFalse(cancelled["executed"])

    def test_worker_failure_is_distinct_from_silence(self) -> None:
        frame = _frame()
        controller = _controller(generation_provider=lambda: int(frame.generation))
        with (
            patch.object(controller, "_infer", side_effect=RuntimeError("private prompt")),
            patch("app.core.live.controller.threading.Thread") as thread,
        ):
            controller._spawn(
                frame, trigger_kind="idle.reconsider", prompt_input={}, user_intent=False,
            )
            thread.call_args.kwargs["target"]()
        details = controller.diagnostics()
        self.assertEqual(details["policy_failure_count"], 1)
        self.assertEqual(details["last_policy_failure"], {
            "reason": "model_failure",
            "generation": int(frame.generation),
            "trigger_kind": "idle.reconsider",
        })
        self.assertNotIn("private prompt", str(details))
        self.assertFalse(details["live_policy_inflight"])

    def test_missing_client_is_recorded_without_a_proposal(self) -> None:
        frame = _frame()
        controller = _controller(
            client_provider=lambda: None,
            generation_provider=lambda: int(frame.generation),
        )
        with patch("app.core.live.controller.threading.Thread") as thread:
            controller._spawn(
                frame, trigger_kind="idle.reconsider", prompt_input={}, user_intent=False,
            )
            thread.call_args.kwargs["target"]()
        details = controller.diagnostics()
        self.assertEqual(details["policy_failure_count"], 1)
        self.assertEqual(details["last_policy_failure"]["reason"], "no_client")
        self.assertFalse(details["last_policy_proposal"])

    def test_wait_lists_twelve_gates_in_order(self) -> None:
        client = _FakePolicyClient()
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: LiveInclinationRuntime(),
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.accepted)
        self.assertTrue(controller.last_proposal["executed"])
        rec = controller.last_admission
        assert rec is not None
        payload = rec.to_payload()
        self.assertEqual(_gate_names(payload), ADMISSION_GATES)
        gates = _by_name(payload)
        self.assertEqual(gates["schema"]["status"], "pass")
        self.assertEqual(gates["main_wake"]["status"], "skip")
        self.assertEqual(gates["main_wake"]["reason"], "not_requested")
        self.assertEqual(gates["executor"]["status"], "pass")
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["rejected_gate"], "")
        self.assertEqual(controller.diagnostics()["last_admission"], payload)
        self.assertEqual(
            controller.last_proposal["admission"]["gates"][10]["reason"],
            "not_requested",
        )
        self.assertFalse(admission_contains_forbidden(rec))
        blob = json.dumps(payload)
        self.assertNotIn("confidence", blob.lower())
        self.assertNotIn("Param", blob)
        self.assertNotIn("title", blob.lower())

    def test_invalid_json_rejects_schema_and_skips_rest(self) -> None:
        truncated = (
            '{"snapshot_generation": 1, "selected_urge_id": null, '
            '"intent": "noop", "arguments": {"reasoning": "The user has'
        )

        class _Truncated:
            def chat_json(self, messages, **kwargs):
                del messages, kwargs
                return truncated, _Usage()

        frame = _frame()
        controller = _controller(
            generation_provider=lambda: int(frame.generation),
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=_Truncated(),
        )
        assert result is not None
        self.assertEqual(result.reason, "invalid_json")
        row = controller.diagnostics()["decision_trace"]["decisions"][-1]
        self.assertEqual(row["reason"], "invalid_json")
        self.assertTrue(row["inference_completed"])
        self.assertNotIn("The user has", json.dumps(row))
        rec = controller.last_admission
        assert rec is not None
        payload = rec.to_payload()
        self.assertEqual(_gate_names(payload), ADMISSION_GATES)
        gates = _by_name(payload)
        self.assertEqual(gates["schema"]["status"], "reject")
        self.assertEqual(gates["schema"]["reason"], "invalid_json")
        self.assertEqual(payload["rejected_gate"], "schema")
        for name in ADMISSION_GATES[1:]:
            self.assertEqual(gates[name]["status"], "skip")
            self.assertEqual(gates[name]["reason"], "schema")

    def test_sleep_speech_is_schema_not_allowed(self) -> None:
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": "u1",
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "want_to_talk",
            "context_refs": [],
        })
        frame = _frame(sleep={"status": "asleep"})
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": ("u1",)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "not_allowed")
        rec = controller.last_admission
        assert rec is not None
        payload = rec.to_payload()
        gates = _by_name(payload)
        self.assertEqual(gates["schema"]["status"], "reject")
        self.assertEqual(gates["schema"]["reason"], "not_allowed")
        self.assertEqual(payload["rejected_gate"], "schema")
        self.assertEqual(gates["main_wake"]["status"], "skip")

    def test_budget_exhaust_rejects_constraints(self) -> None:
        runtime = LiveInclinationRuntime()
        now_ms = time.monotonic() * 1000.0
        while runtime.budget.consume("expression", now_mono_ms=now_ms):
            pass
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "react_affectively",
            "arguments": {},
            "reason_code": "react",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.accepted)
        self.assertTrue(controller.last_proposal.get("budget_exhausted"))
        rec = controller.last_admission
        assert rec is not None
        payload = rec.to_payload()
        gates = _by_name(payload)
        self.assertEqual(gates["constraints"]["status"], "reject")
        self.assertEqual(gates["constraints"]["reason"], "budget_exhausted")
        self.assertEqual(payload["rejected_gate"], "constraints")
        self.assertEqual(gates["main_wake"]["status"], "skip")
        self.assertFalse(payload["accepted"])

    def test_missing_urge_rejects_main_wake_and_keeps_talk_about(self) -> None:
        spoken: list[str] = []
        enqueued: list[dict] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": "u1",
            "intent": "request_main_speech",
            "arguments": {"confidence": 0.9, "title": "secret.md"},
            "reason_code": "want_to_talk",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
            on_main_wake=lambda payload: enqueued.append(payload) or True,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": ("u1",)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.talk_about)
        self.assertEqual(
            controller.last_proposal.get("main_wake_rejected"), "missing_urge",
        )
        rec = controller.last_admission
        assert rec is not None
        payload = rec.to_payload()
        gates = _by_name(payload)
        self.assertEqual(gates["schema"]["status"], "pass")
        self.assertEqual(gates["main_wake"]["status"], "reject")
        self.assertEqual(gates["main_wake"]["reason"], "missing_urge")
        self.assertEqual(payload["rejected_gate"], "main_wake")
        self.assertTrue(payload["talk_about"])
        self.assertFalse(payload["accepted"])
        self.assertEqual(enqueued, [])
        self.assertEqual(spoken, [])
        self.assertFalse(admission_contains_forbidden(rec))
        blob = json.dumps(payload)
        self.assertNotIn("confidence", blob.lower())
        self.assertNotIn("secret.md", blob)
        self.assertNotIn("Param", blob)

    def test_parse_error_helper_matches_gate_order(self) -> None:
        rec = parse_error_record(generation=4)
        payload = rec.to_payload()
        self.assertEqual(_gate_names(payload), ADMISSION_GATES)
        self.assertEqual(payload["rejected_gate"], "schema")

    def test_overlay_skip_does_not_reject_wait(self) -> None:
        frame = _frame()
        proposal = _proposal(intent="wait")
        arbiter = LiveArbiterResult(True, "ok", proposal)
        rec = build_admission_record(
            frame=frame,
            proposal=proposal,
            arbiter=arbiter,
            extra={"overlay_skipped": "turn_or_tts"},
            executed=True,
        )
        payload = rec.to_payload()
        gates = _by_name(payload)
        self.assertEqual(gates["playback"]["status"], "pass")
        self.assertTrue(payload["accepted"])
        self.assertEqual(gates["main_wake"]["reason"], "not_requested")

    def test_unload_clears_last_admission(self) -> None:
        client = _FakePolicyClient()
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
        )
        controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        self.assertIsNotNone(controller.last_admission)
        controller.unload()
        self.assertIsNone(controller.last_admission)
        self.assertIsNone(controller.diagnostics()["last_admission"])


class ArbiterStillDoesNotAuthorizeTests(unittest.TestCase):
    def test_empty_speech_urge_stays_arbiter_missing_urge(self) -> None:
        result = arbitrate_live_proposal(
            LivePolicyProposal(
                snapshot_generation=1,
                selected_urge_id="",
                intent="request_main_speech",
                arguments={},
                reason_code="want_to_talk",
                context_refs=(),
            ),
            snapshot_generation=1,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "missing_urge")
        rec = build_admission_record(
            frame=_frame(),
            proposal=result.proposal,
            arbiter=result,
        )
        self.assertEqual(rec.rejected_gate, "schema")
        self.assertEqual(rec.reason, "missing_urge")


if __name__ == "__main__":
    unittest.main()
