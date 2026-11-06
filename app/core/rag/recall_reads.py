"""Fast recall packets over the existing retriever and authoritative stores."""
import hashlib
import json

import numpy as np

from app.core.memory.recall_records import memory_record, owned, pack_hits


class RecallUnavailable(RuntimeError):
    """Lookup did not complete; it cannot establish absence."""


class RecallReadsMixin:
    def recall_concept_packet(self, query, *, user_id=None, **kwargs):
        bundle = self.recall_concept(query, strict=True, **kwargs)
        if bundle is None:
            return {"status": "no_match", "concept": None}
        concept = self._concept_store.get(bundle["concept"]["concept_id"])
        if user_id and getattr(concept, "user_id", None) not in (None, user_id):
            return {"status": "no_match", "concept": None}
        evidence = []
        for item in bundle["evidence"]:
            row = self._memory_store.get(item["memory_id"]) if self._memory_store else None
            if row is not None and not owned(getattr(row, "source_session", None), user_id):
                bundle["evidence_truncated"] = True
                continue
            evidence.append(item)
        bundle.update(status="ok", evidence=evidence)
        return bundle

    def recall_packet(self, query, *, limit=6, user_id=None):
        diagnostics = {}
        hits = self.retrieve(query, _limit=limit, _mark_used=False, _diagnostics=diagnostics)
        payload = pack_hits(hits, store=self._memory_store, db=self._chat_db, user_id=user_id)
        errors = sorted(set(diagnostics.get("errors", []) + payload["unavailable_sources"]))
        payload["status"] = ("partial" if errors and payload["hits"] else "unavailable" if errors
                             else "ok" if payload["hits"] else "no_match")
        payload["unavailable_sources"] = errors
        return payload

    def recall_topic_page(self, query, *, limit=8, offset=0, revision=None, cluster_id=None,
                          include_historical=False, user_id=None):
        if self._topic_graph is None or self._memory_store is None:
            raise RecallUnavailable("Topic recall is unavailable; graph or memory store is missing")
        try:
            q = np.asarray(self._embedder.embed(query), dtype=np.float32).ravel()
            norm = float(np.linalg.norm(q))
            if not norm or not np.isfinite(norm):
                raise RecallUnavailable("Topic recall could not form a usable query vector.")
            q /= norm
            matches = self._topic_graph.best_clusters_for(q, top_n=3, min_sim=.30)
            if not matches:
                return {"status": "no_match", "topic_label": "", "hits": [],
                        "coverage": "one_topic_cluster", "total": 0, "has_more": False}
            chosen = next((m for m in matches if cluster_id is None or m[0] == cluster_id), None)
            if chosen is None:
                raise ValueError("Choose a cluster_id returned by this topic query.")
            cid, label, _ = chosen
            members = list(self._topic_graph.cluster_member_ids(cid))
            scored, state, withheld, unsearchable = [], [], 0, 0
            for mid in members[:2000]:
                row = self._memory_store.get(mid)
                if row is None or not owned(getattr(row, "source_session", None), user_id):
                    withheld += 1
                    state.append([mid, "missing_or_unavailable"])
                    continue
                record = memory_record(row, store=self._memory_store, db=self._chat_db,
                                       user_id=user_id)
                state.append([mid, record["revision"], record["source_state"]])
                if not include_historical and record["source_state"] in {
                    "withdrawn", "changed", "changed_or_unavailable", "outside_validity",
                }:
                    withheld += 1
                    continue
                vec = np.asarray(row.embedding, dtype=np.float32).ravel()
                norm = float(np.linalg.norm(vec))
                if vec.size != q.size or not norm or not np.isfinite(norm):
                    withheld += 1
                    unsearchable += 1
                    continue
                score = float(np.dot(q, vec) / norm)
                scored.append((score, int(row.id), row))
            scored.sort(key=lambda item: (-item[0], item[1]))
            key = hashlib.sha256(json.dumps([
                query, cid, label, include_historical, user_id, members, state,
                [(score, mid) for score, mid, row in scored],
            ], default=str).encode()).hexdigest()
            if offset and (not revision or revision != key):
                raise ValueError("Topic changed or revision missing; restart at offset 0.")
            selected = scored[offset:offset + limit]
            hits = [self._memory_to_hit(row, score=score) for score, mid, row in selected]
            payload = pack_hits(hits, store=self._memory_store, db=self._chat_db, user_id=user_id)
            if payload["unavailable_sources"] and not payload["hits"]:
                raise RecallUnavailable("Selected topic sources became unavailable; retry.")
            next_offset = offset + len(selected)
            payload.update(
                status="partial" if (len(members) > 2000 or unsearchable
                                     or payload["unavailable_sources"])
                else "ok" if scored else "no_match",
                topic_label=label, cluster_id=cid, revision=key, offset=offset,
                total=len(scored), cluster_members=len(members), scanned=min(2000, len(members)),
                withheld=withheld + payload["withheld"], coverage="one_topic_cluster",
                unsearchable=unsearchable,
                has_more=next_offset < len(scored),
                next_offset=next_offset if next_offset < len(scored) else None,
                alternatives=[{"cluster_id": m[0], "label": m[1], "similarity": float(m[2])}
                              for m in matches if m[0] != cid],
            )
            return payload
        except (RecallUnavailable, ValueError):
            raise
        except Exception as exc:
            raise RecallUnavailable("Topic lookup failed; absence is unknown.") from exc
