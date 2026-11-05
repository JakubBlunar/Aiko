"""Immutable, role-balanced inputs and receipts for one topic summary."""
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from types import SimpleNamespace

from app.core.concepts.concept_support import memory_revision, revision_hash
from app.core.conversation.topic_memory_policy import topic_member_eligible
from app.core.infra import timephrase
from app.core.memory.memory_origin import memory_origin, origin_annotation


@dataclass(frozen=True)
class DigestSources:
    rows: tuple
    fingerprint: str
    member_count: int
    revisions: tuple

    @property
    def ids(self):
        return [row.id for row in self.rows]

    def render(self, *, max_chars, now):
        lines = []
        for row in self.rows:
            short = SimpleNamespace(**{**vars(row), "content": row.content[:max_chars]})
            lines.append(f"ID {row.id} " + timephrase.format_memory_line(short, now)
                         + " [" + origin_annotation(row) + "]")
        return "\n".join(lines)


def capture_sources(cluster, store, *, limit=16):
    rows, signatures = [], []
    for mid in sorted({int(mid) for mid in cluster.member_ids}):
        memory = store.get(mid)
        if memory is None:
            continue
        row = SimpleNamespace(**{key: deepcopy(getattr(memory, key, default)) for key, default in (
            ("id", mid), ("content", ""), ("kind", ""), ("metadata", {}), ("created_at", ""),
            ("event_time", None), ("temporal_type", "durable"), ("provenance", "inferred"),
            ("salience", .5),
        )})
        origin = memory_origin(row)
        eligible = (topic_member_eligible(row) and origin.subject != "unknown"
                    and origin.mode not in {"hypothetical", "question", "summary", "unknown"})
        signature = revision_hash([memory_revision(row), asdict(origin), row.temporal_type,
                                   row.provenance, eligible])
        if eligible:
            signatures.append((mid, signature))
        if eligible and row.content.strip():
            rows.append(row)

    def rank(row):
        stamp = timephrase.parse_iso(row.created_at)
        return (-(stamp.timestamp() if stamp else 0), -float(row.salience), row.id)

    ordered = sorted(rows, key=rank)
    groups = defaultdict(deque)
    picked = []
    for row in ordered:
        if row.id == cluster.representative_id:
            picked.append(row)
        else:
            origin = memory_origin(row)
            groups[(origin.subject, origin.mode, origin.domain)].append(row)
    while groups and len(picked) < limit:
        for key in list(groups):
            picked.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]
            if len(picked) >= limit:
                break
    selected = tuple(picked[:limit])
    revisions = tuple((row.id, dict(signatures)[row.id]) for row in selected)
    return DigestSources(selected, revision_hash(signatures), len(rows), revisions)
