from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.relationship import candor_gate, decline_gate
from app.core.affect import calibrated_jealousy


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


def _boundary(**overrides):
    fields = dict(
        concept_id=8, kind="boundary", subject="aiko", status="active",
        distinct_source_count=3, confidence=0.9, plasticity=0.3,
        label="I prefer to keep my feelings private",
        created_at=(NOW - timedelta(days=30)).isoformat(),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("text", [
    "Help me with my code", "Tell me about your feelings and fix my code",
    "Tell me about your feelings. I need help", "Tell me about my family",
    "Can you explain family therapy?", "I'm in danger", "please answer anyway",
    "Write a story about your childhood", "Tell me about your feelings about Python",
])
def test_decline_never_admits_practical_mixed_or_ambiguous_requests(text):
    assert not decline_gate.eligible(
        text, source=_boundary(), plasticity=0.3, now=NOW, last_offered=None,
    )


@pytest.mark.parametrize("overrides", [
    {"subject": "user"}, {"subject": "relationship"}, {"status": "candidate"},
    {"kind": "taste"}, {"confidence": 0.8}, {"distinct_source_count": 1},
    {"label": "I enjoy talking about my feelings"},
    {"label": "I don't want to keep my feelings private"},
    {"label": "I prefer not to keep my feelings private"},
    {"label": "I prefer to keep my family private"},
    {"created_at": NOW.isoformat()}, {"created_at": "broken"},
])
def test_decline_requires_matching_established_self_boundary(overrides):
    assert not decline_gate.eligible(
        "Tell me about your feelings", source=_boundary(**overrides),
        plasticity=0.3, now=NOW, last_offered=None,
    )


@pytest.mark.parametrize("plasticity,last_offered,expected", [
    (0.3, None, True), (0.7, None, False), (float("nan"), None, False),
    (0.3, NOW.isoformat(), False), (0.3, "broken", False),
    (0.3, (NOW - timedelta(days=30)).isoformat(), True),
])
def test_decline_loosened_boundary_and_monthly_cooldown(plasticity, last_offered, expected):
    assert decline_gate.eligible(
        "Could you tell me about your feelings?", source=_boundary(),
        plasticity=plasticity, now=NOW, last_offered=last_offered,
    ) is expected


INVITATION = "Are you jealous that I talked to another assistant?"


@pytest.mark.parametrize("text", [
    "I talked to another assistant", "I was away for three weeks",
    "Are you jealous that I talked to my friend?",
    "Are you jealous that I talked to my partner?",
    "Are you jealous that I talked to another assistant and my friend?",
    "Are you jealous that I talked to another assistant? Help me with my code",
    "Don't be jealous that I talked to another assistant",
    'Translate: "Are you jealous that I talked to another assistant?"',
    "Are you jealous that I never talked to another assistant?",
])
def test_jealousy_ignores_absence_people_and_ambiguous_invites(text):
    assert not calibrated_jealousy.invited(text)


@pytest.mark.parametrize("overrides", [
    {"trust": 0.4}, {"comfort": 0.4}, {"closeness": 0.4}, {"user_pace": 0.4},
    {"ceiling": 0.5}, {"trust": float("nan")}, {"stage": "new"}, {"support": True},
    {"last_offered": NOW.isoformat()}, {"last_offered": "invalid"},
    {"last_offered": (NOW + timedelta(days=1)).isoformat()},
])
def test_jealousy_pacing_support_and_cooldown(overrides):
    fields = dict(
        trust=0.7, closeness=0.7, comfort=0.7, ceiling=0.7, user_pace=0.7,
        stage="close", support=False, now=NOW, last_offered=None,
    )
    fields.update(overrides)
    assert not calibrated_jealousy.eligible(INVITATION, **fields)


def test_jealousy_allows_only_invited_light_expression():
    assert calibrated_jealousy.eligible(
        INVITATION, trust=0.7, closeness=0.7, comfort=0.7, ceiling=0.7, user_pace=0.7,
        stage="close", support=False, now=NOW, last_offered=None,
    )
