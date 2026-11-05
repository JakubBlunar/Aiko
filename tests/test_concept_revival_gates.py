"""Grounded revival uses the admission bar without consuming delayed support."""
from datetime import timedelta

import pytest

from tests.test_concept_lifecycle_worker import _NOW, _add, _harness, _iso, _settings


@pytest.mark.parametrize("status", ["dormant", "contradicted"])
@pytest.mark.parametrize("kind,confidence,sources,age", [
    ("value", .65, 4, 10),
    ("generalization", .65, 4, 10),
    ("identity", .9, 2, 10),
    ("value", .9, 4, .5),
])
def test_fresh_support_does_not_bypass_kind_gate(status, kind, confidence, sources, age):
    h = _harness(with_clock=False)
    concept = _add(h.store, status=status, kind=kind, confidence=confidence,
                   distinct_source_count=sources, first_evidence_at=_iso(age),
                   last_lifecycle_at=_iso(2), last_reinforced_at=_iso(1))
    assert h.worker._transition(concept, _NOW, confidence) == (status, "")


@pytest.mark.parametrize("status", ["dormant", "contradicted"])
def test_revival_obeys_stricter_configured_source_bar(status):
    h = _harness(_settings(concept_promote_min_sources=5), with_clock=False)
    concept = _add(h.store, status=status, kind="value", distinct_source_count=4,
                   first_evidence_at=_iso(10), last_lifecycle_at=_iso(2),
                   last_reinforced_at=_iso(1))
    assert h.worker._transition(concept, _NOW, .9) == (status, "")


@pytest.mark.parametrize("status", ["dormant", "contradicted"])
@pytest.mark.parametrize("kind,sources", [("value", 3), ("boundary", 1)])
def test_supported_revival_retains_kind_exceptions(status, kind, sources):
    h = _harness(with_clock=False)
    concept = _add(h.store, status=status, kind=kind, distinct_source_count=sources,
                   first_evidence_at=_iso(10), last_lifecycle_at=_iso(2),
                   last_reinforced_at=_iso(1))
    assert h.worker._transition(concept, _NOW, .9) == ("active", "revived")


def test_age_delayed_support_survives_evaluation_and_restart():
    h = _harness(with_clock=False)
    concept = _add(h.store, status="dormant", kind="value", confidence=.85,
                   distinct_source_count=4, first_evidence_at=_iso(.5),
                   last_lifecycle_at=_iso(.2), last_reinforced_at=_iso(.1))
    h.worker.run()
    assert concept.status == "dormant"
    assert h.worker._has_revival_support(concept)
    # Reload the store from SQLite, as after a restart, rather than preserving
    # support only in the worker's Python object.
    from app.core.concepts.concept_store import ConceptStore

    h.worker._store = ConceptStore(h.db)
    h.worker._store.load_all()
    h.worker._clock = lambda: _NOW + timedelta(days=2)
    h.worker.run()
    restored = h.worker._store.get(concept.concept_id)
    assert restored.status == "active"
    assert not h.worker._store.support.review(concept.concept_id)["pending"]
    assert [e.event_type for e in h.events.list()] == ["revived"]


def test_rejected_same_support_is_not_reopened_by_gate_deferral():
    h = _harness(with_clock=False)
    concept = _add(h.store, status="contradicted", last_reinforced_at=_iso(1))
    review = {"support_at": concept.last_reinforced_at, "pending": False,
              "outcome": "assessed_no_support", "reason": "disproof_still_valid"}
    h.store.support.record_review(concept.concept_id, review)
    h.worker._retain_revival_support(concept)
    assert h.store.support.review(concept.concept_id) == review
