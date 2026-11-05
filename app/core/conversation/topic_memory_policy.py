"""Topical organization includes history, but not duplicate or imagined evidence."""

NON_CLUSTERING_KINDS = frozenset({"topic_digest", "pre_thought"})


def topic_member_eligible(memory) -> bool:
    if memory is None or str(getattr(memory, "kind", "")) in NON_CLUSTERING_KINDS:
        return False
    meta = getattr(memory, "metadata", None) or {}
    if isinstance(meta, dict) and (meta.get("consolidated_into") or meta.get("superseded_by")):
        return False
    return True  # Ordinary archive rows can still describe useful, true history.
