"""Owned, quota-bounded tool evidence, independent of its prompt projection."""

from __future__ import annotations

import hashlib

from app.core.infra import timephrase

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_result_artifacts (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    view TEXT NOT NULL,
    parent_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    revision TEXT NOT NULL,
    original_characters INTEGER NOT NULL,
    source_partial INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id, view)
);
CREATE INDEX IF NOT EXISTS idx_work_artifacts_parent ON work_result_artifacts(parent_id);
"""


class ResultArtifacts:
    def __init__(self, db, *, per_result_bytes=2_000_000, per_job_bytes=8_000_000):
        self.db = db
        self.per_result_bytes = per_result_bytes
        self.per_job_bytes = per_job_bytes

    def save(self, task_id: int, content: str, *, view="source", partial=False):
        if view not in {"source", "projection"}:
            raise ValueError("Unknown result view")
        conn = self.db._get_conn()
        # Serialize quota admission with the write, across concurrent child tasks.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT parent_task_id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not owner or owner[0] is None:
                return None
            parent = int(owner[0])
            used = conn.execute(
                "SELECT COALESCE(SUM(length(CAST(content AS BLOB))),0) "
                "FROM work_result_artifacts WHERE parent_id=? AND NOT(task_id=? AND view=?)",
                (parent, task_id, view),
            ).fetchone()[0]
            allowance = max(0, min(self.per_result_bytes, self.per_job_bytes - used))
            body = content.encode("utf-8")[:allowance].decode("utf-8", errors="ignore")
            if not body and content:
                return {"available": False, "source_partial": True, "reason": "artifact quota"}
            revision = hashlib.sha256(body.encode()).hexdigest()
            partial = bool(partial or len(body) < len(content))
            conn.execute(
                "INSERT INTO work_result_artifacts VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id,view) DO UPDATE SET content=excluded.content, "
                "revision=excluded.revision, original_characters=excluded.original_characters, "
                "source_partial=excluded.source_partial, created_at=excluded.created_at",
                (
                    task_id,
                    view,
                    parent,
                    body,
                    revision,
                    len(content),
                    int(partial),
                    timephrase.utcnow().isoformat(),
                ),
            )
        return {
            "available": True,
            "view": view,
            "revision": revision,
            "original_characters": len(content),
            "retained_characters": len(body),
            "source_partial": partial,
        }

    def read(self, parent_id: int, task_id: int, *, view="source"):
        row = (
            self.db._get_conn()
            .execute(
                "SELECT a.content,a.revision,a.original_characters,a.source_partial,a.created_at "
                "FROM work_result_artifacts a JOIN tasks child ON child.id=a.task_id "
                "JOIN tasks parent ON parent.id=a.parent_id WHERE a.task_id=? AND a.parent_id=? "
                "AND child.parent_task_id=parent.id AND child.user_id=parent.user_id AND a.view=?",
                (task_id, parent_id, view),
            )
            .fetchone()
        )
        if row is None:
            return None
        return dict(
            zip(
                ("content", "revision", "original_characters", "source_partial", "created_at"),
                row,
                strict=True,
            )
        )
