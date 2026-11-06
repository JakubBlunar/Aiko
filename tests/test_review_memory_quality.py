import hashlib
import json
import sqlite3
import subprocess
import sys

import numpy as np
import pytest

from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from scripts.review_memory_quality import review


def test_historical_review_links_sources_without_changing_state(tmp_path):
    path = tmp_path / "review.db"
    db = ChatDatabase(path)
    mid = db.add_message("s", "user", "I plan to call the dentist.")
    store = MemoryStore(path)
    plan = store.add("Noah plans to call the dentist", "event", np.ones(4),
                     temporal_type="past_event", source_message_id=mid)
    for _ in range(2):
        store.add("Noah promised: call the dentist", "promise", np.ones(4), skip_dedupe=True,
            source_message_id=mid,
            metadata={"promise_who": "user", "promise_deadline": "2026-10-01T18:30:00Z"})
    knowledge = store.add("A public fact about browser cookies", "knowledge", np.ones(4))
    concepts = ConceptStore(db)
    concept = Concept(label="Noah configured the browser cookies", kind="narrative", subject="user",
                      status="active", confidence=.8, embedding=np.ones(4))
    concepts.add(concept)
    concepts.add_edge(ConceptEdge("memory", str(knowledge.id), "concept", str(concept.concept_id),
                                  "supports"))
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        before = store.get(plan.id).to_dict()
        report = review(conn, limit=2)
        assert report["flag_counts"]["historical_plan_modality"] == 1
        assert report["flag_counts"]["repeated_commitment_source"] == 1
        assert report["flag_counts"]["deadline_needs_source_review"] == 2
        sample = report["memory_samples"]["historical_plan_modality"][0]
        assert sample["source_messages"][0]["id"] == mid
        assert report["concept_samples"][0]["id"] == concept.concept_id
        assert report["concept_samples"][0]["incompatible_sources"][0]["reason"] == "world_fact"
        assert store.get(plan.id).to_dict() == before
        assert concepts.get(concept.concept_id).status == "active"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE memories SET content='changed'")
    finally:
        conn.close()
        store.close()


def test_cli_really_opens_the_database_read_only_and_writes_only_the_report(tmp_path):
    path = tmp_path / "review.db"
    ChatDatabase(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    output = tmp_path / "private-report.json"
    result = subprocess.run([sys.executable, "scripts/review_memory_quality.py",
        "--database", str(path), "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["scope"] == "read_only_review_candidates"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
