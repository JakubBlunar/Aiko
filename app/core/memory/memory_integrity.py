"""Shared source/scope checks for insert, rewrite and consolidation."""
from dataclasses import asdict

from app.core.infra import timephrase
from app.core.memory.memory_origin import MemoryOrigin, memory_origin


def compatible_memories(first, second, *, consolidation=False):
    a, b = memory_origin(first), memory_origin(second)
    if a.subject == "unknown" or b.subject == "unknown":
        return False
    if (a.subject, a.mode, a.domain) != (b.subject, b.mode, b.domain):
        return False
    if consolidation and a.mode not in {"observation", "declaration", "world_fact"}:
        return False
    if getattr(first, "provenance", "inferred") != getattr(second, "provenance", "inferred"):
        return False
    if getattr(first, "temporal_type", "durable") != getattr(second, "temporal_type", "durable"):
        return False
    first_time, second_time = (getattr(m, "event_time", None) for m in (first, second))
    if getattr(first, "temporal_type", "durable") in {"past_event", "ongoing"}:
        if not first_time or not second_time:
            return False
    if any(value and timephrase.parse_iso(value) is None for value in (first_time, second_time)):
        return False
    if timephrase.parse_iso(first_time) != timephrase.parse_iso(second_time):
        return False
    if getattr(first, "kind", "") == "promise" or getattr(second, "kind", "") == "promise":
        return all((getattr(first, "metadata", None) or {}).get(key)
                   == (getattr(second, "metadata", None) or {}).get(key)
                   for key in ("promise_who", "promise_status", "promise_deadline"))
    return True


def normalize_memory_time(content, temporal_type, event_time, recorded_at):
    """Classify stale wording without inventing an unknown event date."""
    event = timephrase.parse_iso(event_time)
    if event_time and event is None:
        event_time = None
    direction = timephrase.deictic_direction(content)
    if temporal_type in {"durable", "preference"} and direction:
        temporal_type = "future_plan" if direction == timephrase.FUTURE else "past_event"
    written = timephrase.parse_iso(recorded_at)
    if (temporal_type == "past_event" and event is not None
            and written is not None and event > written):
        temporal_type = "future_plan"
    return temporal_type, event_time


def rewrite_metadata(memory, metadata, *, at, curated=False):
    """Retain bounded old wording; a synthesis does not inherit verbatim authority."""
    from app.core.concepts.concept_support import memory_revision

    result = dict(metadata)
    previous = (memory.metadata or {}).get("content_versions", [])
    versions = ([v for v in previous if isinstance(v, dict)][-1:]
                if isinstance(previous, list) else [])
    versions.append({"content": memory.content[:4000], "truncated": len(memory.content) > 4000,
                     "revision": memory_revision(memory), "provenance": memory.provenance,
                     "kind": memory.kind, "temporal_type": memory.temporal_type,
                     "event_time": memory.event_time, "replaced_at": at})
    result["content_versions"] = versions
    origin = memory_origin(memory)
    mode = origin.mode
    if not curated and mode in {"observation", "declaration"}:
        mode = "interpretation"
    result["memory_origin"] = asdict(MemoryOrigin(
        origin.subject, mode, "manual_edit" if curated else "content_rewrite", origin.domain,
    ))
    if not curated and isinstance(result.get("admission"), dict):
        result["admission"] = {**result["admission"], "reason": "derived_rewrite"}
    return result
