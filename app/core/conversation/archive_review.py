"""Durable, bounded on-demand archive review jobs and compare-and-save summaries."""

from __future__ import annotations

import json
import secrets
import logging
from datetime import timedelta

from app.core.conversation.archive import ConversationArchive, resolve_range
from app.core.conversation.archive_summary import SUMMARY_VERSION, summary_prompt, validate_summary
from app.core.infra import timephrase

MAX_JOBS = 32
LEASE_SECONDS = 300
log = logging.getLogger(__name__)


class ArchiveReview:
    def __init__(self, db, user_id: str):
        self.archive = ConversationArchive(db, user_id)
        self.db, self.user_id = db, user_id

    def jobs(self) -> list[dict]:
        return [
            dict(zip(("local_day", "timezone", "state", "reason"), row, strict=True))
            for row in self.db.execute_fetchall(
                "SELECT local_day, timezone, state, "
                "CASE WHEN state='failed' THEN reason ELSE '' END FROM archive_summary_jobs "
                "WHERE user_id=? ORDER BY updated_at LIMIT ?",
                (self.user_id, MAX_JOBS),
            )
        ]

    def enqueue(self, day: str, tz_name: str) -> dict:
        resolve_range(start=day, end=day, tz_name=tz_name)
        # Validate day before entering the write transaction. Queue rows contain
        # only dates/status, never source excerpts or generated personal text.
        conn = self.db._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT state FROM archive_summary_jobs "
                "WHERE user_id=? AND timezone=? AND local_day=?",
                (self.user_id, tz_name, day),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM archive_summary_jobs WHERE user_id=?",
                (self.user_id,),
            ).fetchone()[0]
            if not existing and count >= MAX_JOBS:
                raise ValueError("Archive queue is full; finish or remove an existing request.")
            if not existing or existing[0] == "failed":
                conn.execute(
                    "INSERT INTO archive_summary_jobs VALUES (?, ?, ?, 'pending', 0, ?, '') "
                    "ON CONFLICT(user_id, timezone, local_day) DO UPDATE SET "
                    "state='pending', attempts=0, updated_at=excluded.updated_at, reason=''",
                    (self.user_id, tz_name, day, timephrase.utcnow().isoformat()),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"status": "queued", "local_day": day}

    def remove(self, day: str, tz_name: str) -> None:
        # Removing the row also revokes an in-flight job's save receipt.
        self.db.execute_commit(
            "DELETE FROM archive_summary_jobs WHERE user_id=? AND timezone=? AND local_day=?",
            (self.user_id, tz_name, day),
        )

    def pending(self):
        expired = (timephrase.utcnow() - timedelta(seconds=LEASE_SECONDS)).isoformat()
        return self.db.execute_fetchone(
            "SELECT local_day, timezone FROM archive_summary_jobs WHERE user_id=? "
            "AND (state='pending' OR (state='running' AND updated_at<?)) "
            "ORDER BY updated_at, local_day LIMIT 1",
            (self.user_id, expired),
        )

    def _claim(self):
        conn = self.db._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            pending = self.pending()
            if pending is None:
                conn.rollback()
                return None
            day, zone = pending
            attempts = conn.execute(
                "SELECT attempts FROM archive_summary_jobs "
                "WHERE user_id=? AND timezone=? AND local_day=?",
                (self.user_id, zone, day),
            ).fetchone()[0]
            if attempts >= 2:
                conn.execute(
                    "UPDATE archive_summary_jobs SET state='failed', reason='interrupted' "
                    "WHERE user_id=? AND timezone=? AND local_day=?",
                    (self.user_id, zone, day),
                )
                conn.commit()
                return None
            # Random token prevents cancel/requeue or lease reclamation from
            # accepting an older worker's result, even at an identical clock.
            token = secrets.token_hex(16)
            conn.execute(
                "UPDATE archive_summary_jobs SET state='running', attempts=attempts+1, "
                "updated_at=?, reason=? WHERE user_id=? AND timezone=? AND local_day=?",
                (timephrase.utcnow().isoformat(), token, self.user_id, zone, day),
            )
            conn.commit()
            return day, zone, token
        except Exception:
            conn.rollback()
            raise

    def run_one(self, client, model: str, context_window: int | None = None) -> dict:
        claim = self._claim()
        if claim is None:
            return {"status": "caught_up"}
        day, zone, token = claim
        try:
            result = self._generate(day, zone, token, client, model, context_window)
        except Exception as exc:
            # Never save provider error text: it may repeat the private prompt.
            log.warning("archive_summary: failed error_type=%s", type(exc).__name__)
            result = {"status": "generation_failed"}
        status = result["status"]
        if status != "built":
            self.db.execute_commit(
                "UPDATE archive_summary_jobs SET state=CASE "
                "WHEN ?='source_changed' AND attempts<2 THEN 'pending' ELSE 'failed' END, "
                "reason=?, updated_at=? "
                "WHERE user_id=? AND timezone=? AND local_day=? AND reason=?",
                (status, status, timephrase.utcnow().isoformat(), self.user_id, zone, day, token),
            )
        return {"status": status, "local_day": day}

    def _generate(self, day, zone, token, client, model, context_window):
        # Reuse a valid existing outline. Rebuilding would discard its summary.
        span = resolve_range(start=day, end=day, tz_name=zone)
        revision = self.db.execute_fetchone(
            "SELECT revision FROM archive_revisions WHERE user_id=?",
            (self.user_id,),
        )
        revision = revision[0] if revision else 0
        overview = self.archive.overviews(span)["overviews"]
        if not overview:
            built = self.archive.build_day(day, zone)
            if built["status"] != "built":
                return built
            overview = [built["overview"]]
        base = overview[0]
        # Read actual source rows, not just the outline's sampled highlights.
        page = self.archive.messages(span)
        rows, chars, next_cursor = [], 0, None
        while True:
            if page["status"] not in {"ok", "empty_range"}:
                return {"status": "source_changed"}
            for row in page["messages"]:
                if chars + len(row["text"]) > 16000 or len(rows) >= 100:
                    break
                rows.append(row)
                chars += len(row["text"])
            next_cursor = page["next_cursor"]
            if not next_cursor or chars >= 14000 or len(rows) >= 100:
                break
            page = self.archive.messages(cursor=next_cursor)
        packed = summary_prompt(rows, day, context_window=context_window, tz_name=zone)
        if packed is None:
            return {"status": "insufficient_context"}
        used = rows[: packed.item_count]
        raw, _ = client.chat_json(
            packed.messages,
            model=model,
            options={"num_predict": 1400, "temperature": 0.1},
            timeout_seconds=90,
            surface="archive_summary",
        )
        bullets = validate_summary(raw, used)
        summary = {
            "receipt": token,
            "kind": "generated_discussion_summary",
            "version": SUMMARY_VERSION,
            "bullets": bullets,
            "model": model,
            "generated_at": timephrase.utcnow().isoformat(),
            "source_messages_used": len(used),
            "source_text_complete": (
                base["coverage"]["scan_complete"]
                and not next_cursor
                and len(used) == base["message_count"]
                and all(row["text_complete"] for row in used)
            ),
            "note": (
                "Generated interpretation of cited dialogue, not an activity log or verified facts."
            ),
        }
        conn = self.db._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            saved = conn.execute(
                "SELECT overview_json FROM conversation_daily_overviews "
                "WHERE user_id=? AND timezone=? AND local_day=? AND source_hash=?",
                (self.user_id, zone, day, base["source_hash"]),
            ).fetchone()
            job = conn.execute(
                "SELECT 1 FROM archive_summary_jobs WHERE user_id=? AND timezone=? "
                "AND local_day=? AND state='running' AND reason=?",
                (self.user_id, zone, day, token),
            ).fetchone()
            if saved is None or job is None or self.archive._revision(conn) != revision:
                conn.rollback()
                return {"status": "source_changed"}
            value = json.loads(saved[0])
            value["summary"] = summary
            conn.execute(
                "UPDATE conversation_daily_overviews SET overview_json=?, updated_at=? "
                "WHERE user_id=? AND timezone=? AND local_day=?",
                (
                    json.dumps(value, ensure_ascii=False),
                    timephrase.utcnow().isoformat(),
                    self.user_id,
                    zone,
                    day,
                ),
            )
            conn.execute(
                "DELETE FROM archive_summary_jobs WHERE user_id=? AND timezone=? AND local_day=?",
                (self.user_id, zone, day),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"status": "built"}
