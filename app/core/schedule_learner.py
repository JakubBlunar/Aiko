"""Schedule-learning IdleWorker (G2 personality backlog).

Builds a small, low-precision picture of when the user tends to be
present by bucketing ``messages.created_at`` timestamps and writing the
result into a single ``usual_hours`` field on
:class:`UserProfileStore`. Persona uses the rendered profile block
already, so once the field is populated Aiko can comment naturally
("Sunday morning vibes" / "haven't seen you in a few mornings") without
ever calling a tool.

Design choices:

* **No content stored.** Only the *timestamp* of each user message is
  read; the content column is never touched. The bucketed summary is
  even further removed from the actual messages.
* **Rolling window.** A 30-day window keeps the summary current without
  letting a long absence stale-pollute the field.
* **Local timezone.** Buckets are computed against the host's local
  time (same approach as the temporal humanizer in
  :mod:`app.core.rag_retriever`) so "evening" matches the user's
  intuition, not UTC.
* **Idempotent.** When the rendered string matches the existing
  ``usual_hours`` value the worker returns ``wrote=False`` without
  touching the DB. Cuts log noise and avoids confidence drift on a
  no-op pass.
* **Confidence proportional to samples.** Tiny inboxes don't write
  high-confidence claims; the field only becomes "loud" once there's
  enough signal to back it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable

from app.core.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase
    from app.core.settings import AgentSettings, MemorySettings
    from app.core.user_profile import UserProfileStore


log = logging.getLogger("app.schedule_learner")


# Hour-of-day buckets. Edges are inclusive on the lower end and
# exclusive on the upper. ``late`` straddles midnight (23-6) so a
# 02:00 message lands there, not in ``morning``.
_BUCKET_DEFINITIONS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("morning", tuple(range(6, 12))),       # 06:00 - 11:59
    ("afternoon", tuple(range(12, 18))),    # 12:00 - 17:59
    ("evening", tuple(range(18, 23))),      # 18:00 - 22:59
    ("late", (23, 0, 1, 2, 3, 4, 5)),       # 23:00 - 05:59
)


_HOUR_TO_BUCKET: dict[int, str] = {}
for _name, _hours in _BUCKET_DEFINITIONS:
    for _h in _hours:
        _HOUR_TO_BUCKET[_h] = _name


# Render-time labels keyed by ``(daytype, bucket)``. Daytype is
# ``"weekday"`` (Mon-Fri) or ``"weekend"`` (Sat-Sun).
_BUCKET_RENDER: dict[tuple[str, str], str] = {
    ("weekday", "morning"): "weekday mornings (06-12)",
    ("weekday", "afternoon"): "weekday afternoons (12-18)",
    ("weekday", "evening"): "weekday evenings (18-23)",
    ("weekday", "late"): "weekday late nights (23-06)",
    ("weekend", "morning"): "weekend mornings (06-12)",
    ("weekend", "afternoon"): "weekend afternoons (12-18)",
    ("weekend", "evening"): "weekend evenings (18-23)",
    ("weekend", "late"): "weekend late nights (23-06)",
}


# Top-N buckets to render in the final summary string. Two is enough
# to express the common patterns without producing a long sentence.
_MAX_BUCKETS_RENDERED = 2

# Minimum share of total samples a bucket needs before it counts as a
# "dominant" cluster. Stops a single rogue 03:00 message from
# polluting the rendered string.
_MIN_BUCKET_SHARE = 0.20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp string out of a SQLite row.

    Tolerates the trailing ``Z`` shorthand and naive timestamps (no
    timezone) which are assumed to be UTC. Returns ``None`` on any
    parse failure so a single bad row can't crash the whole sweep.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _classify_local(when_local: datetime) -> tuple[str, str]:
    """Return ``(daytype, bucket)`` for a local-tz ``when_local``."""
    daytype = "weekend" if when_local.weekday() >= 5 else "weekday"
    bucket = _HOUR_TO_BUCKET.get(when_local.hour, "late")
    return daytype, bucket


def _summarize_buckets(
    counts: dict[tuple[str, str], int],
    *,
    total: int,
    min_share: float = _MIN_BUCKET_SHARE,
    max_rendered: int = _MAX_BUCKETS_RENDERED,
) -> tuple[str, list[tuple[str, str, int, float]]]:
    """Return ``(rendered_str, top_clusters)``.

    ``top_clusters`` is a list of ``(daytype, bucket, count, share)``
    tuples sorted by count descending; useful for tests and logs.
    The rendered string is empty when no bucket clears ``min_share``.
    """
    if total <= 0:
        return "", []
    sorted_clusters = sorted(
        counts.items(), key=lambda item: item[1], reverse=True,
    )
    chosen: list[tuple[str, str, int, float]] = []
    for (daytype, bucket), count in sorted_clusters:
        share = count / total
        if share < min_share:
            continue
        chosen.append((daytype, bucket, int(count), float(share)))
        if len(chosen) >= max_rendered:
            break
    if not chosen:
        return "", []
    parts: list[str] = []
    seen_labels: set[str] = set()
    for daytype, bucket, _count, _share in chosen:
        label = _BUCKET_RENDER.get((daytype, bucket))
        if label is None or label in seen_labels:
            continue
        parts.append(label)
        seen_labels.add(label)
    return ", ".join(parts), chosen


def _confidence_from_samples(samples: int) -> float:
    """Sample-count -> confidence in [0, 0.95].

    The slope is gentle so a fresh DB doesn't claim a brittle 0.9
    after one busy evening; a long-running session lands close to the
    cap as the picture stabilises.
    """
    if samples <= 0:
        return 0.0
    raw = samples / 50.0
    return min(0.95, max(0.0, raw))


class ScheduleLearner:
    """IdleWorker that maintains a ``usual_hours`` user-profile field.

    Lightweight by design: no LLM call, no embedder, just SQL +
    Python time-bucket math. Fits the "small, periodic, idempotent"
    shape of every other ``IdleWorker`` in the project.
    """

    name = "schedule_learner"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        profile_store: "UserProfileStore",
        user_id_provider: Callable[[], str],
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._profile_store = profile_store
        self._user_id_provider = user_id_provider
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "schedule_learner_interval_seconds",
                86400,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(
            getattr(self._agent_settings, "schedule_learner_enabled", True)
        ):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        now = self._clock()
        window_days = max(
            1,
            int(
                getattr(
                    self._agent_settings,
                    "schedule_learner_window_days",
                    30,
                )
            ),
        )
        min_samples = max(
            1,
            int(
                getattr(
                    self._agent_settings,
                    "schedule_learner_min_samples",
                    5,
                )
            ),
        )
        cutoff = now - timedelta(days=window_days)
        log.info(
            "schedule-learner start: window_days=%d min_samples=%d cutoff=%s",
            window_days,
            min_samples,
            cutoff.isoformat(),
        )

        try:
            rows = self._fetch_user_message_timestamps(cutoff)
        except Exception:
            log.warning(
                "schedule-learner: SELECT failed", exc_info=True,
            )
            return {"errored": True}

        counts = self._bucket_rows(rows)
        total = sum(counts.values())
        log.info(
            "schedule-learner samples: total=%d unique_buckets=%d",
            total,
            len(counts),
        )

        if total < min_samples:
            log.info(
                "schedule-learner skip: total=%d below min_samples=%d",
                total,
                min_samples,
            )
            return {
                "samples": total,
                "wrote": False,
                "reason": "below_min_samples",
            }

        rendered, top = _summarize_buckets(counts, total=total)
        log.info(
            "schedule-learner buckets: top=%s rendered=%r",
            [(d, b, c) for d, b, c, _s in top],
            rendered or "<none>",
        )
        if not rendered:
            return {
                "samples": total,
                "wrote": False,
                "reason": "no_dominant_bucket",
            }

        user_id = self._resolve_user_id()
        if not user_id:
            log.info(
                "schedule-learner skip: no user_id from provider",
            )
            return {
                "samples": total,
                "wrote": False,
                "reason": "no_user_id",
            }

        existing = self._profile_store.fields(user_id).get("usual_hours")
        existing_value = (existing.value or "").strip() if existing else ""
        if existing_value == rendered:
            log.info(
                "schedule-learner upsert skipped: usual_hours unchanged "
                "(%r, samples=%d)",
                rendered,
                total,
            )
            return {
                "samples": total,
                "wrote": False,
                "reason": "unchanged",
                "value": rendered,
            }

        confidence = _confidence_from_samples(total)
        try:
            wrote = self._profile_store.upsert(
                user_id,
                "usual_hours",
                rendered,
                confidence,
            )
        except Exception:
            log.warning(
                "schedule-learner upsert failed", exc_info=True,
            )
            return {"samples": total, "wrote": False, "errored": True}
        log.info(
            "schedule-learner upsert: usual_hours=%r confidence=%.2f wrote=%s",
            rendered,
            confidence,
            bool(wrote),
        )
        return {
            "samples": total,
            "wrote": bool(wrote),
            "value": rendered,
            "confidence": float(confidence),
        }

    # ── helpers ──────────────────────────────────────────────────────

    def _fetch_user_message_timestamps(
        self, cutoff: datetime,
    ) -> list[tuple[Any, ...]]:
        """Pull ``created_at`` for user messages newer than ``cutoff``.

        The ``messages`` table only carries a composite
        ``(session_id, created_at)`` index, but the ``role='user'``
        filter combined with the cutoff keeps the row count tiny
        (per-day, not per-message) so a full scan is cheap enough.
        """
        return self._chat_db.execute_fetchall(
            "SELECT created_at FROM messages "
            "WHERE role = 'user' AND created_at >= ? "
            "ORDER BY created_at ASC",
            (cutoff.astimezone(timezone.utc).isoformat(),),
        )

    def _bucket_rows(
        self, rows: Iterable[tuple[Any, ...]],
    ) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for row in rows:
            if not row:
                continue
            dt = _parse_iso(row[0])
            if dt is None:
                continue
            local = dt.astimezone()
            key = _classify_local(local)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _resolve_user_id(self) -> str:
        try:
            return str(self._user_id_provider() or "").strip()
        except Exception:
            return ""


__all__ = ["ScheduleLearner"]
