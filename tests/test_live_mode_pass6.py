"""Live mode Pass 6: shadow policy bake-off. Avatar unchanged; chat still replies."""
from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.concepts.concept_diets import (
    DietTuning,
    diet_for,
    resolve_budget,
    tuning_from_route,
)
from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.infra.settings import LlmProvider, LlmRoute
from app.core.live.arbiter import arbitrate_live_proposal
from app.core.live.assembler import LiveAssembleInput, LiveSituationAssembler
from app.core.live.controller import LivePolicyController
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.proposal import (
    LIVE_POLICY_INTENTS,
    LIVE_POLICY_JSON_SCHEMA,
    LivePolicyProposal,
)
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.llm_clients_mixin import LlmClientsMixin
from app.llm.llm_gate import (
    CONVERSATION_WORKER,
    LIVE_POLICY,
    MAINTENANCE_WORKER,
    TASK,
    GatedChatClient,
    LlmPriorityGate,
    llm_resource_key,
)
from app.llm.factory import ClientCache


def _now() -> datetime:
    return datetime(2026, 9, 11, 17, 0, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-11T17:00:00Z",
        input_mode="typed",
        floor_transition="neither",
        dialogue_act="",
        arc="casual_check_in",
        arc_confidence=0.4,
        mood_label="content",
        vitality_band="normal",
        user_present=True,
        user_active_app="Cursor",
        world=WorldSituation(location_slug="beanbag", activity="idle"),
        inferred=None,
        inferred_stale=True,
        world_compatible=False,
        conflict_reason="",
        shared_commitment_active=False,
        since_user_activity_ms=0,
        sleep={"status": "awake"},
    )
    fields.update(kwargs)
    return ConversationSituationSnapshot(**fields)  # type: ignore[arg-type]


def _frame(**kwargs: object):
    assembler = LiveSituationAssembler()
    inp = LiveAssembleInput(
        snapshot=_snapshot(**kwargs),
        impulses=(),
        now=_now(),
        monotonic_ms=10_000.0,
        trigger_kind="heartbeat",
    )
    return assembler.assemble(inp)


def _proposal(**kwargs: object) -> LivePolicyProposal:
    fields: dict[str, object] = dict(
        snapshot_generation=1,
        selected_urge_id="",
        intent="wait",
        arguments={},
        reason_code="nothing_new",
        context_refs=(),
    )
    fields.update(kwargs)
    return LivePolicyProposal(**fields)  # type: ignore[arg-type]


class _Usage:
    prompt_tokens = 42
    completion_tokens = 8


class _FakePolicyClient:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {
            "snapshot_generation": 1,
            "selected_urge_id": "",
            "intent": "wait",
            "arguments": {},
            "reason_code": "idle",
            "context_refs": [],
            "confidence": 0.99,
        }
        self.calls: list[dict[str, object]] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return json.dumps(self.payload), _Usage()


class SchemaTests(unittest.TestCase):
    def test_schema_round_trip_strips_confidence(self) -> None:
        raw = {
            "snapshot_generation": 4,
            "selected_urge_id": None,
            "intent": "remain_present",
            "arguments": {"note": "ok"},
            "reason_code": "shared",
            "context_refs": ["1"],
            "confidence": 0.88,
        }
        parsed = LivePolicyProposal.model_validate(raw)
        self.assertEqual(parsed.intent, "remain_present")
        self.assertEqual(parsed.selected_urge_id, "")
        self.assertNotIn("confidence", parsed.to_payload())
        self.assertIn("intent", LIVE_POLICY_JSON_SCHEMA["properties"])
        self.assertEqual(
            set(LIVE_POLICY_JSON_SCHEMA["properties"]["intent"]["enum"]),
            set(LIVE_POLICY_INTENTS),
        )
        self.assertFalse(LIVE_POLICY_JSON_SCHEMA.get("additionalProperties", True))

    def test_arbiter_never_reads_confidence(self) -> None:
        proposal = LivePolicyProposal.model_validate({
            "snapshot_generation": 1,
            "selected_urge_id": "",
            "intent": "wait",
            "arguments": {"confidence": 0.4},
            "reason_code": "idle",
            "context_refs": [],
        })
        result = arbitrate_live_proposal(
            proposal, snapshot_generation=1, user_intent=False,
        )
        self.assertTrue(result.accepted)
        self.assertNotIn("confidence", result.proposal.arguments if result.proposal else {})


class ArbiterTests(unittest.TestCase):
    def test_model_generation_echo_is_not_authorization(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(snapshot_generation=1), snapshot_generation=2,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "ok")

    def test_rig_identifier_rejected(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(intent="attend", selected_urge_id="u1", reason_code="ParamEyeLOpen"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "rig_identifier")

    def test_unprompted_main_speech_is_talk_about(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="u1"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
            user_intent=False,
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.talk_about)


class PromptAssemblerTests(unittest.TestCase):
    def test_fresh_message_and_idle_opening_have_distinct_authoritative_state(self) -> None:
        assembler = LivePolicyPromptAssembler()
        base = _frame()
        idle = replace(
            base, monotonic_ms=610_000,
            interaction=replace(base.interaction, silence_since_user_intent_ms=600_000),
            constraints=replace(base.constraints, stale_sources=("c6", "os_idle")),
        )
        fresh = replace(
            idle, interaction=replace(
                idle.interaction, floor_owner="user", turn_active=True,
                silence_since_user_intent_ms=0,
            ),
        )
        impulse = SimpleNamespace(kind="user.message_sent", mode_generation=1, monotonic_ms=10_000)
        texts = [
            assembler.assemble(
                frame=frame, trigger_kind=trigger, impulses=(impulse,),
                context_window=40960, max_tokens=512, prompt_ceiling=2000,
            ).messages[0]["content"]
            for frame, trigger in ((idle, "idle.reconsider"), (fresh, "user.message_sent"))
        ]
        self.assertIn("trigger=idle.reconsider", texts[0])
        self.assertIn("floor=neither turn_active=0", texts[0])
        self.assertIn("since_user_intent_ms=600000", texts[0])
        self.assertIn("stale_sources=c6,os_idle", texts[0])
        self.assertIn("age_ms=600000", texts[0])
        self.assertIn("trigger=user.message_sent", texts[1])
        self.assertIn("floor=user turn_active=1", texts[1])
        self.assertIn("since_user_intent_ms=0", texts[1])
        self.assertIn("wait or noop", texts[0])

    def test_instructions_allow_brief_reasoning(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_frame(),
            context_window=40960,
            max_tokens=512,
            prompt_ceiling=12000,
        )
        system = prompt.messages[0]["content"]
        self.assertIn("arguments.reasoning", system)
        self.assertIn("reason_code", system)
        self.assertIn("complete JSON", system)
        self.assertIn("context_refs", system)
        self.assertIn("wait and noop are for idle", system)

    def test_sizes_off_live_policy_window(self) -> None:
        frame = _frame()
        concepts = [
            SimpleNamespace(lane="rail", kind="value", label=f"value-{i} " * 8)
            for i in range(16)
        ]
        rows = [
            SimpleNamespace(
                role="user" if i % 2 == 0 else "assistant",
                content=f"line {i} " * 120,
                created_at="2026-09-11T16:50:00Z",
            )
            for i in range(80)
        ]
        assembler = LivePolicyPromptAssembler()
        wide = assembler.assemble(
            frame=frame,
            concepts=concepts,
            transcript_rows=rows,
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
            diet_tuning=DietTuning(context_window=40960),
        )
        narrow = assembler.assemble(
            frame=frame,
            concepts=concepts,
            transcript_rows=rows,
            context_window=4096,
            max_tokens=64,
            prompt_ceiling=12000,
            diet_tuning=DietTuning(context_window=4096),
        )
        self.assertGreater(wide.available_tokens, narrow.available_tokens)
        self.assertGreaterEqual(
            wide.region_tokens["concepts"], narrow.region_tokens["concepts"],
        )
        # Pass 10: LAST CONVERSATION is a reserved floor, not leftover
        # after concepts, so both windows keep a non-empty transcript.
        self.assertGreater(wide.region_tokens["transcript"], 0)
        self.assertGreater(narrow.region_tokens["transcript"], 0)

    def test_prompt_ceiling_binds_before_window(self) -> None:
        frame = _frame()
        assembler = LivePolicyPromptAssembler()
        prompt = assembler.assemble(
            frame=frame,
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=2000,
        )
        self.assertLessEqual(prompt.available_tokens, 2000)
        self.assertLessEqual(prompt.total_tokens, prompt.available_tokens)

    def test_situation_kept_when_history_overflows(self) -> None:
        frame = _frame()
        rows = [
            SimpleNamespace(
                role="user",
                content=("recent chat " * 80),
                created_at="2026-09-11T16:59:00Z",
            )
            for _ in range(40)
        ]
        prompt = LivePolicyPromptAssembler().assemble(
            frame=frame,
            transcript_rows=rows,
            context_window=4096,
            max_tokens=64,
            prompt_ceiling=2000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("SITUATION:", text)
        self.assertIn("app=Cursor", text)
        self.assertIn("World truth", text)

    def test_omits_persona_and_t3(self) -> None:
        text = LivePolicyPromptAssembler().assemble(
            frame=_frame(),
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
        ).messages[0]["content"]
        lowered = text.lower()
        self.assertNotIn("aiko_companion", lowered)
        self.assertNotIn("you are aiko", lowered)
        self.assertNotIn("### t3", lowered)
        self.assertNotIn("tools:", lowered)

    def test_world_truth_coding_not_replaced_by_anime_prior(self) -> None:
        frame = _frame()
        concepts = [
            SimpleNamespace(
                lane="rail",
                kind="ritual",
                label="watching anime together",
            ),
        ]
        text = LivePolicyPromptAssembler().assemble(
            frame=frame,
            concepts=concepts,
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
        ).messages[0]["content"]
        self.assertIn("app=Cursor", text)
        self.assertIn("World truth", text)
        self.assertIn("watching anime together", text)
        situation, _sep, rest = text.partition("CONCEPTS:")
        self.assertIn("app=Cursor", situation)
        self.assertNotIn("watching anime together", situation)


class DietWindowTests(unittest.TestCase):
    def test_live_policy_diet_caps_raised(self) -> None:
        diet = diet_for("live_policy")
        assert diet is not None
        self.assertEqual(diet.max_concepts, 16)
        self.assertEqual(diet.per_kind_cap, 3)

    def test_tuning_from_route_uses_live_policy_window(self) -> None:
        host = SimpleNamespace(
            _memory_settings=None,
            _worker_route_model_ctx=lambda: ("qwen3.5:9b", 65536),
            _route_or_none=lambda role: (
                SimpleNamespace(context_window=40960)
                if role == "live_policy"
                else SimpleNamespace(context_window=65536)
            ),
        )
        live = tuning_from_route(host, role="live_policy")
        worker = tuning_from_route(host, role="worker_default")
        self.assertEqual(live.context_window, 40960)
        self.assertEqual(worker.context_window, 65536)
        diet = diet_for("live_policy")
        assert diet is not None
        small = DietTuning(context_window=4096)
        big = DietTuning(context_window=40960)
        self.assertLess(resolve_budget(diet, small), resolve_budget(diet, big))


class GateTopologyTests(unittest.TestCase):
    def test_live_policy_tier_sits_between_conversation_and_maintenance(self) -> None:
        self.assertLess(CONVERSATION_WORKER, LIVE_POLICY)
        self.assertLess(LIVE_POLICY, MAINTENANCE_WORKER)

    def test_distinct_models_are_different_keys(self) -> None:
        four = llm_resource_key(
            kind="ollama",
            base_url="http://127.0.0.1:11434/",
            model="qwen3.5:4b",
        )
        nine = llm_resource_key(
            kind="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
        )
        self.assertNotEqual(four, nine)

    def test_contention_group_collides_across_models(self) -> None:
        four = llm_resource_key(
            kind="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:4b",
            contention_group="gpu0",
        )
        nine = llm_resource_key(
            kind="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            contention_group="gpu0",
        )
        self.assertEqual(four, nine)


class _GateHost(LlmClientsMixin):
    def __init__(
        self,
        *,
        live_model: str,
        worker_model: str,
        live_group: str = "",
        worker_group: str = "",
        gate_enabled: bool = True,
    ) -> None:
        provider = LlmProvider(
            id="local_ollama",
            name="Local",
            kind="ollama",
            base_url="http://127.0.0.1:11434",
        )
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                worker_llm_gate_enabled=gate_enabled,
                worker_llm_priority_overrides={},
            ),
            llm=SimpleNamespace(
                providers=[provider],
                routes={
                    "live_policy": LlmRoute(
                        provider_id="local_ollama",
                        model=live_model,
                        contention_group=live_group,
                    ),
                    "worker_default": LlmRoute(
                        provider_id="local_ollama",
                        model=worker_model,
                        contention_group=worker_group,
                    ),
                },
            ),
        )
        self._worker_llm_gate = LlmPriorityGate(name="worker")
        self._live_policy_client = None

    def _route_or_none(self, role: str) -> LlmRoute | None:
        return self._settings.llm.routes.get(role)

    def _find_llm_provider(self, provider_id: str) -> LlmProvider:
        return self._settings.llm.providers[0]

    def _build_live_policy_raw_client(self):
        return _FakePolicyClient()


class LivePolicyClientInstallTests(unittest.TestCase):
    def test_distinct_model_is_passthrough(self) -> None:
        host = _GateHost(live_model="qwen3.5:4b", worker_model="qwen3.5:9b")
        host._install_live_policy_client()
        client = host._live_policy_client
        assert isinstance(client, GatedChatClient)
        self.assertIsNone(client._gate)

    def test_same_model_joins_worker_gate(self) -> None:
        host = _GateHost(live_model="qwen3.5:9b", worker_model="qwen3.5:9b")
        host._install_live_policy_client()
        client = host._live_policy_client
        assert isinstance(client, GatedChatClient)
        self.assertIs(client._gate, host._worker_llm_gate)
        self.assertEqual(client._priority, LIVE_POLICY)


class WorkflowClientInstallTests(unittest.TestCase):
    @staticmethod
    def _host(
        *,
        workflow_group: str = "",
        worker_group: str = "",
        workflow_on_mac: bool = False,
    ):
        host = LlmClientsMixin()
        mac = LlmProvider(
            id="mac_lmstudio",
            name="Mac LM Studio",
            kind="openai_compatible",
            base_url="http://aiko-mac.local:1234/v1",
        )
        windows = LlmProvider(
            id="windows_ollama",
            name="Windows Ollama",
            kind="ollama",
            base_url="http://127.0.0.1:11434",
        )
        host._settings = SimpleNamespace(
            llm=SimpleNamespace(
                providers=[mac, windows],
                routes={
                    "worker_default": LlmRoute(
                        provider_id="mac_lmstudio",
                        model="worker-model",
                        context_window=32768,
                        contention_group=worker_group,
                    ),
                    "workflow": LlmRoute(
                        provider_id=(
                            "mac_lmstudio" if workflow_on_mac
                            else "windows_ollama"
                        ),
                        model="workflow-model",
                        context_window=65536,
                        contention_group=workflow_group,
                    ),
                },
            ),
        )
        host._client_cache = ClientCache()
        host._worker_client_inner = _FakePolicyClient()
        host._find_llm_provider = lambda provider_id: next(
            (
                provider
                for provider in host._settings.llm.providers
                if provider.id == provider_id
            ),
            None,
        )
        return host

    def test_workflow_on_second_machine_bypasses_worker_gate(self) -> None:
        host = self._host()
        worker_gate = LlmPriorityGate(name="worker")
        client = host._build_workflow_client(worker_gate, TASK)
        self.assertIsInstance(client, GatedChatClient)
        self.assertIsNone(client._gate)

    def test_explicit_contention_group_shares_worker_gate(self) -> None:
        host = self._host(
            workflow_group="shared-host",
            worker_group="shared-host",
            workflow_on_mac=True,
        )
        worker_gate = LlmPriorityGate(name="worker")
        client = host._build_workflow_client(worker_gate, TASK)
        self.assertIsInstance(client, GatedChatClient)
        self.assertIs(client._gate, worker_gate)


class ControllerTests(unittest.TestCase):
    def test_generation_cancel_drops_late_proposal(self) -> None:
        client = _FakePolicyClient()
        gen = {"value": 1}

        def _gen() -> int:
            return int(gen["value"])

        controller = LivePolicyController(
            client_provider=lambda: client,
            model_provider=lambda: "qwen3.5:4b",
            route_provider=lambda: LlmRoute(
                provider_id="local_ollama",
                model="qwen3.5:4b",
                context_window=40960,
                max_tokens=64,
                temperature=0.0,
            ),
            generation_provider=_gen,
        )
        frame = _frame()
        gen["value"] = 2
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=1,
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertEqual(result.reason, "cancelled")
        self.assertFalse(result.accepted)

    def test_unprompted_main_speech_does_not_enqueue(self) -> None:
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": "u1",
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "want_to_talk",
            "context_refs": [],
        })
        controller = LivePolicyController(
            client_provider=lambda: client,
            route_provider=lambda: LlmRoute(
                provider_id="local_ollama",
                model="qwen3.5:4b",
                context_window=40960,
                max_tokens=64,
            ),
            generation_provider=lambda: 0,
        )
        frame = _frame()
        # Assembler stamps generation 1.
        client.payload["snapshot_generation"] = int(frame.generation)
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": ("u1",)},
            user_intent=False,
            started_generation=0,
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.talk_about)
        self.assertEqual(controller.proactive_enqueued, 0)
        self.assertFalse(controller.last_proposal.get("executed"))
        schema = client.calls[0]["json_schema"]
        self.assertEqual(schema["title"], "live_policy_proposal")

    def test_heartbeat_does_not_poll(self) -> None:
        client = _FakePolicyClient()
        controller = LivePolicyController(client_provider=lambda: client)
        controller.consider(_frame(), trigger_kind="heartbeat")
        self.assertEqual(client.calls, [])
        self.assertFalse(controller._inflight)

    def test_critical_admit_does_not_overwrite_shadow_proposal(self) -> None:
        controller = LivePolicyController()
        controller.last_proposal = {
            "proposal": {"intent": "wait", "reason_code": "idle"},
            "shadow": True,
        }
        controller._admit_critical(_frame())
        self.assertEqual(controller.last_proposal["proposal"]["intent"], "wait")
        admit = controller.last_user_intent_admit
        self.assertEqual(admit["proposal"]["intent"], "request_main_speech")
        self.assertEqual(admit["proposal"]["reason_code"], "critical_user_intent")

    def test_invalid_json_is_stored_on_last_proposal(self) -> None:
        truncated = (
            '{"snapshot_generation": 1, "selected_urge_id": null, '
            '"intent": "noop", "arguments": {"reasoning": "The user has'
        )

        class _Truncated:
            def chat_json(self, messages, **kwargs):
                del messages, kwargs
                return truncated, _Usage()

        controller = LivePolicyController(
            route_provider=lambda: LlmRoute(
                provider_id="local_ollama",
                model="qwen3.5:4b",
                context_window=40960,
                max_tokens=512,
            ),
            generation_provider=lambda: 1,
        )
        frame = _frame()
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=1,
            cancel=threading.Event(),
            client=_Truncated(),
        )
        assert result is not None
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "invalid_json")
        self.assertIsNone(controller.last_proposal.get("proposal"))
        self.assertEqual(
            controller.last_proposal["arbiter"]["reason"], "invalid_json",
        )
        self.assertIn("reasoning", controller.last_proposal.get("raw_preview", ""))


class InterceptTests(unittest.TestCase):
    def test_user_intent_admit_always_true_even_on_error(self) -> None:
        class _Host(LiveModeMixin):
            def __init__(self) -> None:
                self._settings = SimpleNamespace(
                    agent=SimpleNamespace(
                        behavior_posture="live_presence",
                        live_quiet=False,
                        live_impulse_bus_enabled=True,
                    ),
                )
                self._live_voice_session_active = False
                self.session_key = "u:s"
                self._chat_db = None
                self._init_live_mode()
                self._live_situation_assembler.remember(_frame())
                self._live_policy_controller = MagicMock()
                self._live_policy_controller.last_proposal = {}
                self._live_policy_controller.last_user_intent_admit = {}
                self._live_policy_controller._admit_critical.side_effect = RuntimeError("boom")

            def is_live_presence(self) -> bool:
                return True

        host = _Host()
        self.assertTrue(host.admit_live_user_intent())


if __name__ == "__main__":
    unittest.main()
