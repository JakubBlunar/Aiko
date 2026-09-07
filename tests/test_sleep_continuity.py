from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.debug_clock import DebugClock
from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler
from app.core.session.inner_life_part2 import InnerLifePart2Mixin
from app.core.session.post_turn_mixin import PostTurnMixin
from app.core.session.turn_runner import TurnRunner
from app.core.world.sleep_lifecycle_worker import SleepLifecycleWorker
from app.core.services.response_text_service import (
    extract_sleep_decisions,
    safe_visible_prefix,
    strip_all_meta_tags,
)
from app.core.world.sleep_state import (
    ASLEEP,
    AWAKE,
    NAP,
    WINDING_DOWN,
    WOKEN,
    InvalidSleepTransition,
    PropensityInputs,
    evaluate_propensity,
    initial_state,
    transition,
)
from app.core.world.sleep_store import SleepGenerationConflict, SleepStore


UTC = timezone.utc


def _store(tmp_path: Path) -> SleepStore:
    return SleepStore(ChatDatabase(tmp_path / "chat.db"))


def test_state_machine_keeps_wake_distinct_from_fully_awake() -> None:
    now = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    state = transition(initial_state(now), "wind_down", now=now, sleep_kind="overnight")
    assert state.status == WINDING_DOWN
    state = transition(state, "fall_asleep", now=now)
    assert state.status == ASLEEP
    state = transition(state, "wake", now=now)
    assert state.status == WOKEN
    state = transition(state, "fully_awake", now=now)
    assert state.status == AWAKE


def test_sleep_talk_advances_generation_without_waking() -> None:
    now = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    state = transition(initial_state(now), "wind_down", now=now)
    state = transition(state, "fall_asleep", now=now)
    next_state = transition(state, "stay_asleep", now=now + timedelta(minutes=1))
    assert next_state.status == ASLEEP
    assert next_state.generation == state.generation + 1


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(InvalidSleepTransition):
        transition(initial_state(), "fully_awake")


def test_overnight_and_nap_propensity_have_distinct_gates() -> None:
    overnight = evaluate_propensity(
        PropensityInputs(0.2, 0.15, 1.0, 12.0, "late_night")
    )
    assert overnight.should_wind_down
    assert overnight.sleep_kind == "overnight"

    nap = evaluate_propensity(
        PropensityInputs(0.18, 0.72, 1.0, 8.0, "day")
    )
    assert nap.should_wind_down
    assert nap.sleep_kind == NAP

    no_nap = evaluate_propensity(
        PropensityInputs(0.18, 0.72, 1.0, 8.0, "day", naps_enabled=False)
    )
    assert not no_nap.should_wind_down


def test_shared_situation_vetoes_even_high_propensity() -> None:
    decision = evaluate_propensity(
        PropensityInputs(
            0.05,
            0.05,
            5.0,
            20.0,
            "late_night",
            shared_situation_active=True,
        )
    )
    assert not decision.should_wind_down
    assert decision.reason_code == "shared_situation"


def test_store_persists_episode_interruption_and_generation_cas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.get_state()
    state = store.transition(
        "wind_down",
        expected_generation=state.generation,
        sleep_kind="overnight",
        reason_code="tired",
        previous_world={"location_slug": "desk", "activity": "reading"},
    )
    state = store.transition("fall_asleep", expected_generation=state.generation)
    state, episode = store.record_interruption(
        session_id="chat-a", message_id=7, message_text="Aiko?"
    )
    assert state.status == ASLEEP
    assert episode is not None
    assert episode.interruptions[0]["message_id"] == 7

    with pytest.raises(SleepGenerationConflict):
        store.transition("wake", expected_generation=0)

    reloaded = SleepStore(ChatDatabase(tmp_path / "chat.db"))
    assert reloaded.get_state().status == ASLEEP
    assert reloaded.current_episode() is not None


def test_episode_outputs_are_reserved_and_finished_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.get_state()
    state = store.transition("wind_down", expected_generation=state.generation)
    state = store.transition("fall_asleep", expected_generation=state.generation)
    episode_id = state.current_episode_id
    assert episode_id is not None
    assert store.reserve_episode_output(episode_id, kind="dream")
    assert not store.reserve_episode_output(episode_id, kind="dream")
    assert store.finish_episode_output(episode_id, kind="dream", memory_id=42)
    assert store.get_episode(episode_id).dream_memory_id == 42


def test_reconcile_expires_only_recorded_nap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)
    state = store.get_state()
    state = store.transition(
        "wind_down",
        expected_generation=state.generation,
        now=now - timedelta(hours=3),
        sleep_kind="nap",
    )
    state = store.transition(
        "fall_asleep",
        expected_generation=state.generation,
        now=now - timedelta(hours=3),
    )
    reconciled = store.reconcile(now=now, nap_max_hours=2)
    assert reconciled.status == WOKEN


def test_nap_maximum_counts_across_an_interruption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)
    state = store.get_state()
    state = store.transition(
        "wind_down",
        expected_generation=state.generation,
        now=start,
        sleep_kind="nap",
    )
    state = store.transition(
        "fall_asleep", expected_generation=state.generation, now=start
    )
    state = store.transition(
        "wake",
        expected_generation=state.generation,
        now=start + timedelta(hours=1),
    )
    store.transition(
        "back_to_sleep",
        expected_generation=state.generation,
        now=start + timedelta(hours=1, minutes=10),
    )
    reconciled = store.reconcile(
        now=start + timedelta(hours=2, minutes=1),
        nap_max_hours=2,
    )
    assert reconciled.status == WOKEN


def test_debug_clock_advances_sleep_reconciliation(tmp_path: Path) -> None:
    clock = DebugClock(enabled=True)
    clock.install()
    try:
        store = _store(tmp_path)
        now = timephrase.utcnow()
        state = store.get_state()
        state = store.transition(
            "wind_down",
            expected_generation=state.generation,
            now=now,
            sleep_kind="nap",
        )
        store.transition(
            "fall_asleep",
            expected_generation=state.generation,
            now=now,
        )
        clock.advance(hours=3)
        assert store.reconcile(nap_max_hours=2).status == WOKEN
    finally:
        clock.uninstall()
        timephrase.set_now_provider(None)


def test_sleep_tag_is_held_stripped_and_parsed() -> None:
    raw = "...mm. [[sleep:stay_asleep]]"
    assert extract_sleep_decisions(raw) == ["stay_asleep"]
    assert strip_all_meta_tags(raw).strip() == "...mm."
    assert safe_visible_prefix("...mm. [[sleep:stay") == "...mm. "


def test_first_interruption_prompt_contains_authoritative_sleep_facts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = store.get_state()
    state = store.transition(
        "wind_down",
        expected_generation=state.generation,
        sleep_kind="overnight",
        reason_code="circadian_low",
        previous_world={"location_slug": "desk", "activity": "reading"},
    )
    store.transition("fall_asleep", expected_generation=state.generation)
    store.record_interruption(
        session_id="chat-a", message_id=3, message_text="Aiko, wake up"
    )
    host = object.__new__(type("_PromptHost", (InnerLifePart2Mixin,), {}))
    host._sleep_store = store
    host._settings = SimpleNamespace(agent=SimpleNamespace(sleep_enabled=True))
    host._active_sleep_context = store.snapshot()
    host._active_turn_user_text = "Aiko, wake up"
    block = host._render_sleep_state_block()
    assert "state=asleep" in block
    assert "interruptions=1" in block
    assert "activity=reading" in block
    assert "[[sleep:stay_asleep]]" in block


def test_sleep_return_uses_only_a_completed_recorded_episode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
    state = store.get_state()
    state = store.transition(
        "wind_down",
        expected_generation=state.generation,
        now=now - timedelta(hours=6),
        sleep_kind="overnight",
        reason_code="circadian_low",
    )
    state = store.transition(
        "fall_asleep",
        expected_generation=state.generation,
        now=now - timedelta(hours=6),
    )
    state = store.transition(
        "wake",
        expected_generation=state.generation,
        now=now,
    )
    store.transition(
        "fully_awake",
        expected_generation=state.generation,
        now=now,
    )
    host = object.__new__(type("_ReturnHost", (InnerLifePart2Mixin,), {}))
    host._sleep_store = store
    host._chat_db = store._db
    host._settings = SimpleNamespace(
        agent=SimpleNamespace(sleep_return_enabled=True)
    )
    host._gap_cue_surfaced = False
    host._pending_sleep_return_seconds = None
    block = host._render_sleep_return_block()
    assert "RECORDED SLEEP CONTINUITY" in block
    assert "6.0 hours" in block
    assert host._gap_cue_surfaced is True
    assert host._render_sleep_return_block() == ""


def test_post_turn_sleep_talk_decision_preserves_asleep_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.get_state()
    state = store.transition("wind_down", expected_generation=state.generation)
    store.transition("fall_asleep", expected_generation=state.generation)
    store.record_interruption(
        session_id="chat-a", message_id=4, message_text="Aiko?"
    )
    host = object.__new__(type("_PostTurnHost", (PostTurnMixin,), {}))
    host._sleep_store = store
    host._world_store = None
    host.session_key = "chat-a"
    result = host._apply_sleep_decision(
        raw_assistant_text="...mm? [[sleep:stay_asleep]]",
        assistant_text="...mm?",
        assistant_message_id=5,
    )
    assert result == "stay_asleep"
    assert store.get_state().status == ASLEEP
    episode = store.current_episode()
    assert episode is not None
    assert episode.interruptions[-1]["decision"] == "stay_asleep"


def test_diary_tags_are_rejected_while_asleep_without_blocking_other_memory(
    tmp_path: Path,
) -> None:
    del tmp_path

    class _MemoryStore:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        def add(self, **kwargs):
            self.rows.append(kwargs)
            return SimpleNamespace(id=len(self.rows))

    runner = object.__new__(TurnRunner)
    runner._memory_store = _MemoryStore()
    runner._embedder = SimpleNamespace(embed=lambda _text: [0.1])
    runner._diary_allowed_provider = lambda: False
    runner._self_tagged_salience = 0.7
    runner._on_memory_added = None
    runner._extract_diary_memories(
        "[[diary:I was interrupted during sleep.]]",
        session_key="chat-a",
        assistant_message_id=6,
    )
    assert runner._memory_store.rows == []
    runner._extract_self_tagged_memories(
        "[[remember:self:I prefer the quiet.]]",
        session_key="chat-a",
        assistant_message_id=6,
    )
    assert runner._memory_store.rows[0]["kind"] == "self"


def _sleep_settings(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        sleep_enabled=enabled,
        sleep_wind_down_minutes=5.0,
        sleep_nap_max_hours=2.0,
        sleep_back_to_sleep_minutes=20.0,
        sleep_wake_energy_threshold=0.48,
        sleep_max_woken_minutes=90.0,
    )


def test_lifecycle_rechecks_world_guard_before_falling_asleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 8, 2, 0, tzinfo=UTC)
    state = store.get_state()
    store.transition(
        "wind_down",
        expected_generation=state.generation,
        now=now - timedelta(minutes=10),
    )
    guard = SimpleNamespace(
        check_autonomous=lambda *_args, **_kwargs: SimpleNamespace(
            allowed=False,
            reason="active_shared_conversation_situation",
        )
    )
    worker = SleepLifecycleWorker(
        sleep_store=store,
        chat_db=store._db,
        world_store=None,
        world_guard=guard,
        idle_depth_provider=lambda: 3600.0,
        settings_provider=lambda: _sleep_settings(),
    )
    monkeypatch.setattr(timephrase, "now", lambda: now)
    result = worker.run()
    assert result["status"] == AWAKE
    assert store.list_episodes(limit=1)[0].outcome == (
        "active_shared_conversation_situation"
    )


def test_disabling_sleep_closes_an_active_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 8, 2, 0, tzinfo=UTC)
    state = store.get_state()
    state = store.transition("wind_down", expected_generation=state.generation, now=now)
    store.transition("fall_asleep", expected_generation=state.generation, now=now)
    worker = SleepLifecycleWorker(
        sleep_store=store,
        chat_db=store._db,
        world_store=None,
        world_guard=None,
        idle_depth_provider=lambda: 0.0,
        settings_provider=lambda: _sleep_settings(enabled=False),
    )
    monkeypatch.setattr(timephrase, "now", lambda: now + timedelta(minutes=1))
    assert worker.run()["status"] == AWAKE
    episode = store.list_episodes(limit=1)[0]
    assert episode.ended_at is not None
    assert episode.outcome == "disabled"


class _Worker:
    def __init__(self, name: str, policy: str | None = None) -> None:
        self.name = name
        if policy is not None:
            self.sleep_policy = policy
        self.interval_seconds = 1.0
        self.runs = 0

    def is_ready(self, **_kwargs) -> bool:
        return True

    def run(self) -> dict[str, int]:
        self.runs += 1
        return {"runs": self.runs}


def test_scheduler_pauses_world_producers_but_keeps_maintenance() -> None:
    status = {"value": "asleep"}
    scheduler = IdleWorkerScheduler(
        wake_seconds=1,
        sleep_state_provider=lambda: status["value"],
        pressure_enabled=False,
    )
    diary = _Worker("diary")
    vitality = _Worker("vitality")
    dream = _Worker("sleep_dream", "sleep_only")
    scheduler.register(diary)
    scheduler.register(vitality)
    scheduler.register(dream)
    scheduler._tick()
    assert diary.runs == 0
    assert vitality.runs == 1
    assert dream.runs == 1
    report = scheduler.get_status()
    rows = {row["name"]: row for row in report["workers"]}
    assert rows["diary"]["sleep_suppressed"] is True

    status["value"] = "woken"
    scheduler._tick()
    assert diary.runs == 0
    assert dream.runs == 1

    status["value"] = "awake"
    scheduler._tick()
    assert diary.runs == 1
    assert dream.runs == 1


def test_sleep_state_tables_exist_on_schema_v43(tmp_path: Path) -> None:
    db = ChatDatabase(tmp_path / "chat.db")
    tables = {
        str(row[0])
        for row in db._get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"sleep_state", "sleep_episodes"} <= tables
    version = db._get_conn().execute("SELECT version FROM schema_version").fetchone()
    assert int(version[0]) >= 43


def test_reason_with_relative_time_is_not_frozen() -> None:
    state = transition(
        initial_state(),
        "wind_down",
        reason_text="I am tired tonight",
    )
    assert state.reason_text == ""
