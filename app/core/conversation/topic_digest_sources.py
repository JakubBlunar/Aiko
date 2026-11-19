"""Immutable, role-balanced inputs and receipts for one topic summary."""
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from types import SimpleNamespace

from app.core.concepts.concept_support import memory_revision, revision_hash
from app.core.conversation.digest_fidelity import claim_fields_valid, current_state_fresh
from app.core.conversation.topic_memory_policy import topic_member_eligible
from app.core.infra import timephrase
from app.core.memory import promise_lifecycle
from app.core.memory.memory_origin import memory_origin, origin_annotation
from app.core.memory.observation_receipts import longitudinal_sources_current
from app.core.memory.plan_timing import historical_plan
from app.core.memory.reflective_evidence import eligible


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
            # The old soft target clipped trailing exceptions. Capture bounds
            # whole records instead; never show a qualification-free prefix.
            lines.append(f"ID {row.id} " + timephrase.format_memory_line(row, now)
                         + " [" + origin_annotation(row) + "]")
        return "\n".join(lines)


def _source_revision(row, store):
    if not topic_member_eligible(row) or not eligible(row) or not 0 < len(row.content) <= 1000:
        return None
    origin = memory_origin(row)
    if (origin.subject == "unknown"
            or origin.mode in {"hypothetical", "question", "summary", "unknown"}
            or not longitudinal_sources_current(row, store)):
        return None
    # Keep the signature format shared with receipts from existing digests.
    return revision_hash([memory_revision(row), asdict(origin),
                          getattr(row, "temporal_type", "durable"),
                          getattr(row, "provenance", "inferred"), True])


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
            ("relevance_until", None),
        )})
        signature = _source_revision(row, store)
        if signature is not None:
            signatures.append((mid, signature))
            if row.content.strip():
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
    return DigestSources(selected, revision_hash(["scope_clauses_v1", signatures]),
                         len(rows), revisions)


def digest_expired(memory, now):
    receipt = (getattr(memory, "metadata", None) or {}).get("summary_claims")
    if not isinstance(receipt, dict):
        return False
    saved = receipt.get("valid_until")
    until = timephrase.parse_iso(saved)
    return saved is not None and (until is None or until < now)


def current_digest_text(memory, store, now, *, source_allowed=None):
    """Project complete, currently supported clauses from the canonical receipt."""
    if getattr(memory, "kind", None) != "topic_digest" or not eligible(memory):
        return None
    now = timephrase.to_aware(now)
    until = timephrase.parse_iso(getattr(memory, "relevance_until", None))
    if (until and until < now) or digest_expired(memory, now):
        return None
    meta = getattr(memory, "metadata", None) or {}
    receipt, revisions = meta.get("summary_claims"), meta.get("source_revisions")
    offered = meta.get("source_ids")
    if (not isinstance(receipt, dict) or receipt.get("version") != 1
            or receipt.get("text") != memory.content
            or not isinstance(revisions, dict) or not isinstance(offered, list)
            or not 1 <= len(offered) <= 16 or any(type(mid) is not int for mid in offered)):
        return None
    claims = receipt.get("claims")
    if not isinstance(claims, list) or not 1 <= len(claims) <= 4:
        return None

    sources, lines, length = {}, [], 0
    for claim in claims:
        if not claim_fields_valid(claim):
            continue
        ids = claim["source_ids"]
        for mid in ids:
            if mid in sources:
                continue
            source = store.get(mid) if mid in offered else None
            saved = revisions.get(str(mid), revisions.get(mid))
            sources[mid] = (source if source is not None and saved
                            and (source_allowed is None or source_allowed(source))
                            and _source_revision(source, store) == saved else None)
        scope = claim["scope"]
        rows = [sources[mid] for mid in ids]
        if not all(row is not None and _source_live(row, scope, now) for row in rows):
            continue
        domains = ", ".join(sorted({memory_origin(row).domain for row in rows}))
        cited = ", ".join(str(mid) for mid in ids)
        text = " ".join(claim["text"].split())
        line = (f"[subject={claim['subject']}; scope={scope}; domain={domains}; "
                f"sources={cited}] {text}")
        if length + len(line) <= 1200:
            lines.append(line)
            length += len(line) + 1
    return "\n".join(lines) or None


def _source_live(row, scope, now):
    until = timephrase.parse_iso(getattr(row, "relevance_until", None))
    if until and until < now:
        return False
    if scope == "current_state":
        return current_state_fresh(row, now)
    if scope == "intention":
        if row.kind == "promise" and promise_lifecycle.promise_status(row) in {
            promise_lifecycle.STATUS_DROPPED, promise_lifecycle.STATUS_FULFILLED,
        }:
            return False
        return not historical_plan(row, now)
    return True
