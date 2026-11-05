"""Topic organization must follow current source rows, not obsolete sizes/vectors."""
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.conversation.topic_cluster_store import TopicClusterStore
from app.core.conversation.topic_graph import TopicGraph
from app.core.conversation.topic_digest_sources import capture_sources
from tests.test_topic_digest_worker import _worker, _FakeOllama, _agent_settings
from app.core.conversation.topic_label_worker import ClusterLabelWorker
from tests.test_topic_label_worker import _FakeOllama as LabelModel
from tests.test_topic_label_worker import _agent_settings as label_settings


@pytest.fixture
def setup(tmp_path):
    db = ChatDatabase(tmp_path / "topics.db")
    store = MemoryStore(tmp_path / "topics.db")
    for mid in range(6):
        store.add(f"A specific observation {mid}", "fact",
                  np.array([1., 0.] if mid < 3 else [0., 1.], dtype=np.float32),
                  skip_dedupe=True, salience=.9 if mid == 0 else .5)
    graph = TopicGraph(store, similarity=.7, min_cluster_size=2,
                       cluster_store=TopicClusterStore(db))
    graph.rebuild()
    store.add_memory_listener(graph.on_memory_added)
    store.add_update_listener(graph.on_memory_updated)
    yield db, store, graph
    store.close()


def test_real_update_reassigns_new_vector_and_removes_absorbed_influence(setup):
    _, store, graph = setup
    before = graph.cluster_id_for(1)
    destination = graph.cluster_id_for(4)
    assert before != destination
    store.update(1, content="A different subject", embedding=np.array([0., 1.], dtype=np.float32))
    assert graph.cluster_id_for(1) == destination
    store.update(1, metadata={"consolidated_into": 4}, metadata_merge=True)
    assert graph.cluster_id_for(1) is None
    assert store.get(1) is not None
    store.update(2, tier="archive")
    assert graph.cluster_id_for(2) is not None  # Retention tier does not invalidate history.


def test_warm_start_prunes_obsolete_assignments_without_deleting_memories(setup):
    db, store, graph = setup
    store.remove_update_listener(graph.on_memory_updated)
    store.update(1, metadata={"consolidated_into": 2}, metadata_merge=True)
    restored = TopicGraph(store, similarity=.7, min_cluster_size=2,
                           cluster_store=TopicClusterStore(db))
    assert all(1 not in c.member_ids for c in restored.topic_clusters())
    assert 1 not in restored._cluster_store.load_all()[1]
    assert store.get(1) is not None


def test_ann_coverage_ignores_hypothetical_or_absorbed_nearest_hit(setup):
    db, store, _ = setup
    store.update(1, metadata={"consolidated_into": 2}, metadata_merge=True)
    ann = SimpleNamespace(knn_memories=lambda *_, **__: [(1, .99), (2, .85)])
    graph = TopicGraph(store, rag_store=ann, cluster_store=TopicClusterStore(db))
    assert graph.best_match(np.array([1., 0.])) == (.85, 2)
    assert not graph.is_close_to_any_cluster(np.array([1., 0.]), threshold=.9)


def test_digest_refreshes_on_an_edit_even_when_cluster_size_is_unchanged(setup):
    db, store, graph = setup
    model = _FakeOllama("A grounded summary of a specific subject.")
    worker = _worker(db, graph, store, model)
    assert worker.run()["written"] == 2
    old_id = worker.cluster_digest_map[graph.cluster_id_for(1)]
    store.update(1, content="A corrected specific observation")
    assert worker.run()["written"] == 1
    row = store.get(old_id)
    assert row.metadata["source_revisions"]
    assert row.metadata["source_scope"] == "summary_inputs"
    assert worker.run()["reused"] == 2


def test_digest_cannot_commit_if_its_source_changed_during_generation(setup):
    db, store, graph = setup
    cluster = next(c for c in graph.topic_clusters() if 1 in c.member_ids)
    proxy = SimpleNamespace(persistent=True, topic_clusters=lambda: [cluster])

    class EditingModel:
        def chat_stream(self, *_args, **_kwargs):
            store.update(1, content="A changed source during the model call")
            yield json.dumps({"digest": "A summary based on the old wording."})

    worker = _worker(db, proxy, store, EditingModel())
    assert worker.run()["written"] == 0
    assert not list(store.iter_by_kind("topic_digest"))


def test_digest_inputs_keep_roles_event_time_and_only_actual_offered_source_ids(setup):
    _, store, _ = setup
    ids = []
    for index in range(20):
        row = store.add(f"A general public fact number {index}", "knowledge",
                        np.array([1., 0.]), skip_dedupe=True)
        ids.append(row.id)
    plan = store.add("Noah plans a mountain walk", "event", np.array([1., 0.]),
                     temporal_type="future_plan", event_time="2099-01-01T12:00:00Z",
                     skip_dedupe=True)
    draft = store.add("If Noah asks about a mountain walk", "pre_thought", np.array([1., 0.]),
                      skip_dedupe=True)
    cluster = SimpleNamespace(member_ids=[*ids, plan.id, draft.id], representative_id=ids[0])
    sources = capture_sources(cluster, store, limit=4)
    rendered = sources.render(max_chars=200, now=None)
    assert len(sources.ids) == 4
    assert plan.id in sources.ids and draft.id not in sources.ids
    assert "subject=world" in rendered and "subject=user" in rendered
    assert "event_time=2099-01-01" in rendered
    assert all(f"ID {mid} " in rendered for mid in sources.ids)


def test_digest_keeps_an_existing_summary_on_transient_generation_failure(setup):
    db, store, graph = setup
    model = _FakeOllama("A grounded summary of a specific subject.")
    worker = _worker(db, graph, store, model, settings=_agent_settings(topic_digest_max_per_run=1))
    worker.run()
    old_ids = list(worker.cluster_digest_map.values())
    store.update(1, content="A corrected specific observation")
    model.digest = ""
    worker.run()
    assert all(store.get(mid) is not None for mid in old_ids)


def test_topic_labels_refresh_on_edits_but_respect_user_pinned_names(setup):
    db, store, graph = setup
    model = LabelModel("Quiet music")
    worker = ClusterLabelWorker(topic_graph=graph, memory_store=store, ollama=model,
        chat_model="stub", cancel_event=threading.Event(),
        agent_settings=label_settings(), kv_get=db.kv_get, kv_set=db.kv_set)
    assert worker.run()["labeled"] == 2
    store.update(1, content="A corrected observation about the subject")
    assert worker.run()["labeled"] == 1
    assert model.calls == 3
    db.kv_set("aiko.topic_label.1", json.dumps({"label": "My own label", "size": 3,
                                               "user_pinned": True}))
    store.update(1, content="Another corrected observation about the subject")
    worker.run()
    assert model.calls == 3
    assert any(c.summary == "My own label" for c in graph.topic_clusters())


def test_pinned_digest_wording_survives_a_source_refresh(setup):
    db, store, graph = setup
    worker = _worker(db, graph, store, _FakeOllama("A generated subject summary."))
    worker.run()
    cid = graph.cluster_id_for(1)
    old_id = worker.cluster_digest_map[cid]
    store.set_pinned(old_id, True)
    store.update(old_id, content="A hand-curated subject summary", provenance="stated")
    store.update(1, content="A corrected source observation")
    worker.run()
    assert worker.cluster_digest_map[cid] != old_id
    assert store.get(old_id).content == "A hand-curated subject summary"
    assert store.get(old_id).provenance == "stated"


def test_metadata_only_updates_keep_a_singleton_topics_identity(setup):
    _, store, graph = setup
    original = graph.cluster_id_for(3)
    store.update(1, metadata={"consolidated_into": 3}, metadata_merge=True)
    store.update(2, metadata={"consolidated_into": 3}, metadata_merge=True)
    assert graph.cluster_member_ids(original) == [3]
    store.update(3, salience=.95, confidence=.8,
                 metadata={"a_later_annotation": "same subject"}, metadata_merge=True)
    assert graph.cluster_id_for(3) == original
    assert graph._cluster_store.load_all()[1][3] == original


def test_repeated_add_notifications_cannot_move_or_reweight_an_existing_point(setup):
    _, store, graph = setup
    row = store.add("A related point", "fact", np.array([.95, .3], dtype=np.float32),
                    skip_dedupe=True)
    cid = graph.cluster_id_for(row.id)
    centroid = graph._live[cid].centroid.copy()
    members = set(graph.cluster_member_ids(cid))
    for _ in range(3):
        graph.on_memory_added(row)
    assert set(graph.cluster_member_ids(cid)) == members
    np.testing.assert_allclose(graph._live[cid].centroid, centroid)
    group = [store.get(mid) for mid in members]
    np.testing.assert_allclose(centroid, graph._compute_centroid(group), atol=1e-6)


def test_repeated_unassigned_notification_does_not_inflate_refit_demand(setup):
    _, store, graph = setup
    row = store.add("An orthogonal subject", "fact", np.array([-1., 0.]), skip_dedupe=True)
    before = graph.pending_count()
    assert before > 0
    graph.on_memory_added(row)
    assert graph.pending_count() == before


def test_restored_graph_also_preserves_membership_on_metadata_update(setup):
    db, store, _ = setup
    graph = TopicGraph(store, similarity=.7, min_cluster_size=2,
                       cluster_store=TopicClusterStore(db))
    graph.topic_clusters()
    cid = graph.cluster_id_for(1)
    graph.on_memory_added(store.get(1))  # Replayed delivery after warm-start.
    store.get(1).salience = .7
    graph.on_memory_updated(store.get(1))
    assert graph.cluster_id_for(1) == cid
