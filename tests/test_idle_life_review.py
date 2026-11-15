from datetime import datetime, timedelta, timezone

from app.core.world.idle_life_review import review_events

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


def event(eid, kind, payload, *, actor="autonomous", start=NOW, end=None):
    return {
        "id": eid,
        "kind": kind,
        "payload": payload,
        "actor": actor,
        "worker": "away_activity",
        "run_id": str(eid),
        "recorded_at": start.isoformat(),
        "last_seen_at": (end or start).isoformat(),
        "occurred_at": start.isoformat(),
    }


def action(eid, name, instance="1:batch1", **kwargs):
    return event(
        eid, "action_effect", {"action": name, "item_id": 1, "drink_instance": instance}, **kwargs
    )


def test_reheating_flags_survive_later_progress_but_batches_do_not_merge():
    rows = [
        action(1, "reheat_drink"),
        action(2, "reheat_drink"),
        action(3, "reheat_drink"),
        action(4, "sip_drink"),
        action(5, "reheat_drink", "1:batch2"),
    ]
    flags = review_events(rows)["flags"]
    assert flags[0]["kind"] == "reheating_without_sipping"
    assert flags[0]["event_ids"] == [1, 2, 3]
    assert not review_events(
        [
            action(1, "reheat_drink", "1:a"),
            action(2, "reheat_drink", "1:b"),
            action(3, "reheat_drink", "1:c"),
        ]
    )["flags"]


def test_conversation_actions_and_productive_revisions_do_not_trigger_loops():
    rows = [action(i, "reheat_drink", actor="conversation") for i in range(20)]
    rows += [
        event(
            50 + i,
            "step_completed",
            {"key": "writing", "receipt": {"effect": "save_creation", "revision": i + 1}},
        )
        for i in range(6)
    ]
    report = review_events(rows)
    assert not report["flags"]
    assert report["autonomous_steps"] == 6


def test_home_steps_are_not_double_counted_and_resource_ledgers_stay_separate():
    delta = [
        {
            "item_id": 1,
            "fields": {"state.home_vessel.drink.amount_ml": {"before": 250, "after": 175}},
        }
    ]
    rows = [
        event(1, "action_effect", {"action": "sip_drink", "resource_changes": delta}),
        event(
            2,
            "step_completed",
            {"key": "home_sip_drink", "receipt": {"mode": "simulated_home_action"}},
        ),
        event(3, "run_finished", {"resource_changes": delta}),
    ]
    report = review_events(rows)
    assert report["autonomous_steps"] == 1
    assert report["canonical_home_resource_deltas"][0]["net_change"] == -75
    assert report["observed_worker_resource_deltas"][0]["net_change"] == -75


def test_long_hold_requires_observed_continuity_and_does_not_bridge_offline_gap():
    rows = [
        event(
            i,
            "admission",
            {
                "reason": "active_shared_conversation_situation",
                "hold_owner": {"situation_generation": 1, "situation_source_message_id": 10},
            },
            start=NOW + timedelta(minutes=5 * i),
            end=NOW + timedelta(minutes=5 * i + 4),
        )
        for i in range(75)
    ]
    assert any(flag["kind"] == "long_shared_hold" for flag in review_events(rows)["flags"])
    separated = rows[:30] + rows[50:]
    assert not review_events(separated)["flags"]


def test_virtual_time_analysis_is_explicit_and_not_recording_time():
    row = event(
        1,
        "admission",
        {
            "reason": "active_shared_conversation_situation",
            "last_narrative_at": (NOW + timedelta(hours=7)).isoformat(),
        },
    )
    assert not review_events([row])["flags"]
    assert review_events([row], time_basis="narrative")["flags"][0]["kind"] == "long_shared_hold"


def test_incomplete_means_no_terminal_event_not_an_empty_success():
    start = event(1, "run_started", {})
    finished = event(2, "run_finished", {})
    finished["run_id"] = start["run_id"]
    assert review_events([start])["incomplete_run_event_ids"] == [1]
    assert review_events([start, finished])["incomplete_run_event_ids"] == []


def test_coverage_distinguishes_observation_from_restart_and_unknown_time():
    from app.core.world.idle_life_review import coverage_summary

    report = coverage_summary(
        [
            (NOW, NOW + timedelta(minutes=4), "boot1"),
            (NOW + timedelta(minutes=5), NOW + timedelta(minutes=9), "boot1"),
            (NOW + timedelta(hours=2), NOW + timedelta(hours=2, minutes=4), "boot2"),
        ],
        cutoff=NOW - timedelta(hours=1),
        window_end=NOW + timedelta(hours=3),
    )
    assert report["unknown_span_count"] == 3
    assert report["observed_scheduler_seconds"] == 13 * 60
    assert all("unknown" in row["status"] for row in report["unknown_spans"])
