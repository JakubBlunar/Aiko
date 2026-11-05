"""Concept context may guide questions; it must not manufacture observations."""
from dataclasses import dataclass

from app.core.concepts.concept_support import memory_revision, revision_hash
from app.core.memory.memory_origin import claim_source_refusal, origin_annotation


def _proof(view, concept):
    store, memories = view._store, view._memory_store
    if (memories is None or concept.status != "active"
            or not store.is_assertable(concept.concept_id)):
        return None
    edges = store.evidence_of(concept.concept_id)
    if not edges or len(edges) > 32:
        return None
    roots = {}
    clusters = None
    for edge in edges:
        if edge.src_type in {"memory", "moment"}:
            ids = [int(edge.src_id)]
        elif edge.src_type == "cluster" and view._topic_graph is not None:
            if clusters is None:
                clusters = {int(c.representative_id): c for c in view._topic_graph.topic_clusters()}
            cluster = clusters.get(int(edge.src_id))
            if cluster is None:
                return None
            ids = [int(mid) for mid in cluster.member_ids]
        else:
            return None  # Initial integration requires directly resolvable memory support.
        if not ids or len(roots) + len(ids) > 64:
            return None
        for mid in ids:
            row = memories.get(mid)
            if (row is None or (getattr(row, "metadata", None) or {}).get("superseded_by")
                    or claim_source_refusal(row, subject=concept.subject, kind=concept.kind)):
                return None
            roots[mid] = revision_hash([memory_revision(row), origin_annotation(row)])
    return revision_hash([concept.concept_id, concept.label, concept.subject, concept.kind,
                          sorted(roots.items())]) if roots else None


@dataclass(frozen=True)
class QuestionContext:
    refs: tuple = ()
    view: object = None

    def render(self):
        if not self.refs:
            return ""
        return ("\n\nCurrent interpretations, not new observations. Use them to find a useful "
                "new angle; do not re-ask settled basics or assert an imagined event:\n"
                + "\n".join(f"- {r['subject']}/{r['kind']}: {r['label']}"
                            for r in self.refs))

    def metadata(self):
        return {"context_concepts": [dict(r) for r in self.refs],
                "context_scope": "question_guidance"} if self.refs else {}

    def is_current(self):
        try:
            return all((c := self.view._store.get(ref["concept_id"])) is not None
                       and c.confidence >= .65 and _proof(self.view, c) == ref["revision"]
                       for ref in self.refs)
        except Exception:
            return False


def build_question_context(view, consumer, embedding):
    if embedding is None or view._store is None:
        return QuestionContext()
    proofs = {}
    relevant = {c.concept_id for c, _ in view.relevant(embedding, k=24, min_sim=.45)}

    def eligible(concept):
        try:
            if concept.concept_id not in relevant:
                return False
            proof = _proof(view, concept)
            if proof:
                proofs[concept.concept_id] = proof
                return True
        except Exception:
            pass
        return False

    rows = view.for_consumer(consumer, candidate_filter=eligible)
    return QuestionContext(tuple({"concept_id": c.concept_id, "label": c.label,
        "subject": c.subject, "kind": c.kind, "revision": proofs[c.concept_id]}
        for c in rows), view)


def question_context_from(provider, embedder, consumer, text):
    if provider is None or not text.strip():
        return QuestionContext()
    try:
        view = provider()
        return (view.question_context(consumer, embedder.embed(text[:900]))
                if view else QuestionContext())
    except Exception:
        return QuestionContext()
