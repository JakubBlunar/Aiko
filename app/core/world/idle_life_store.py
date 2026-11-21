"""Fourteen days of structured idle-life evidence, independent of chat surfacing."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
from uuid import uuid4

from app.core.infra import timephrase
from app.core.world.idle_life_trace import bounded

log = logging.getLogger("app.idle_life")
RETENTION_DAYS = 14


def real_stamp():
    return timephrase.real_now().astimezone(timezone.utc).isoformat()


def runtime_fingerprint():
    root = Path(__file__).parents[3]
    digest = hashlib.sha256()
    paths = list((root / "app/core/world").glob("*.py"))
    paths.extend(root / name for name in (
        "app/core/proactive/idle_worker_scheduler.py", "app/core/proactive/hobby_worker.py",
        "app/core/infra/timephrase.py", "app/core/concepts/concept_diets.py",
    ))
    for path in sorted(paths):
        if path.exists():
            digest.update(str(path.relative_to(root)).replace("\\", "/").encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


class IdleLifeStore:
    def __init__(self, db, *, observation_seconds=None):
        self._db = db
        self._observation_seconds = observation_seconds
        self._lock = threading.RLock()
        self._observations = {}
        self._last_prune = 0.0
        self.failed_writes = 0
        self.last_error = None
        self.boot_id = uuid4().hex
        self.fingerprint = runtime_fingerprint()
        self.prune()
        self.record("startup", "scheduler", {"boot_id": self.boot_id,
                    "runtime_fingerprint": self.fingerprint, "schema": 1,
                    "retention_days": RETENTION_DAYS})

    def record(self, kind, worker, payload, *, run_id="", actor="autonomous",
               event_key=None, conn=None):
        """A supplied transaction owns commit/rollback, including its effect event."""
        own = conn is None
        try:
            conn = conn or self._db._get_conn()
            stamp = real_stamp()
            body = json.dumps(bounded(payload), ensure_ascii=True, allow_nan=False)
            if len(body) > 60000:
                body = json.dumps({"payload_omitted": "size_limit", "bytes": len(body)})
            cursor = conn.execute(
                "INSERT OR IGNORE INTO idle_life_events "
                "(event_key,run_id,worker,actor,kind,occurred_at,recorded_at,last_seen_at,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (event_key, run_id, worker, actor, kind, timephrase.utcnow().isoformat(),
                 stamp, stamp, body),
            )
            if own:
                conn.commit()
            return cursor.lastrowid if cursor.rowcount else None
        except Exception as exc:
            self.failed_writes += 1
            self.last_error = type(exc).__name__
            log.warning("idle life trace write failed: %s", type(exc).__name__)
            # Atomic effect capture must not silently lose its event.
            if not own:
                raise
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return None

    def observe(self, worker, reason, payload):
        """Transition immediately; persist unchanged observations at most once/minute."""
        import time

        stamp = real_stamp()
        now = self._observation_seconds() if self._observation_seconds else time.monotonic()
        signature = json.dumps(bounded(payload), sort_keys=True)
        with self._lock:
            previous = self._observations.get(worker)
            if (previous and previous[0] == reason and previous[1] == signature
                    and now - previous[5] < 300 and now - previous[6] < 90):
                previous[6] = now
                previous[4] += 1
                if now - previous[3] < 60:
                    return
                try:
                    conn = self._db._get_conn()
                    conn.execute("UPDATE idle_life_events SET last_seen_at=?,observations=?, "
                                 "payload=json_set(payload,'$.last_narrative_at',?) WHERE id=?",
                                 (stamp, previous[4], timephrase.utcnow().isoformat(), previous[2]))
                    conn.commit()
                    previous[3] = now
                except Exception as exc:
                    self.failed_writes += 1
                    self.last_error = type(exc).__name__
                    try:
                        self._db._get_conn().rollback()
                    except Exception:
                        pass
                    log.warning("idle life interval update failed: %s", type(exc).__name__)
                return
            event_id = self.record("admission", worker, {
                "reason": reason, **payload, "boot_id": self.boot_id,
                "last_narrative_at": timephrase.utcnow().isoformat(),
            })
            if event_id:
                self._observations[worker] = [reason, signature, event_id, now, 1, now, now]
            if now - self._last_prune >= 3600:
                self.prune()
                self._last_prune = now

    def prune(self):
        cutoff = (timephrase.real_now().astimezone(timezone.utc)
                  - timedelta(days=RETENTION_DAYS)).isoformat()
        conn = self._db._get_conn()
        try:
            conn.execute("DELETE FROM idle_life_events WHERE last_seen_at < ?", (cutoff,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _row(row):
        keys = ("id", "event_key", "run_id", "worker", "actor", "kind", "occurred_at",
                "recorded_at", "last_seen_at", "observations", "payload")
        result = dict(zip(keys, row, strict=True))
        result["payload"] = json.loads(result["payload"])
        return result

    def history(self, *, hours=72, before_id=None, worker=None, actor=None, kind=None,
                run_id=None, limit=50):
        hours = max(1, min(float(hours), RETENTION_DAYS * 24))
        cutoff = (timephrase.real_now().astimezone(timezone.utc)
                  - timedelta(hours=hours)).isoformat()
        clauses, params = ["last_seen_at>=?"], [cutoff]
        for column, value in (("worker", worker), ("actor", actor), ("kind", kind),
                              ("run_id", run_id)):
            if value:
                clauses.append(column + "=?")
                params.append(str(value))
        if before_id is not None:
            clauses.append("id<?")
            params.append(int(before_id))
        count = max(1, min(int(limit), 200))
        rows = self._db._get_conn().execute(
            "SELECT * FROM idle_life_events WHERE " + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?", (*params, count + 1),
        ).fetchall()
        return {"events": [self._row(row) for row in rows[:count]],
                "next_before_id": rows[count - 1][0] if len(rows) > count else None,
                "retention_days": RETENTION_DAYS, "requested_hours": hours,
                "telemetry": self.health()}

    def event(self, event_id):
        row = self._db._get_conn().execute(
            "SELECT * FROM idle_life_events WHERE id=?", (int(event_id),),
        ).fetchone()
        return self._row(row) if row is not None else None

    @classmethod
    def read_decision(cls, conn, run_id):
        """Read one trace using a supplied connection, including from backups."""
        rows = conn.execute(
            "SELECT * FROM idle_life_events WHERE run_id=? ORDER BY id LIMIT 501",
            (str(run_id),),
        ).fetchall()
        return {"events": [cls._row(row) for row in rows[:500]], "truncated": len(rows) > 500}

    def decision(self, run_id):
        return {**self.read_decision(self._db._get_conn(), run_id), "telemetry": self.health()}

    def health(self):
        row = self._db._get_conn().execute(
            "SELECT MIN(recorded_at), MAX(last_seen_at), COUNT(*) FROM idle_life_events",
        ).fetchone()
        return {"oldest_recorded_at": row[0], "latest_observed_at": row[1],
                "retained_events": row[2], "failed_writes_since_boot": self.failed_writes,
                "last_error": self.last_error, "boot_id": self.boot_id,
                "runtime_fingerprint": self.fingerprint,
                "coverage_note": "History starts at capture; gaps are unknown, not inactivity."}

    def summary(self, *, hours=72):
        hours = max(1, min(float(hours), RETENTION_DAYS * 24))
        cutoff = (timephrase.real_now().astimezone(timezone.utc)
                  - timedelta(hours=hours)).isoformat()
        conn = self._db._get_conn()
        counts = conn.execute(
            "SELECT worker,kind,COUNT(*) FROM idle_life_events "
            "WHERE last_seen_at>=? GROUP BY worker,kind", (cutoff,),
        ).fetchall()
        holds = conn.execute(
            "SELECT payload,COUNT(*),SUM(observations) FROM idle_life_events "
            "WHERE last_seen_at>=? AND kind='admission' GROUP BY json_extract(payload,'$.reason')",
            (cutoff,),
        ).fetchall()
        actions = Counter()
        for (raw,) in conn.execute(
            "SELECT payload FROM idle_life_events WHERE recorded_at>=? AND kind='action_effect'",
            (cutoff,),
        ):
            actions[json.loads(raw).get("action", "unknown")] += 1
        from app.core.world.idle_life_review import review_events

        evidence = (self._row(row) for row in conn.execute(
            "SELECT * FROM idle_life_events WHERE last_seen_at>=? ORDER BY id", (cutoff,),
        ))
        review = review_events(evidence, cutoff=timephrase.parse_iso(cutoff),
                               window_end=timephrase.real_now())
        return {**review, "hours": hours, "counts": [dict(worker=w, kind=k, count=n)
                                            for w, k, n in counts],
                "admission": [dict(reason=json.loads(p).get("reason"), intervals=n,
                                   observations=o) for p, n, o in holds],
                "actions": dict(actions), "telemetry": self.health(),
                "interpretation": "Runs, effects, modeled spans and rest are separate; "
                                  "counts are not a productivity score."}
