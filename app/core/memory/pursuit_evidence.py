"""Execution in Aiko's virtual world and research reflection have distinct receipts."""
from dataclasses import asdict
import hashlib
import json

from app.core.memory.memory_origin import MemoryOrigin


def build(source, topic, stamp, extra, store):
    activity = "virtual" if source == "away_beat" else (
        "research" if extra.get("progress_source") == "research" else "idea"
    )
    identity = json.dumps([source, topic, stamp, extra.get("thread_started_at"),
                           extra.get("keys"), extra.get("world_receipts")], sort_keys=True)
    receipt = {"version": 1, "episode_id": hashlib.sha256(identity.encode()).hexdigest(),
               "activity_mode": activity, "recorded_at": stamp,
               "event_scope": "virtual_episode" if activity == "virtual" else "reflection",
               "physical_occurrence_time": None, "clauses": extra.get("clauses", []),
               "world_receipts": extra.get("world_receipts", []), "research_inputs": []}
    derivation = None
    if activity == "research":
        from app.core.memory.reflective_evidence import capture

        ids = [mid for mid in extra.get("research_memory_ids", []) if type(mid) is int and mid > 0]
        ids = list(dict.fromkeys(ids))[-12:]
        if not ids:
            return None
        rows = [store.get(mid) for mid in ids]
        if any(row is None or row.kind != "curiosity_finding"
               or not (row.metadata or {}).get("research_evidence") for row in rows):
            return None
        _, derivation, _ = capture(rows, max_chars=12000, limit=12)
        if len(derivation["memory_inputs"]) != len(ids):
            return None
        derivation["note_kind"] = "research_reflection"
        receipt["research_inputs"] = derivation["memory_inputs"]
    origin = MemoryOrigin("aiko", "virtual_event" if activity == "virtual" else "reflection",
                          "pursuit_notes", "virtual" if activity == "virtual" else "conversation")
    return receipt, asdict(origin), derivation


def current(receipt, store):
    if receipt is None:
        return True
    if not isinstance(receipt, dict) or receipt.get("version") != 1:
        return False
    inputs = receipt.get("research_inputs")
    if not isinstance(inputs, list):
        return False
    if not inputs:
        return receipt.get("activity_mode") in {"virtual", "idea"}
    from app.core.memory.reflective_evidence import current as sources_current

    for item in inputs:
        if not isinstance(item, dict) or type(item.get("memory_id")) is not int:
            return False
        row = store.get(item["memory_id"])
        if (row is None or row.kind != "curiosity_finding"
                or (row.metadata or {}).get("pursuit_evidence")):
            return False
    return sources_current({"memory_inputs": inputs}, store)


def context(memory):
    receipt = (getattr(memory, "metadata", None) or {}).get("pursuit_evidence")
    if not isinstance(receipt, dict):
        return ""
    return (f"activity={receipt.get('activity_mode', 'unknown')}; "
            f"scope={receipt.get('event_scope', 'unknown')}; "
            "no physical occurrence inferred; "
            f"episode={receipt.get('episode_id', 'unknown')}")
