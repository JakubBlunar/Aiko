"""L24 concept integration contract: ``ConceptView`` facade + kind routing.

Covers the single read path (``core`` / ``relevant`` / ``for_target`` /
``for_cluster`` / ``evidence_labels``), subject/kind/confidence filtering,
empty + missing-dep degradation, and the authoritative
``surfacing_targets`` -> ``kinds_for_target`` routing (identity feeds
``profile_block`` for the user, ``self_image_block`` for Aiko).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.concepts.concept_kinds import (
    ConceptKind,
    kinds_for_target,
    register_kind,
    target_for,
    targets_of,
)
from app.core.concepts.concept_view import ConceptView, concept_view_from


def _c(cid, *, subject="user", kind="identity", confidence=0.7,
       label=None, status="active"):
    return SimpleNamespace(
        concept_id=cid,
        label=label or f"concept {cid}",
        kind=kind,
        subject=subject,
        confidence=confidence,
        status=status,
    )


class _Edge:
    def __init__(self, dst_type, dst_id):
        self.dst_type = dst_type
        self.dst_id = str(dst_id)


class _FakeStore:
    """In-memory stand-in exposing the ConceptStore read surface used by
    the facade."""

    def __init__(self, concepts, *, near_score=0.6, edges=None):
        self._concepts = {int(c.concept_id): c for c in concepts}
        self._near_score = near_score
        self._edges = edges or {}
        self.nearest_calls = []

    def list_by(self, *, status=None, subject=None, kind=None, user_id=None):
        return [
            c for c in self._concepts.values()
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
        ]

    def nearest(self, _vec, *, status="active", subject=None, kind=None,
                k=8, **_kw):
        self.nearest_calls.append(
            {"status": status, "subject": subject, "kind": kind, "k": k}
        )
        rows = [
            c for c in self._concepts.values()
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
        ]
        return [(c, self._near_score) for c in rows[:k]]

    def get(self, cid):
        return self._concepts.get(int(cid))

    def edges_from(self, node_type, node_id):
        return list(self._edges.get((node_type, str(node_id)), []))


class CoreTests(unittest.TestCase):
    def test_filters_by_subject_kind_confidence_and_sorts(self) -> None:
        store = _FakeStore([
            _c(1, subject="user", confidence=0.6),
            _c(2, subject="user", confidence=0.9),
            _c(3, subject="aiko", confidence=0.95),
            _c(4, subject="user", confidence=0.3),
        ])
        view = ConceptView(store)
        out = view.core(subject="user", kind="identity", min_confidence=0.5)
        self.assertEqual([c.concept_id for c in out], [2, 1])

    def test_limit_caps(self) -> None:
        store = _FakeStore([_c(i, confidence=0.9 - i * 0.01) for i in range(5)])
        view = ConceptView(store)
        self.assertEqual(len(view.core(limit=2)), 2)

    def test_none_store_degrades_to_empty(self) -> None:
        self.assertEqual(ConceptView(None).core(), [])


class RelevantTests(unittest.TestCase):
    def test_wraps_nearest_active_and_applies_min_sim(self) -> None:
        store = _FakeStore([_c(1)], near_score=0.4)
        view = ConceptView(store)
        self.assertEqual(view.relevant([1.0], k=1, min_sim=0.5), [])
        self.assertEqual(len(view.relevant([1.0], k=1, min_sim=0.3)), 1)
        # Unfiltered query does not forward subject/kind (fast-path / doubles).
        call = store.nearest_calls[0]
        self.assertEqual(call["status"], "active")
        self.assertIsNone(call["subject"])
        self.assertIsNone(call["kind"])

    def test_forwards_subject_when_set(self) -> None:
        store = _FakeStore([_c(1, subject="aiko")])
        ConceptView(store).relevant([1.0], subject="aiko", k=2)
        self.assertEqual(store.nearest_calls[0]["subject"], "aiko")

    def test_none_embedding_degrades(self) -> None:
        store = _FakeStore([_c(1)])
        self.assertEqual(ConceptView(store).relevant(None), [])


class ForTargetTests(unittest.TestCase):
    def test_identity_routes_per_subject(self) -> None:
        store = _FakeStore([
            _c(1, subject="user", confidence=0.8),
            _c(2, subject="aiko", confidence=0.9),
        ])
        view = ConceptView(store)
        user = view.for_target("profile_block", subject="user")
        self.assertEqual([c.concept_id for c in user], [1])
        aiko = view.for_target("self_image_block", subject="aiko")
        self.assertEqual([c.concept_id for c in aiko], [2])

    def test_unknown_target_empty(self) -> None:
        store = _FakeStore([_c(1)])
        self.assertEqual(ConceptView(store).for_target("nope"), [])


class ForClusterTests(unittest.TestCase):
    def test_resolves_active_concepts_spanning_cluster(self) -> None:
        store = _FakeStore(
            [_c(1), _c(2, status="candidate"), _c(3)],
            edges={
                ("cluster", "100"): [
                    _Edge("concept", 1),
                    _Edge("concept", 2),  # not active -> dropped
                    _Edge("concept", 3),
                    _Edge("memory", 9),   # wrong dst_type -> ignored
                ],
            },
        )
        out = ConceptView(store).for_cluster(100)
        self.assertEqual(sorted(c.concept_id for c in out), [1, 3])

    def test_no_edges_empty(self) -> None:
        self.assertEqual(ConceptView(_FakeStore([_c(1)])).for_cluster(1), [])


class EvidenceLabelsTests(unittest.TestCase):
    def test_none_store_and_missing_deps_degrade(self) -> None:
        # No store -> [].
        self.assertEqual(ConceptView(None).evidence_labels(1), [])
        # Store present but no topic_graph / memory_store -> resolves what it
        # can (here nothing), never raises.
        store = _FakeStore([_c(1)])
        store.evidence_of = lambda _cid: []  # type: ignore[attr-defined]
        self.assertEqual(ConceptView(store).evidence_labels(1), [])


class ConceptViewFromTests(unittest.TestCase):
    def test_builds_from_host_attrs(self) -> None:
        store = _FakeStore([_c(1)])
        host = SimpleNamespace(
            _concept_store=store, _topic_graph=None, _memory_store=None,
        )
        view = concept_view_from(host)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertTrue(view.enabled)
        self.assertEqual(len(view.core()), 1)

    def test_none_when_store_absent(self) -> None:
        host = SimpleNamespace(_concept_store=None)
        self.assertIsNone(concept_view_from(host))


class KindRoutingTests(unittest.TestCase):
    def test_identity_targets_per_subject(self) -> None:
        from app.core.concepts.concept_kinds import get_kind

        identity = get_kind("identity")
        assert identity is not None
        self.assertEqual(target_for(identity, "user"), "profile_block")
        self.assertEqual(target_for(identity, "aiko"), "self_image_block")

    def test_kinds_for_target_resolves(self) -> None:
        self.assertIn(
            "identity", kinds_for_target("profile_block", subject="user"),
        )
        self.assertIn(
            "identity", kinds_for_target("self_image_block", subject="aiko"),
        )
        # Subject-agnostic query sees both targets of the kind.
        self.assertIn("identity", kinds_for_target("self_image_block"))
        self.assertEqual(kinds_for_target("nonexistent"), set())

    def test_scalar_fallback_when_no_map(self) -> None:
        # A kind with only the scalar surfacing_target still routes.
        register_kind(
            ConceptKind(name="_tv_scalar", surfacing_target="scalar_block")
        )
        try:
            k = ConceptKind(name="_tv_scalar", surfacing_target="scalar_block")
            self.assertEqual(target_for(k, "user"), "scalar_block")
            self.assertEqual(targets_of(k), {"scalar_block"})
            self.assertIn("_tv_scalar", kinds_for_target("scalar_block"))
        finally:
            from app.core.concepts.concept_kinds import CONCEPT_KINDS

            CONCEPT_KINDS.pop("_tv_scalar", None)


if __name__ == "__main__":
    unittest.main()
