"""Away-life receipts, interruption and stale-choice regressions."""
from datetime import timedelta
from unittest.mock import patch

from app.core.infra import timephrase
from app.core.world import in_progress_beat
from app.core.world.idle_activity_worker import AWAY_ACTIVITIES_JOURNAL_KEY, load_journal
from test_idle_activity_worker import (
    _FakeItem, _FakeKV, _FakeLoc, _FakeWorldStore, _make_worker,
)


def test_clean_desk_is_not_a_chore_and_wandering_is_independent():
    worker = _make_worker(world=_FakeWorldStore(), kv=_FakeKV())
    candidates = worker._build_candidates("Jacob", timephrase.utcnow()).candidates
    assert "tidy_desk" not in candidates
    assert "Jacob" not in candidates["wander"].summary


def test_bed_rest_does_not_project_sleep():
    worker = _make_worker(world=_FakeWorldStore(locations=[_FakeLoc(1, "bed")]), kv=_FakeKV())
    plan = worker._build_candidates("Jacob", timephrase.utcnow()).candidates["nap"]
    assert plan.activity == "idle"
    assert "nap" not in plan.summary


def test_one_passage_does_not_satisfy_finishing_a_book():
    from app.core.world import day_intention

    book = _FakeItem(1, "Paper Boats", kind="book",
                     state={"title": "Paper Boats", "progress": 2, "total": 4})
    world, kv = _FakeWorldStore(items=[book]), _FakeKV()
    worker = _make_worker(world=world, kv=kv, cooldown=0, day_intention=True)
    worker.force_activity("read_book")
    assert not worker.run().get("closed_intention")
    assert not day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY)).satisfied
    worker.force_activity("read_book")
    assert worker.run()["closed_intention"]
    assert day_intention.load(kv.get(day_intention.DAY_INTENTION_KEY)).satisfied


def test_only_saved_hobby_findings_satisfy_hobby_intention():
    from app.core.world import day_intention
    from test_hobby_worker import _FakeChatDb, _worker, _write_hobby

    now = timephrase.utcnow()
    db = _FakeChatDb()
    _write_hobby(db)
    intention = day_intention.DayIntention(
        day=day_intention.local_day(now), text="explore a question about leaves", beat_key="hobby",
    )
    db.kv_set(day_intention.DAY_INTENTION_KEY, day_intention.dump(intention))
    worker = _worker(db=db)
    worker._research = None
    assert worker.run()["waiting"]
    assert not day_intention.load(db.kv_get(day_intention.DAY_INTENTION_KEY)).satisfied
    worker = _worker(db=db)
    assert worker.run()["advanced"]
    assert day_intention.load(db.kv_get(day_intention.DAY_INTENTION_KEY)).satisfied


def test_scene_changed_during_choice_prevents_old_plan_from_running():
    world, kv = _FakeWorldStore(), _FakeKV()
    worker = _make_worker(world=world, kv=kv, cooldown=0)
    choose = worker._choose_plan

    def moved(*args):
        plan = choose(*args)
        world._scene_id = 2
        return plan

    with patch.object(worker, "_choose_plan", side_effect=moved):
        assert worker.run()["reason"] == "world_changed"
    assert world.set_state_calls == []
    assert load_journal(kv.get) == []


def test_open_steps_keep_receipts_and_failed_journal_can_retry():
    world, kv = _FakeWorldStore(items=[_FakeItem(
        1, "tea pot", slug="tea_pot", state={"fullness": "full"},
    )]), _FakeKV()
    notes = []

    class Writer:
        def write(self, *args, **kwargs):
            notes.append(kwargs)
            return 1

    worker = _make_worker(world=world, kv=kv, cooldown=0,
                          in_progress_ratio=1, pursuit_notes=Writer())
    worker.force_activity("tea")
    assert worker.run()["in_progress"]
    beat = in_progress_beat.load(kv.get)
    assert beat.effects[0]["effect"] == "pour_tea"
    beat.expected_end_at = (timephrase.utcnow() - timedelta(minutes=1)).isoformat()
    in_progress_beat.save(kv.set, beat)
    put = worker._kv_set

    def unavailable(key, value):
        if key == AWAY_ACTIVITIES_JOURNAL_KEY:
            raise OSError("database unavailable")
        put(key, value)

    with patch.object(worker, "_kv_set", side_effect=unavailable):
        assert worker.run()["reason"] == "journal_write_failed"
    assert in_progress_beat.load(kv.get) is not None
    assert notes == []
    assert worker.run()["completed_in_progress"]
    assert in_progress_beat.load(kv.get) is None
    assert notes[0]["extra"]["world_receipts"][0]["effect"] == "pour_tea"
    assert world.get_item(1).state["fullness"] == "half"
