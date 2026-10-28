from types import SimpleNamespace as NS

from app.core.concepts.concept_applicability import (
    applicability_state, current_contexts, parse_applicability, scope_hint, scope_sources_valid,
)


def scoped():
    row = NS(content="When code review, explain tradeoffs; except during painting, keep it light.",
             kind="self_tagged", metadata={}, created_at="2026-10-01", event_time=None)
    payload = {"contexts": [{"name": "code review", "memory_id": 1, "quote": "When code review"}],
               "exceptions": [{"name": "painting", "memory_id": 1,
                               "quote": "except during painting"}]}
    return row, parse_applicability(payload, {1: row})


def test_explicit_activity_scopes_and_exceptions_do_not_become_global_traits():
    _, scope = scoped()
    assert scope is not None
    assert applicability_state(
        scope, current_contexts("Let's do code review", scope=scope),
    ) == "matched"
    assert applicability_state(scope, current_contexts("painting", scope=scope)) == "exception"
    assert applicability_state(
        scope, current_contexts("hey there", scope=scope),
    ) == "unknown_context"
    assert "code review" in scope_hint(scope)
    assert current_contexts("Finished code review", scope=scope) == set()


def test_stale_or_different_situation_does_not_supply_an_activity():
    _, scope = scoped()
    snapshot = NS(inferred=NS(status="active", shared_activity="code review"),
                  inferred_stale=False, world_compatible=True)
    assert current_contexts("that one", snapshot, scope=scope) == {"code review"}
    snapshot.inferred_stale = True
    assert current_contexts("that one", snapshot, scope=scope) == set()


def test_source_change_and_deletion_invalidate_a_scoped_rule():
    row, scope = scoped()
    memories = NS(get=lambda _: row)
    assert scope_sources_valid(scope, memories)
    row.content += " Correction: keep it short instead."
    assert not scope_sources_valid(scope, memories)
    assert not scope_sources_valid(scope, NS(get=lambda _: None))


def test_custom_names_must_be_literal_source_qualifiers():
    row, _ = scoped()
    for name in ("all", "debugging", "Code Review", "painting.*", "code"):
        assert parse_applicability({"contexts": [{"name": name, "memory_id": 1,
                                                   "quote": "When code review"}]}, {1: row}) is None
