"""Qualified concept context for a map reflection, not additional observations."""
import hashlib
import json
from types import SimpleNamespace

from app.core.infra import timephrase


def concept_record(concept, evidence=None):
    record = {key: getattr(concept, key, default) for key, default in (
        ("concept_id", None), ("subject", "unknown"), ("kind", "unknown"),
        ("label", ""), ("status", "unknown"), ("confidence", None),
        ("rationale", ""), ("created_at", None), ("updated_at", None),
        ("first_evidence_at", None), ("last_reinforced_at", None),
    )}
    if evidence is not None:
        record["basis"] = {key: evidence.get(key) for key in (
            "validity", "applicability", "applicability_sources_valid", "uncertainty",
            "source_issues", "truncated",
        )}
        # Excerpts in the general projection are shortened for inspection. This
        # consumer keeps their scope and revisions, never an incomplete quotation.
        record["basis"]["sources"] = [
            {key: value for key, value in row.items() if key != "text"}
            for row in evidence.get("evidence", [])
        ]
    else:
        record["basis"] = {"uncertainty": "source scope unknown"}
    return record


def context_key(rich, gaps):
    # Changing age labels, recalled-row counts and this worker's own reflection
    # do not establish a new understanding. Labels and held records can do so.
    territories = [[row.label, sorted(
        [{key: value for key, value in record.items()
          if key not in {"confidence", "updated_at"}} for record in row.concept_inputs],
        key=lambda record: json.dumps(record, sort_keys=True, default=str),
    )] for row in rich]
    data = [sorted(territories, key=lambda row: row[0]), sorted(label for label, size in gaps)]
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def derivation(rich, gaps):
    return {"version": 1, "scope": "generation_context", "memory_inputs": [],
            "context_key": context_key(rich, gaps),
            "territories": [{"label": row.label, "concept_inputs": row.concept_inputs}
                            for row in rich],
            "gap_labels": [label for label, size in gaps]}


def render_concept(record):
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > 2800:
        return ""
    return timephrase.format_memory_line(SimpleNamespace(
        kind="held_concept", content=text, temporal_type="durable", event_time=None,
        created_at=record.get("updated_at") or record.get("created_at"), metadata={},
    ))
