"""Pass 17: C6 Level-3 companion intake (pooled companion_activity cue)."""
from __future__ import annotations

import json
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.activity.companion_cue_worker import (
    CompanionActivityWorker,
    companion_activity_signature,
    decide_companion_activity,
    render_companion_activity_cue,
)
from app.core.activity.interpretation_worker import KV_INTERP
from app.core.infra.chat_database import ChatDatabase
from app.core.live.cue_adapter import CueUrgeAdapter
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.main_wake import admit_main_wake
from app.core.live.urge_store import LiveUrgeStore
from app.core.proactive.cue_accounting import (
    CUE_POLICIES,
    CUE_SPECS,
    FULFILMENT_SPOKEN,
    GAP_CUE_ORDER,
    MATCH_LEXICAL_OR_COSINE,
)
from app.core.proactive.cue_store import CueStore
from app.core.proactive.idle_worker import SLEEP_CONTINUE_WORKER_NAMES
from app.core.session.debug_overrides import KNOWN_OVERRIDES
from app.core.session.session_controller import SessionController
from tests.test_live_mode_pass7 import _FakePolicyClient, _controller, _frame
from tests.test_live_mode_pass13 import Pass13Host, _capture_spawns


_NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


def _interp(**kwargs: object) -> dict:
    blob = {
        "reading": "working in the editor",
        "kind": "coding",
        "app": "Cursor",
        "confidence": 0.9,
        "nothing": False,
        "reason": "ok",
        "title": "secret.md — assistant",
    }
    blob.update(kwargs)
    return blob


def _kv(interp: dict | None = None, signature: str | None = None) -> dict[str, str]:
    data: dict[str, str] = {}
    if interp is not None:
        data[KV_INTERP] = json.dumps(interp)
    if signature is not None:
        data["companion_activity.last_signature"] = signature
    return data


def _cue_store() -> tuple[CueStore, TemporaryDirectory]:
    tmp = TemporaryDirectory(ignore_cleanup_errors=True)
    return CueStore(ChatDatabase(Path(tmp.name) / "chat.db")), tmp


def _worker(
    *,
    kv: dict[str, str] | None = None,
    cues: CueStore | None = None,
    enabled: bool = True,
) -> CompanionActivityWorker:
    store = dict(kv or {})

    def _get(key: str) -> str | None:
        return store.get(key)

    def _set(key: str, value: str) -> None:
        store[key] = value

    return CompanionActivityWorker(
        kv_get=_get,
        kv_set=_set,
        enabled_provider=lambda: enabled,
        cue_store_provider=(lambda: cues) if cues is not None else None,
        user_name_provider=lambda: "Jacob",
    )


class DecideCompanionActivityTests(unittest.TestCase):
    def test_coding_reading_is_noticeable(self) -> None:
        decided = decide_companion_activity(_interp())
        self.assertIsNotNone(decided)
        assert decided is not None
        self.assertEqual(decided["kind"], "coding")
        self.assertEqual(decided["app"], "Cursor")
        self.assertNotIn("secret.md", decided["subject"])
        self.assertEqual(
            decided["signature"],
            companion_activity_signature("coding", "Cursor"),
        )

    def test_nothing_idle_and_weak_are_skipped(self) -> None:
        self.assertIsNone(decide_companion_activity(None))
        self.assertIsNone(decide_companion_activity(_interp(nothing=True)))
        self.assertIsNone(decide_companion_activity(_interp(reading="")))
        self.assertIsNone(decide_companion_activity(_interp(kind="idle")))
        self.assertIsNone(decide_companion_activity(_interp(confidence=0.2)))

    def test_render_has_no_titles(self) -> None:
        text = render_companion_activity_cue(
            user_name="Jacob",
            reading="working in the editor",
            kind="coding",
            app="Cursor",
        )
        self.assertIn("Jacob", text)
        self.assertIn("working in the editor", text)
        self.assertNotIn("secret.md", text)


class CompanionActivityWorkerTests(unittest.TestCase):
    def test_no_publish_on_nothing(self) -> None:
        cues, tmp = _cue_store()
        try:
            worker = _worker(kv=_kv(_interp(nothing=True)), cues=cues)
            result = worker.run()
            self.assertEqual(result["drafted"], 0)
            self.assertEqual(cues.count_pending("companion_activity"), 0)
        finally:
            tmp.cleanup()

    def test_no_publish_on_idle(self) -> None:
        cues, tmp = _cue_store()
        try:
            worker = _worker(kv=_kv(_interp(kind="idle")), cues=cues)
            self.assertEqual(worker.run()["drafted"], 0)
            self.assertEqual(cues.count_pending("companion_activity"), 0)
        finally:
            tmp.cleanup()

    def test_same_signature_does_not_redraft(self) -> None:
        cues, tmp = _cue_store()
        try:
            sig = companion_activity_signature("coding", "Cursor")
            worker = _worker(
                kv=_kv(_interp(), signature=sig),
                cues=cues,
            )
            demand = worker.demand(now=_NOW, last_run_at=None)
            self.assertIsNotNone(demand)
            assert demand is not None
            self.assertEqual(demand.pressure, 0.0)
            self.assertEqual(worker.run()["same_signature"], sig)
            self.assertEqual(cues.count_pending("companion_activity"), 0)
        finally:
            tmp.cleanup()

    def test_publishes_coding_without_titles(self) -> None:
        cues, tmp = _cue_store()
        try:
            worker = _worker(kv=_kv(_interp()), cues=cues)
            result = worker.run()
            self.assertEqual(result["drafted"], 1)
            rows = cues.pending("companion_activity")
            self.assertEqual(len(rows), 1)
            self.assertNotIn("secret.md", rows[0].subject)
            self.assertNotIn("secret.md", rows[0].text)
            self.assertNotIn("secret.md", json.dumps(rows[0].payload))
            self.assertIn("cursor", rows[0].subject)
            self.assertIn("working in the editor", rows[0].text)
        finally:
            tmp.cleanup()

    def test_missing_store_does_not_stall(self) -> None:
        worker = _worker(kv=_kv(_interp()), cues=None)
        demand = worker.demand(now=_NOW, last_run_at=None)
        self.assertIsNotNone(demand)
        assert demand is not None
        self.assertEqual(demand.pressure, 0.0)
        self.assertEqual(demand.reason, "no cue store")
        result = worker.run()
        self.assertEqual(result["drafted"], 0)
        self.assertTrue(result.get("publish_failed"))

    def test_new_signature_supersedes_pending(self) -> None:
        cues, tmp = _cue_store()
        try:
            worker = _worker(kv=_kv(_interp()), cues=cues)
            self.assertEqual(worker.run()["drafted"], 1)
            worker2 = _worker(
                kv=_kv(_interp(kind="media", app="YouTube", reading="watching something")),
                cues=cues,
            )
            self.assertEqual(worker2.run()["drafted"], 1)
            live = cues.pending("companion_activity")
            self.assertEqual(len(live), 1)
            self.assertIn("youtube", live[0].subject)
        finally:
            tmp.cleanup()


class CompanionActivityAccountingTests(unittest.TestCase):
    def test_spec_and_policy_are_pool_only_not_a_gap_cue(self) -> None:
        spec = CUE_SPECS["companion_activity"]
        self.assertFalse(spec.gap_cue)
        self.assertFalse(spec.slot_attr)
        self.assertFalse(spec.journal_key)
        self.assertNotIn("companion_activity", GAP_CUE_ORDER)
        policy = CUE_POLICIES["companion_activity"]
        self.assertEqual(policy.inventory_target, 1)
        self.assertEqual(policy.ttl_hours, 12.0)
        self.assertEqual(policy.surface_cooldown_hours, 12.0)
        self.assertEqual(policy.fulfilment, FULFILMENT_SPOKEN)
        self.assertEqual(policy.match_mode, MATCH_LEXICAL_OR_COSINE)
        self.assertEqual(policy.block, "companion_activity_block")
        self.assertEqual(
            policy.handling_section,
            "When you notice what {user_name} is doing on the machine:",
        )

    def test_debug_override_and_sleep_continue(self) -> None:
        self.assertIn("companion_activity_force_next", KNOWN_OVERRIDES)
        self.assertIn("companion_activity", SLEEP_CONTINUE_WORKER_NAMES)


class CompanionActivityLiveTests(unittest.TestCase):
    def test_policy_handoff_carries_cue_id_not_desktop_reading(self) -> None:
        cues, tmp = _cue_store()
        try:
            self.assertEqual(_worker(kv=_kv(_interp()), cues=cues).run()["drafted"], 1)
            row = cues.pending("companion_activity")[0]
            runtime = LiveInclinationRuntime()
            urge = CueUrgeAdapter(pending_provider=lambda: [row]).project(
                runtime.urges, now_mono_ms=time.monotonic() * 1000.0,
            )[0]
            client = _FakePolicyClient({
                "snapshot_generation": 1,
                "selected_urge_id": urge.urge_id,
                "intent": "request_main_speech",
                "arguments": {"speech_act": "share_observation"},
                "reason_code": "quiet_observation",
                "context_refs": [],
            })
            enqueued: list[dict] = []
            frame = _frame()
            controller = _controller(
                generation_provider=lambda: int(frame.generation),
                inclination_provider=lambda: runtime,
                on_main_wake=lambda payload: enqueued.append(payload) or True,
            )
            result = controller._infer(
                frame, trigger_kind="idle.reconsider",
                prompt_input={"known_urge_ids": (urge.urge_id,), "urges": (urge,)},
                user_intent=False, started_generation=int(frame.generation),
                cancel=threading.Event(), client=client,
            )
            assert result is not None
            self.assertTrue(result.accepted)
            self.assertEqual(len(enqueued), 1)
            self.assertEqual(enqueued[0]["cue_id"], row.id)
            self.assertEqual(enqueued[0]["speech_act"], "share_observation")
            self.assertNotIn("working in the editor", str(enqueued))
            self.assertIn("cursor coding", str(client.calls).lower())
            self.assertNotIn("secret.md", str(enqueued) + str(client.calls))
        finally:
            tmp.cleanup()

    def test_published_companion_cue_wakes_live_policy_once(self) -> None:
        cues, tmp = _cue_store()
        try:
            result = _worker(kv=_kv(_interp()), cues=cues).run()
            self.assertEqual(result["drafted"], 1)
            host = Pass13Host()
            host._cue_store = cues
            spawned = _capture_spawns(host)
            host._live_heartbeat_tick()
            host._live_heartbeat_tick()
            host._live_heartbeat_tick()
            self.assertEqual(spawned, ["idle.reconsider"])
            urge = next(
                urge for urge in host._live_inclination.urges.active() if urge.cue_id
            )
            self.assertEqual(urge.purpose, "share")
            self.assertNotIn("secret.md", urge.subject)
            self.assertIsNotNone(cues.available(urge.cue_id))
        finally:
            tmp.cleanup()

    def test_peek_does_not_take_pool_cue(self) -> None:
        take = MagicMock()
        row = SimpleNamespace(
            id=11,
            cue_type="companion_activity",
            subject="Cursor coding",
            last_surfaced_at=None,
        )
        store = LiveUrgeStore()
        adapter = CueUrgeAdapter(pending_provider=lambda: [row])
        created = adapter.project(store, now_mono_ms=5_000.0)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].cue_id, 11)
        self.assertEqual(created[0].kind, "share_observation")
        self.assertEqual(created[0].purpose, "share")
        take.assert_not_called()

    def test_observation_can_be_shared_without_question_permission(self) -> None:
        row = SimpleNamespace(
            id=11, cue_type="companion_activity", subject="editor work",
            last_surfaced_at=None,
        )
        urges = CueUrgeAdapter(pending_provider=lambda: [row]).project(
            LiveUrgeStore(), now_mono_ms=5_000.0,
        )
        frame = _frame()
        frame = replace(frame, constraints=replace(frame.constraints, questions_allowed=False))
        self.assertEqual(admit_main_wake(
            intent="request_main_speech", user_intent=False,
            selected_urge_id=urges[0].urge_id, urges=urges, frame=frame,
            decided_generation=None, now_mono_ms=5_001.0, budget_remaining=1,
        ), "")

    def test_still_no_live_mode_enabled_flag(self) -> None:
        controller = SessionController.__new__(SessionController)
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))
        self.assertFalse(hasattr(SessionController, "_live_mode_enabled"))


if __name__ == "__main__":
    unittest.main()
