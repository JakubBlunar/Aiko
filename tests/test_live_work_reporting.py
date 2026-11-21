"""Run an actual background calculation through the Live floor and report outbox."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from work_review_helpers import supported_review

from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import LlmRoute
from app.core.live.simulation import _START, _Clock, _SimulationHost
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from app.core.tasks.handlers.fast_tool import FastToolHandler
from app.core.tasks.task_events import TaskEventStore
from app.core.tasks.task_orchestrator import TaskOrchestrator
from app.core.tasks.task_store import TaskStore
from app.core.tasks.work_outcome import task_outcome
from app.core.tasks.work_reports import WorkReports
from app.core.tasks.workflow.fast_tools import shared_fast_skills
from app.core.tasks.workflow.goal_workflow_handler import GoalWorkflowHandler
from app.core.tasks.workflow.skill_registry import WorkflowSkillRegistry
from app.llm.tools.base import ToolRegistry
from app.llm.tools.plugin_tool import PluginFastTool
from app.llm.tools.workflow_tools import StartWorkflowTool
from app.plugins.sdk import PluginApi
from plugins.calculator.entry import define_plugin


_REQUEST = "Work out 6 times 7 in the background and tell me when you have the result."
_ANSWER = "Six times seven is 42."


class _Host(_SimulationHost, TaskOrchestrationMixin):
    def refresh_live_situation(self, **kwargs):
        frame = super().refresh_live_situation(**kwargs)
        while self._pending_policy:
            self._pending_policy.popleft()()
        return frame

    def tick(self, milliseconds=1000):
        self.clock.elapsed_ms += milliseconds
        self._live_heartbeat_tick()

    def work_events(self):
        return [event for event in self._wake_queue if event.source.endswith("work_report")]


@pytest.fixture
def work(tmp_path, monkeypatch):
    db = ChatDatabase(tmp_path / "live-work.db")
    host = _Host(
        _Clock(_START), LlmRoute(provider_id="local_ollama", model="test"),
        None, 6, 2000, learn_timing=False, avatar_profile={}, database=db,
    )
    host._init_live_work_reports()
    shown = []
    host._proactive = SimpleNamespace(present_work_report=lambda *args: shown.append(args))

    plugin_root = Path(__file__).resolve().parents[1] / "plugins" / "calculator"
    monkeypatch.syspath_prepend(str(plugin_root))
    api = PluginApi(plugin_id="calculator", plugin_root=plugin_root)
    define_plugin(api)
    brain_tools = ToolRegistry()
    brain_tools.register(PluginFastTool(api.fast_tools[0]), workflow_read_only=True)
    skills = WorkflowSkillRegistry()
    skills.replace_fast(shared_fast_skills(brain_tools))

    store = TaskStore(db)
    orchestrator = TaskOrchestrator(
        store=store, event_store=TaskEventStore(db), heartbeat_enabled=False,
    )
    host._task_orchestrator = orchestrator
    orchestrator.register_handler(FastToolHandler(lambda *_: brain_tools))

    def model(messages, *, surface, **kwargs):
        if surface in {"workflow_verify", "work_report_review"}:
            return supported_review(messages), None
        if surface == "work_report":
            return json.dumps({"text": _ANSWER, "reaction": "neutral"}), None
        assert surface == "workflow_planner"
        children = [row for row in store.list_for_user(host._user_id) if row.parent_task_id]
        if not children:
            return '{"action":"calculate","args":{"expression":"6 * 7"}}', None
        child, = children
        assert json.loads(child.result["content"]) == {"expression": "6 * 7", "result": 42}
        return json.dumps({
            "action": "finish", "outcome": "success", "findings": _ANSWER,
            "evidence_ids": [child.id],
        }), None

    client = SimpleNamespace(chat_json=model)
    orchestrator.register_handler(GoalWorkflowHandler(
        orchestrator=orchestrator, skill_registry=skills, worker_client_provider=lambda: client,
    ))
    reports = WorkReports(
        db, orchestrator, None, user=lambda: host._user_id, session=lambda: host.session_key,
        client=lambda: client, model=lambda: "test", window=lambda: 8192,
        context=lambda: {"revision": "isolated", "voice": "Speak plainly."},
        enqueue=host._enqueue_work_report, offer_live=host._offer_live_work_report,
        present=host._present_work_report, free=host._brain_loop_free_to_speak,
    )
    host._work_reports = reports
    try:
        yield host, reports, shown
    finally:
        reports.shutdown()
        orchestrator.shutdown()
        db._get_conn().close()


def _start_calculation(host):
    host.publish("user.message_sent", {"text": _REQUEST})
    host._active_turn_user_text = _REQUEST
    result = json.loads(StartWorkflowTool(host).run({"goal": _REQUEST}))
    host.reply("I’ll let you know when it’s ready.")
    assert host._task_orchestrator.wait_for_task(result["task_id"], timeout=8) == "done"
    row = host._task_orchestrator.get(result["task_id"])
    assert task_outcome(row)["verified"], row.result
    assert row.metadata["source_session"] == host.session_key
    assert row.metadata["source_message_id"] > 0
    return row


def _dispatch_one(host):
    event, = host.work_events()
    host._wake_queue.remove(event)
    host._on_task_proactive_event(event)
    return event


@pytest.mark.parametrize("interruption", [
    "typing", "voice", "new_message", "session_switch", "away_at_ready", "round_trip",
    "queued_before_live", "switch_during_prepare",
])
def test_verified_background_work_reports_at_first_safe_opening(work, interruption):
    host, reports, shown = work
    origin = host.session_key
    row = _start_calculation(host)
    if interruption == "typing":
        host.publish("user.typing_started", {})
    if interruption == "away_at_ready":
        host.new_session("other")
    if interruption == "queued_before_live":
        host._settings.agent.behavior_posture = "turn_based"
    if interruption == "switch_during_prepare":
        original_context = reports.context

        def switch_on_context():
            host.new_session("other")
            reports.context = original_context
            return original_context()

        reports.context = switch_on_context
    reports.tick()
    if interruption == "queued_before_live":
        queued, = host.work_events()
        assert queued.source == "work_report"
        host._settings.agent.behavior_posture = "live_presence"
        host.bump_live_mode_generation("scenario-posture")
        host.publish("user.typing_started", {})
        _dispatch_one(host)
    host.tick(3000)

    if interruption in {"typing", "queued_before_live"}:
        assert not host.work_events() and not shown
        host.publish("user.typing_stopped", {})
    if interruption == "voice":
        assert len(host.work_events()) == 1
        host._apply_state({"voice_session": True})
        host.publish("user.voice_start", {})
        _dispatch_one(host)
        assert not shown
        host.publish("user.voice_stop", {})
        host.tick()
    if interruption == "new_message":
        assert len(host.work_events()) == 1
        host.publish("user.message_sent", {"text": "One thing first."})
        _dispatch_one(host)
        assert not shown
        host.reply("Go ahead.")
        host.tick(1999)
        assert not host.work_events()
        host.tick(1)
    if interruption in {"session_switch", "away_at_ready", "round_trip", "switch_during_prepare"}:
        old = None
        if interruption in {"session_switch", "round_trip"}:
            assert len(host.work_events()) == 1
            host.new_session("other")
            if interruption == "round_trip":
                old = host.work_events()[0]
            else:
                _dispatch_one(host)
        host.tick()
        assert not shown and host.work_events() == ([old] if old else [])
        other = host.session_key
        assert not host._chat_db.get_messages(other)
        host.new_session("live")
        if old is not None:
            host._wake_queue.remove(old)
            host._on_task_proactive_event(old)
        if interruption in {"away_at_ready", "switch_during_prepare"}:
            reports.tick()
            host.tick()

    assert not shown
    event = _dispatch_one(host)
    assert event.source == "live_work_report" and event.session_key == origin
    assert len(shown) == 1 and shown[0][0] == origin and "42" in shown[0][1]
    receipt = host._chat_db.execute_fetchone(
        "SELECT session_id,status,assistant_message_id FROM work_reports WHERE task_id=?",
        (row.id,),
    )
    assert receipt == (origin, "committed", shown[0][2])
    assert [msg.content for msg in host._chat_db.get_messages(origin)].count(shown[0][1]) == 1

    host._on_task_proactive_event(event)
    host.new_session("other")
    host.new_session("live")
    reports.tick()
    host.tick()
    assert len(shown) == 1 and not host.work_events()
