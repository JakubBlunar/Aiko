"""Expiry retires weak interpretations, while keeping evidence and a way back."""
from datetime import timedelta

import pytest

from app.core.concepts.concept_candidate_retirement import candidate_retirement_reason
from scripts.concept_candidate_retirement_report import collect
from tests.test_concept_lifecycle_worker import _NOW, _add, _harness, _iso


def reason(**overrides):
    values = dict(age_days=50, ttl_days=21, sources=4, min_sources=2,
                  confidence=.1, retire_floor=.15, quiet_wall_days=40)
    return candidate_retirement_reason(**(values | overrides))


@pytest.mark.parametrize("override", [
    {"age_days": 20}, {"quiet_wall_days": 20}, {"quiet_wall_days": None},
    {"confidence": .15}, {"confidence": .8}, {"ttl_days": 0},
])
def test_protect_developing_recent_supported_and_strong_candidates(override):
    assert reason(**override) is None


def test_exact_age_and_quiet_boundaries_and_strict_confidence_floor():
    assert reason(age_days=21, quiet_wall_days=21) == "weak_without_recent_support"
    assert reason(confidence=.15) is None


def test_old_thin_source_route_also_gives_new_support_time_to_develop():
    assert reason(sources=1, confidence=.5) == "insufficient_sources"
    assert reason(sources=1, quiet_wall_days=1) is None


def test_weak_candidate_retires_without_deletion_and_can_return_on_new_support():
    h = _harness(with_clock=False)
    c = _add(h.store, confidence=.05, distinct_source_count=4,
             created_at=_iso(60), first_evidence_at=_iso(60),
             last_lifecycle_at=_iso(1), last_reinforced_at=_iso(50))
    h.worker.run()
    assert c.status == "retired"
    assert h.store.get(c.concept_id) is c
    assert h.db._get_conn().execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 1
    assert "Supporting memories and history are retained" in h.events.list()[0].reason
    h.worker._clock = lambda: _NOW + timedelta(days=1)
    c.last_reinforced_at = (_NOW + timedelta(hours=12)).isoformat()
    h.store.update(c)
    h.worker.run()
    assert c.status == "active"
    assert {e.event_type for e in h.events.list()} == {"retired", "revived"}


@pytest.mark.parametrize("sources", [1, 4])
def test_newly_inferred_candidate_from_old_memories_gets_a_quiet_grace_period(sources):
    h = _harness(with_clock=False)
    c = _add(h.store, confidence=.05, distinct_source_count=sources,
             created_at=_iso(1), first_evidence_at=_iso(60),
             last_lifecycle_at=_iso(.5))
    h.worker.run()
    assert c.status == "candidate"


def test_eligibility_report_uses_same_rule_and_does_not_write():
    h = _harness(with_clock=False)
    old = _add(h.store, confidence=.05, distinct_source_count=4,
               created_at=_iso(60), first_evidence_at=_iso(60), last_reinforced_at=_iso(50))
    _add(h.store, confidence=.05, created_at=_iso(60), first_evidence_at=_iso(60),
         last_reinforced_at=_iso(1))
    # Report the wall-clock fallback used by this harness.
    h.settings.engagement_clock_enabled = False
    connection = h.db._get_conn()
    before = connection.total_changes
    report = collect(connection, h.settings, now=_NOW)
    assert report["candidates"] == 2
    assert report["eligible"] == 1
    assert report["records"][0]["id"] == old.concept_id
    assert h.worker._is_stale_candidate(old, _NOW)
    assert connection.total_changes == before
    assert old.status == "candidate"
