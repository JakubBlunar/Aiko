"""Gate tests for the C6 worker-model task-report decision.

Exercises ``TaskOrchestrationMixin._dispatch_task_report`` +
``_run_task_report_decision`` through the same stub-host fixture used by
``test_task_orchestration_mixin``:

* **floor** (user-requested) parks + arms immediately regardless of the
  worker verdict, and the shadow pass logs + enriches the angle.
* **discretionary** acts on the verdict: ``drop`` parks nothing,
  ``park_for_natural_opening`` parks without arming,
  ``surface_now`` parks + arms.
* The drafted angle lands on the cue and renders in the T6 block.
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.core.brain import TaskResultEvent
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import load_settings
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from app.core.tasks import CUE_KIND_RESULT
from app.core.tasks.cue_render import render_cue_block
from app.core.tasks.report_decision import PROVENANCE_SELF, PROVENANCE_USER
from app.core.tasks.task_handler import (
    INITIATED_BY_AIKO,
    INITIATED_BY_BACKGROUND,
)


class _FakeTts:
    def is_active(self) -> bool:
        return False


class _FakeWorker:
    """chat_json returns a canned verdict blob."""

    def __init__(self, action: str, angle: str = "") -> None:
        self._blob = json.dumps({"action": action, "angle": angle})

    def chat_json(self, messages, **kwargs):  # noqa: ANN001
        return (self._blob, None)


class _Host(TaskOrchestrationMixin):
    def __init__(self, *, chat_db: ChatDatabase, settings: Any) -> None:
        self._chat_db = chat_db
        self._settings = settings
        self._user_id = "test-user"
        self._turn_in_progress = False
        self._tts = _FakeTts()
        self._last_user_activity_at = -float("inf")
        self._maintenance_client: Any = None
        self._effective_worker_model: str | None = "worker-model"
        self.user_display_name = "Jacob"

    @property
    def session_key(self) -> str:
        return f"session-{self._user_id}"


class _Fixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.chat_db = ChatDatabase(Path(self._tmp.name) / "chat.db")
        self.settings = load_settings(None)

    def host(self, **overrides: Any) -> _Host:
        agent = dataclasses.replace(self.settings.agent, **overrides)
        # Dial escalation windows up so timers never fire mid-test.
        agent = dataclasses.replace(
            agent,
            task_completion_proactive_after_seconds=3600,
            task_input_needed_proactive_after_seconds=3600,
            task_reply_when_free_seconds=3600.0,
        )
        settings = dataclasses.replace(self.settings, agent=agent)
        return _Host(chat_db=self.chat_db, settings=settings)

    def cleanup(self) -> None:
        conn = getattr(self.chat_db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.chat_db._local.conn = None  # type: ignore[union-attr]
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


def _make_task(host: _Host, *, initiated_by: str, self_initiated: bool = False) -> int:
    meta: dict[str, Any] = {"origin_prompt": "do the thing"}
    if self_initiated:
        meta["self_initiated"] = True
    return host._task_store.create(
        user_id="test-user",
        handler_name="file_search",
        title="search",
        initiated_by=initiated_by,
        metadata=meta,
    )


class FloorTierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def test_floor_parks_and_arms_regardless_of_verdict(self) -> None:
        # Even a worker that says "drop" can't suppress a user-asked task
        # in shadow mode (the hard floor).
        self.host._maintenance_client = _FakeWorker("drop")
        tid = _make_task(self.host, initiated_by=INITIATED_BY_AIKO)
        event = TaskResultEvent(
            task_id=str(tid),
            session_key="test-user",
            status="done",
            title="search",
            result_summary="found 3",
            notify_aiko=True,
        )
        self.host._on_task_result_event(event)

        self.assertEqual(self.host._task_cue_store.pending_count(), 1)
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 1,
        )

    def test_shadow_enriches_angle_without_changing_escalation(self) -> None:
        self.host._maintenance_client = _FakeWorker("drop", angle="ask if useful")
        tid = _make_task(self.host, initiated_by=INITIATED_BY_AIKO)
        event = TaskResultEvent(
            task_id=str(tid),
            session_key="test-user",
            status="done",
            title="search",
            result_summary="found 3",
            notify_aiko=True,
        )
        # Park + arm synchronously first (floor), then run the shadow
        # pass directly for determinism.
        self.host._dispatch_task_report(
            event, self.host._task_cue_store, self.host._task_escalation_manager,
        )
        self.host._run_task_report_decision(
            event,
            self.host._task_cue_store,
            self.host._task_escalation_manager,
            PROVENANCE_USER,
            True,
        )
        cues = self.host._task_cue_store.snapshot()
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].angle, "ask if useful")
        # Escalation count unchanged by the shadow enrich.
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 1,
        )


class DiscretionaryTierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.host = self.fx.host()
        self.host._init_task_orchestration()
        self.addCleanup(self.host._shutdown_task_orchestration)

    def _event(self, tid: int) -> TaskResultEvent:
        return TaskResultEvent(
            task_id=str(tid),
            session_key="test-user",
            status="done",
            title="search",
            result_summary="found 3",
            notify_aiko=True,
        )

    def test_drop_parks_nothing(self) -> None:
        self.host._maintenance_client = _FakeWorker("drop")
        tid = _make_task(self.host, initiated_by=INITIATED_BY_BACKGROUND)
        event = self._event(tid)
        self.host._run_task_report_decision(
            event,
            self.host._task_cue_store,
            self.host._task_escalation_manager,
            PROVENANCE_SELF,
            False,
        )
        self.assertEqual(self.host._task_cue_store.pending_count(), 0)
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 0,
        )

    def test_park_for_natural_opening_parks_without_arming(self) -> None:
        self.host._maintenance_client = _FakeWorker(
            "park_for_natural_opening", angle="bring it up if it fits",
        )
        tid = _make_task(self.host, initiated_by=INITIATED_BY_BACKGROUND)
        event = self._event(tid)
        self.host._run_task_report_decision(
            event,
            self.host._task_cue_store,
            self.host._task_escalation_manager,
            PROVENANCE_SELF,
            False,
        )
        cues = self.host._task_cue_store.snapshot()
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].angle, "bring it up if it fits")
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 0,
        )

    def test_surface_now_parks_and_arms(self) -> None:
        self.host._maintenance_client = _FakeWorker("surface_now", angle="tell him")
        tid = _make_task(self.host, initiated_by=INITIATED_BY_BACKGROUND)
        event = self._event(tid)
        self.host._run_task_report_decision(
            event,
            self.host._task_cue_store,
            self.host._task_escalation_manager,
            PROVENANCE_SELF,
            False,
        )
        self.assertEqual(self.host._task_cue_store.pending_count(), 1)
        self.assertEqual(
            self.host._task_escalation_manager.pending_count(), 1,
        )

    def test_provenance_self_for_self_initiated_aiko_task(self) -> None:
        tid = _make_task(
            self.host, initiated_by=INITIATED_BY_AIKO, self_initiated=True,
        )
        provenance, is_floor = self.host._task_report_provenance(tid)
        self.assertEqual(provenance, PROVENANCE_SELF)
        self.assertFalse(is_floor)

    def test_provenance_user_for_plain_aiko_task(self) -> None:
        tid = _make_task(self.host, initiated_by=INITIATED_BY_AIKO)
        provenance, is_floor = self.host._task_report_provenance(tid)
        self.assertEqual(provenance, PROVENANCE_USER)
        self.assertTrue(is_floor)


class AngleRenderTests(unittest.TestCase):
    def test_angle_renders_in_cue_block(self) -> None:
        from app.core.tasks.task_cue_store import TaskCueStore

        store = TaskCueStore()
        store.park(
            task_id="1",
            session_key="s",
            kind=CUE_KIND_RESULT,
            title="search",
            status="done",
            summary="found 3 docs",
            angle="ask if he wants the summary",
        )
        block = render_cue_block(store.snapshot())
        self.assertIn("found 3 docs", block)
        self.assertIn("(angle: ask if he wants the summary)", block)

    def test_no_angle_renders_clean(self) -> None:
        from app.core.tasks.task_cue_store import TaskCueStore

        store = TaskCueStore()
        store.park(
            task_id="1",
            session_key="s",
            kind=CUE_KIND_RESULT,
            title="search",
            status="done",
            summary="found 3 docs",
        )
        block = render_cue_block(store.snapshot())
        self.assertIn("found 3 docs", block)
        self.assertNotIn("angle:", block)


if __name__ == "__main__":
    unittest.main()
