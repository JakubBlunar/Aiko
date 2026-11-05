"""Exploration needs a purpose and an appropriate source, not just sparse notes."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.proactive.associative_wander_worker import AssociativeWanderWorker, WanderPair
from app.core.proactive.cue_quality import connection_usable, inventory_demand, seed_usable
from app.core.proactive.idle_worker import WorkSignal
from app.core.proactive.topic_exploration import explorable_topic, payload_explorable
from app.core.live.cue_adapter import CueUrgeAdapter
from app.core.live.urge_store import LiveUrgeStore


@pytest.mark.parametrize("label,kinds", [
    ("tender bedtime cuddles", ("shared_moment", "shared_moment", "diary")),
    ("Rowan's promises", ("promise", "promise")),
    ("a quiet shared evening", ("shared_moment", "reflection")),
    ("An old event with a cut-off subject…", ("event",)),
])
def test_a_personal_scene_is_not_an_external_knowledge_gap(label, kinds):
    assert not explorable_topic(SimpleNamespace(summary=label, member_kinds=kinds))


def test_a_real_research_question_can_still_explore_a_personal_subject():
    cluster = SimpleNamespace(summary="why cuddling feels soothing",
                              member_kinds=("shared_moment", "knowledge_gap"))
    assert explorable_topic(cluster)
    assert explorable_topic(SimpleNamespace(summary="javascript promises", member_kinds=("fact",)))


def test_current_topic_validation_uses_the_bounded_lookup():
    class Graph:
        def topic_clusters_for_labels(self, labels):
            assert labels == {"coffee extraction"}
            return [SimpleNamespace(summary="coffee extraction", member_kinds=("knowledge",))]

        def topic_clusters(self):
            raise AssertionError("must not scan the entire memory graph on the turn path")

    payload = {"exploration_version": 1, "topic": "coffee extraction"}
    assert payload_explorable("knowledge_gap_notice", payload, Graph())


def test_stale_automatic_text_cannot_keep_claiming_ignorance():
    assert not payload_explorable("knowledge_gap_notice", {"topic": "coffee", "at": "old"})
    assert not seed_usable({"source": "llm", "why": "unexplained novelty"})
    assert not connection_usable({"connection": "both have a rhythm"})
    assert seed_usable({"source": "manual"})


@pytest.mark.parametrize("response", [
    {"connection": "both reward patience", "useful_because": "Try following a small clue."},
    {"connects": "true", "connection": "both reward patience", "useful_because": "A useful try."},
    {"connects": True, "connection": "both reward patience"},
    {"connects": True, "connection": ["stringified garbage"], "useful_because": "A useful try."},
    {"connects": True, "connection": "I could try it today", "useful_because": "A useful try."},
])
def test_association_requires_an_explicit_accepted_connection_and_use(response):
    model = SimpleNamespace(chat_json=lambda *_, **__: (json.dumps(response), None))
    worker = AssociativeWanderWorker(
        topic_graph_provider=lambda: None, memory_store=SimpleNamespace(),
        kv_get=lambda _: None, kv_set=lambda *_: None, ollama=model, model="test",
    )
    pair = WanderPair(1, 2, "following a trail", "reading a mystery", 0.3, "pair")
    assert worker._compose_connection(pair, [], []) == ""


def test_one_external_note_does_not_reclassify_a_scene_collection():
    cluster = SimpleNamespace(summary="tender bedtime cuddles",
                              member_kinds=("shared_moment",) * 20 + ("knowledge",))
    assert not explorable_topic(cluster)


@pytest.mark.parametrize("cue_type,payload", [
    ("curiosity_seed", {"source": "llm"}),
    ("associative_wander", {"connection": "a vague similarity"}),
    ("knowledge_gap_notice", {"at": "old", "topic": "coffee"}),
])
def test_live_projection_does_not_reintroduce_unreviewed_automatic_cues(cue_type, payload):
    row = SimpleNamespace(id=1, cue_type=cue_type, payload=payload, subject="coffee")
    adapter = CueUrgeAdapter(pending_provider=lambda: [row])
    assert adapter.project(LiveUrgeStore(), now_mono_ms=1000) == ()


@pytest.mark.parametrize("needs_llm", [False, True])
def test_unreviewed_full_inventory_can_be_replaced_without_mutating_during_demand(needs_llm):
    signal = WorkSignal(pressure=0, reason="6/6 stocked", needs_llm=needs_llm)
    rows = [SimpleNamespace(payload={"source": "llm"})]
    producer = SimpleNamespace(demand=lambda **_: signal, stock_rows=lambda **_: rows)
    demand = inventory_demand(producer, seed_usable, needs_llm=needs_llm)
    assert demand.pressure > 0
    assert demand.reason == "unreviewed_stock"
    assert demand.needs_llm == needs_llm
    assert rows[0].payload == {"source": "llm"}
    rows[0].payload["quality"] = {"version": 1, "useful_because": "Explore an open question"}
    assert inventory_demand(producer, seed_usable, needs_llm=needs_llm) is signal
