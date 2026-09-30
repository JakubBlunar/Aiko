from datetime import datetime, timedelta, timezone

import pytest

from app.core.relationship import candor_gate


NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "overrides",
    [
        {"trust": 0.64},
        {"trust": float("nan")},
        {"tenure_days": 29},
        {"tenure_days": float("inf")},
        {"source_kind": "value"},
        {"source_kind": "self"},
        {"definite": False},
        {"last_offered": NOW.isoformat()},
        {"last_offered": (NOW + timedelta(days=1)).isoformat()},
        {"last_offered": "broken"},
        {"last_offered": "2026-09-01T00:00:00"},
    ],
)
def test_candor_fails_closed(overrides):
    inputs = dict(
        trust=0.8, tenure_days=90, source_kind="taste", definite=True,
        now=NOW, last_offered=None,
    )
    inputs.update(overrides)
    assert not candor_gate.eligible(**inputs)


@pytest.mark.parametrize("last_offered", [None, (NOW - timedelta(days=14)).isoformat()])
def test_candor_earned_taste_permission(last_offered):
    assert candor_gate.eligible(
        trust=0.65, tenure_days=30, source_kind="taste", definite=True,
        now=NOW, last_offered=last_offered,
    )
