"""K85d — the pursuit cold start.

Almost every test here is guarding the same invariant: a seed enters as
an unevidenced candidate and gets no shortcut whatsoever. If any of
these ever start passing with an ``active`` row or a non-zero source
count, the feature has quietly become the canned hobby the backlog warns
about -- a confident claim about an interest she has never once shown.
"""
from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any

import numpy as np

from app.core.concepts.concept_lifecycle import pursuit_evidence_gate
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.pursuit_seeds import (
    KV_SEEDED,
    SEED_CONFIDENCE,
    STARTER_PURSUITS,
    seed_pursuits,
)
from app.core.infra.chat_database import ChatDatabase


class _Embedder:
    """Deterministic and label-sensitive, so dedupe is real."""

    def embed(self, text: str) -> Any:
        vec = np.zeros(256, dtype=np.float32)
        for word in text.lower().split():
            vec[zlib.crc32(word.encode()) % 256] += 1.0
        norm = float(np.linalg.norm(vec)) or 1.0
        return vec / norm


class _Kv:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class _Fixture:
    def __enter__(self) -> tuple[ConceptStore, _Kv]:
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        path = Path(self._dir.name) / "chat.db"
        self._db = ChatDatabase(path)
        self.store = ConceptStore(self._db)
        self.kv = _Kv()
        return self.store, self.kv

    def __exit__(self, *exc: Any) -> None:
        try:
            self._db.close()
            self._dir.cleanup()
        except (PermissionError, AttributeError):
            pass


def _seed(store: ConceptStore, kv: _Kv, **kw: Any) -> int:
    return seed_pursuits(
        store, _Embedder(), kv_get=kv.get, kv_set=kv.set, **kw,
    )


class SeedingTests(unittest.TestCase):
    def test_seeds_land_as_unevidenced_candidates(self) -> None:
        with _Fixture() as (store, kv):
            added = _seed(store, kv)
            self.assertEqual(added, len(STARTER_PURSUITS))
            rows = store.list_by(subject="aiko", kind="pursuit")
            self.assertEqual(len(rows), len(STARTER_PURSUITS))
            for row in rows:
                self.assertEqual(row.status, "candidate")
                self.assertEqual(row.distinct_source_count, 0)
                self.assertEqual(row.evidence_count, 0)
                self.assertEqual(row.confidence, SEED_CONFIDENCE)

    def test_a_seed_cannot_clear_the_gate_on_its_own(self) -> None:
        # However long it sits there and however the caller's thresholds
        # are set, zero sources is zero sources.
        self.assertFalse(
            pursuit_evidence_gate(
                distinct_source_count=0,
                age_days=365.0,
                confidence=1.0,
                min_sources=0,
                min_age_days=0.0,
                min_confidence=0.0,
            )
        )

    def test_a_seed_still_needs_three_lived_notes(self) -> None:
        common = {
            "age_days": 30.0,
            "confidence": 0.8,
            "min_sources": 2,
            "min_age_days": 0.0,
            "min_confidence": 0.5,
        }
        self.assertFalse(
            pursuit_evidence_gate(distinct_source_count=2, **common)
        )
        self.assertTrue(
            pursuit_evidence_gate(distinct_source_count=3, **common)
        )

    def test_seeding_is_watermarked(self) -> None:
        with _Fixture() as (store, kv):
            _seed(store, kv)
            self.assertIn(KV_SEEDED, kv.data)
            self.assertEqual(_seed(store, kv), 0)
            self.assertEqual(
                len(store.list_by(subject="aiko", kind="pursuit")),
                len(STARTER_PURSUITS),
            )

    def test_a_grown_pursuit_is_never_forked_by_a_seed(self) -> None:
        with _Fixture() as (store, kv):
            label = STARTER_PURSUITS[0]
            store.add(
                Concept(
                    label=label,
                    kind="pursuit",
                    subject="aiko",
                    status="active",
                    confidence=0.8,
                    distinct_source_count=4,
                    embedding=_Embedder().embed(label),
                )
            )
            added = _seed(store, kv)
            self.assertEqual(added, len(STARTER_PURSUITS) - 1)
            rows = store.list_by(subject="aiko", kind="pursuit")
            self.assertEqual(len(rows), len(STARTER_PURSUITS))
            grown = [r for r in rows if r.label == label]
            self.assertEqual(len(grown), 1)
            self.assertEqual(grown[0].status, "active")

    def test_a_blank_label_is_skipped(self) -> None:
        with _Fixture() as (store, kv):
            self.assertEqual(_seed(store, kv, labels=("", "   ")), 0)
            self.assertEqual(store.list_by(kind="pursuit"), [])

    def test_the_starters_are_first_person_and_about_her(self) -> None:
        # A seed that names him is a bond-scoped concept wearing the
        # pursuit label, which is the thing this kind exists to avoid.
        for label in STARTER_PURSUITS:
            self.assertTrue(label.startswith("you "), label)
            self.assertNotIn("jacob", label.lower())
            self.assertNotIn(" him", label.lower())
            self.assertNotIn(" together", label.lower())


class WiringTests(unittest.TestCase):
    def test_the_switch_is_parsed(self) -> None:
        from app.core.infra.agent_settings_parse import parse_agent_settings

        self.assertTrue(parse_agent_settings({}).pursuit_seeds_enabled)
        self.assertFalse(
            parse_agent_settings(
                {"pursuit_seeds_enabled": False}
            ).pursuit_seeds_enabled
        )


if __name__ == "__main__":
    unittest.main()
