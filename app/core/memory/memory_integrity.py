"""Shared source/scope checks for insert, rewrite and consolidation."""
from dataclasses import asdict

from app.core.infra import timephrase
from app.core.memory.memory_origin import MemoryOrigin, memory_origin


def merge_source_key(memory):
    """Ephemeral full source check, without upgrading historical support hashes."""
    from app.core.concepts.concept_support import memory_revision, revision_hash

    metadata = getattr(memory, "metadata", None) or {}
    return revision_hash([memory_revision(memory),
                          getattr(memory, "temporal_type", None),
                          getattr(memory, "provenance", None),
                          getattr(memory, "tier", None),
                          getattr(memory, "pinned", False),
                          metadata.get("admission"), metadata.get("consolidated_into"),
                          metadata.get("source_ids"), metadata.get("source_revisions"),
                          metadata.get("consolidation_lineage")])


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
    from app.core.memory.observation_receipts import facet_scope

    if facet_scope(first) != facet_scope(second):
        return False
    if getattr(first, "temporal_type", "durable") != getattr(second, "temporal_type", "durable"):
        return False
    first_time, second_time = (getattr(m, "event_time", None) for m in (first, second))
    if (getattr(first, "temporal_type", "durable") == "future_plan"
            and not first_time and not second_time):
        recorded = [timephrase.parse_iso(getattr(m, "created_at", None)) for m in (first, second)]
        if (None in recorded or abs((recorded[0] - recorded[1]).total_seconds()) > 6 * 3600):
            return False
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


def conflict_scope_compatible(first, second):
    """Different actors, purposes or episodes are not factual contradictions."""
    a, b = memory_origin(first), memory_origin(second)
    factual = {"observation", "declaration", "interpretation", "world_fact"}
    if a.subject == "unknown" or (a.subject, a.domain) != (b.subject, b.domain):
        return False
    if a.mode not in factual or b.mode not in factual:
        return False
    scope = getattr(first, "temporal_type", "durable")
    if scope != getattr(second, "temporal_type", "durable"):
        return False
    if scope not in {"durable", "preference"}:
        dates = [timephrase.parse_iso(getattr(m, "event_time", None)) for m in (first, second)]
        return dates[0] is not None and dates[0] == dates[1]
    return True


def rewrite_metadata(memory, metadata, *, at, curated=False):
    """Retain bounded old wording; a synthesis does not inherit verbatim authority."""
    from app.core.concepts.concept_support import memory_revision

    result = dict(metadata)
    previous = (memory.metadata or {}).get("content_versions", [])
    versions = ([v for v in previous if isinstance(v, dict)][-1:]
                if isinstance(previous, list) else [])
    version = {"content": memory.content[:4000], "truncated": len(memory.content) > 4000,
               "revision": memory_revision(memory), "provenance": memory.provenance,
               "kind": memory.kind, "temporal_type": memory.temporal_type,
               "event_time": memory.event_time, "replaced_at": at}
    versions.append(version)
    original = (memory.metadata or {}).get("original_content")
    result["original_content"] = dict(original) if isinstance(original, dict) else dict(version)
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
