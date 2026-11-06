"""Preserve source meaning when SQLite memories become recall context."""
from app.core.memory.memory_origin import MemoryOrigin, memory_origin


def recall_origin(hit) -> MemoryOrigin:
    origin = getattr(hit, "memory_origin", None)
    if isinstance(origin, MemoryOrigin):
        return origin
    record = hit.record
    # A vector record has no metadata. Its kind alone cannot stand in for
    # a failed/missing authoritative join. Whole SQLite records can resolve
    # their legacy contract through the same seam as evidence admission.
    if isinstance(getattr(record, "metadata", None), dict):
        return memory_origin(record)
    return MemoryOrigin("unknown", "unknown", "unresolved_recall")


def recall_scope(hit) -> str:
    origin = recall_origin(hit)
    label = f"subject={origin.subject}; source={origin.mode}; domain={origin.domain}"
    if getattr(hit.record, "kind", "") == "catchphrase":
        label += "; recurring quotation, not a personal claim; authorship unproven"
    if getattr(hit, "memory_context", ""):
        label += "; " + hit.memory_context
    return f"[{label}] "


def recall_sections(lines, user_display_name):
    headers = {
        "user": f"What you know about {user_display_name} (long-term memory):",
        "aiko": "Things you've shared / decided about yourself:",
        "relationship": "Memories about your shared relationship:",
        "world": "Learned world knowledge (not personal user testimony):",
        "unknown": "Memories with unresolved subject or source (do not assume personal facts):",
    }
    return [headers[subject] + "\n" + "\n".join(lines[subject])
            for subject in headers if lines.get(subject)]
