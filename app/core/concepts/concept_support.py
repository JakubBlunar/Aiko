"""Cold, content-free receipts for support freshness and revival outcomes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.infra import timephrase

SOURCE_PREFIX = "concept.support_source:"
STATE_PREFIX = "concept.support_state:"
REVIEW_PREFIX = "concept.support_review:"
INPUT_PREFIX = "concept.support_inputs:"
DISPROOF_PREFIX = "concept.support_disproof:"
MANIFEST_PREFIX = "concept.support_manifest:"


def revision_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]


def memory_revision(memory: Any) -> str:
    metadata = getattr(memory, "metadata", None) or {}
    return revision_hash(
        [
            memory.content,
            getattr(memory, "kind", ""),
            getattr(memory, "event_time", None),
            getattr(memory, "created_at", ""),
            {
                key: metadata.get(key)
                for key in (
                    "source_message_ids",
                    "input_message_ids",
                    "source_session",
                    "provenance",
                    "superseded_by",
                    "superseded_reason",
                    "support_observations",
                    "support_observations_truncated",
                )
            },
        ]
    )


class SupportLedger:
    """Latest revision per cited source, stored in SQLite rather than mirrored."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def observe(self, concept_id, node, *, revision="ref", previously_held=False,
                manifest=None) -> bool:
        key = f"{SOURCE_PREFIX}{int(concept_id)}:{node[0]}:{node[1]}"
        previous = self._db.kv_get(key)
        fresh = (
            previous is None
            and not previously_held
            or previous is not None
            and previous != "ref"
            and revision != "ref"
            and previous != revision
        )
        if previous is None or revision != "ref":
            self._db.kv_set(key, revision)
        if fresh and manifest is not None:
            self._db.kv_set(
                f"{MANIFEST_PREFIX}{int(concept_id)}:{node[0]}:{node[1]}",
                json.dumps({"node": list(node), "revision": revision,
                            "at": timephrase.utcnow().isoformat(), "lineage": manifest}),
            )
        return fresh

    def manifests(self, concept_id: int, *, limit: int = 128):
        rows = self._db._get_conn().execute(
            "SELECT value FROM kv_meta WHERE key GLOB ? ORDER BY key LIMIT ?",
            (f"{MANIFEST_PREFIX}{int(concept_id)}:*", int(limit) + 1),
        ).fetchall()
        return [json.loads(row[0]) for row in rows[:limit]], len(rows) > limit

    def set_state(self, concept_id: int, state: str, reason: str) -> None:
        self._db.kv_set(
            STATE_PREFIX + str(int(concept_id)),
            json.dumps(
                {
                    "state": state,
                    "reason": reason,
                    "at": timephrase.utcnow().isoformat(),
                }
            ),
        )

    def state(self, concept_id: int) -> dict[str, Any]:
        raw = self._db.kv_get(STATE_PREFIX + str(int(concept_id)))
        try:
            result = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return {}
        return result if isinstance(result, dict) else {}

    def inputs(self, concept_id: int) -> list[tuple[str, str]]:
        raw = self._db.kv_get(INPUT_PREFIX + str(int(concept_id)))
        return [tuple(node) for node in json.loads(raw)] if raw else []

    def record_inputs(self, concept_id: int, nodes) -> None:
        self._db.kv_set(INPUT_PREFIX + str(int(concept_id)), json.dumps(sorted(nodes)[:6]))

    def review(self, concept_id: int) -> dict[str, Any]:
        raw = self._db.kv_get(REVIEW_PREFIX + str(int(concept_id)))
        return json.loads(raw) if raw else {}

    def record_review(self, concept_id: int, row: dict[str, Any]) -> None:
        self._db.kv_set(REVIEW_PREFIX + str(int(concept_id)), json.dumps(row))

    def consume_review(self, concept_id: int, evaluated_at: str) -> None:
        row = self.review(concept_id)
        if row:
            row.update(pending=False, consumed_at=evaluated_at)
            self.record_review(concept_id, row)

    def disproof_ids(self, concept_id: int) -> list[int]:
        raw = self._db.kv_get(DISPROOF_PREFIX + str(int(concept_id)))
        return json.loads(raw) if raw else []

    def remember_disproof(self, concept_id: int, memory_id: int, *, support_at=None) -> None:
        ids = sorted(set(self.disproof_ids(concept_id)) | {int(memory_id)})[:7]
        self._db.kv_set(DISPROOF_PREFIX + str(int(concept_id)), json.dumps(ids))
        review = self.review(concept_id)
        review.update(
            pending=False, outcome="invalidated", reason="renewed_disproof",
            blocked_support_at=support_at or review.get("support_at"),
            disproof_version=int(review.get("disproof_version", 0)) + 1,
            invalidated_at=timephrase.utcnow().isoformat(),
        )
        self.record_review(concept_id, review)

    def had_disproof(self, concept_id: int) -> bool:
        if self.disproof_ids(concept_id):
            return True
        return bool(
            self._db._get_conn()
            .execute(
                "SELECT 1 FROM concept_events WHERE concept_id = ? "
                "AND event_type = 'contradicted' LIMIT 1",
                (int(concept_id),),
            )
            .fetchone()
        )

    def diagnostics(self) -> dict[str, Any]:
        connection = self._db._get_conn()
        rows = connection.execute(
            "SELECT json_extract(value, '$.state'), COUNT(*) FROM kv_meta "
            "WHERE key GLOB 'concept.support_state:*' AND json_valid(value) "
            "GROUP BY json_extract(value, '$.state')"
        ).fetchall()
        return {"states": dict(rows), "scope": "latest recorded assessment per concept"}
