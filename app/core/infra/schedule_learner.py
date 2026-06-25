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
  :mod:`app.core.rag.rag_retriever`) so "evening" matches the user's
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

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.infra.user_profile import UserProfileStore


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


# K3 (routine / ritual awareness) builds on the same bucket math but
# operates at finer granularity: weekday name × hour-bucket. Where
# G2 says "weekday evenings" (a daytype-coarse phrase), K3 says
# "Sunday-morning chats" (a named ritual). We only count a slot as a
# ritual if the user has touched it in several different ISO weeks —
# raw volume isn't enough, recurrence across weeks is what makes
# something a routine.
_WEEKDAY_NAMES: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


# Mon..Sun × 4 buckets = 28 deterministic labels. Names are written
# to read naturally inside the rendered profile block ("Routines:
# Sunday-morning chats, Friday-evening wind-downs"); they avoid the
# word "ritual" so the line doesn't feel surveillance-y. The labels
# stay constant across runs so a re-detection of the same slot
# produces the same string and the idempotent upsert short-circuits.
_RITUAL_LABELS: dict[tuple[str, str], str] = {
    ("monday", "morning"): "Monday-morning check-ins",
    ("monday", "afternoon"): "Monday-afternoon catch-ups",
    ("monday", "evening"): "Monday-evening unwinds",
    ("monday", "late"): "Monday-late nights",
    ("tuesday", "morning"): "Tuesday-morning check-ins",
    ("tuesday", "afternoon"): "Tuesday-afternoon catch-ups",
    ("tuesday", "evening"): "Tuesday-evening unwinds",
    ("tuesday", "late"): "Tuesday-late nights",
    ("wednesday", "morning"): "Wednesday-morning check-ins",
    ("wednesday", "afternoon"): "Wednesday-afternoon catch-ups",
    ("wednesday", "evening"): "Wednesday-evening unwinds",
    ("wednesday", "late"): "Wednesday-late nights",
    ("thursday", "morning"): "Thursday-morning check-ins",
    ("thursday", "afternoon"): "Thursday-afternoon catch-ups",
    ("thursday", "evening"): "Thursday-evening unwinds",
    ("thursday", "late"): "Thursday-late nights",
    ("friday", "morning"): "Friday-morning check-ins",
    ("friday", "afternoon"): "Friday-afternoon catch-ups",
    ("friday", "evening"): "Friday-evening wind-downs",
    ("friday", "late"): "Friday-late nights",
    ("saturday", "morning"): "Saturday-morning chats",
    ("saturday", "afternoon"): "Saturday-afternoon hangs",
    ("saturday", "evening"): "Saturday-evening wind-downs",
    ("saturday", "late"): "Saturday-late nights",
    ("sunday", "morning"): "Sunday-morning chats",
    ("sunday", "afternoon"): "Sunday-afternoon hangs",
    ("sunday", "evening"): "Sunday-evening unwinds",
    ("sunday", "late"): "Sunday-late nights",
}


# Hard cap on the rendered ``routines`` value to fit ``ProfileEntry``'s
# 240-char column. We additionally truncate the *number of clusters*
# via the user-facing ``routine_max_active`` setting; this is the
# safety net for pathological label widths.
_ROUTINES_VALUE_CAP = 240


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


def _weekday_name_local(when_local: datetime) -> str:
    """Return ``"monday"`` … ``"sunday"`` for a local-tz datetime."""
    return _WEEKDAY_NAMES[when_local.weekday()]


def _iso_week_key(when_local: datetime) -> tuple[int, int]:
    """Return ``(iso_year, iso_week)`` for a local-tz datetime.

    We key recurrence on the ISO calendar (year + week-of-year) so the
    set cardinality of "distinct weeks the slot was touched" is what
    drives the routine threshold. Using year+week (not just week) is
    what makes a 30-day window straddling Dec/Jan still count cleanly.
    """
    iso = when_local.isocalendar()
    return (int(iso[0]), int(iso[1]))


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


def _summarize_routines(
    weekly_seen: dict[tuple[str, str], set[tuple[int, int]]],
    *,
    total_weeks: int,
    min_touches: int,
    min_share: float,
    max_active: int,
) -> tuple[str, list[tuple[str, str, int, float]]]:
    """Return ``(rendered_str, top_clusters)`` for the K3 routines pass.

    ``weekly_seen`` is keyed by ``(weekday_name, bucket)`` and the
    value is the set of ``(iso_year, iso_week)`` tuples that lit up
    that slot. ``top_clusters`` is a list of
    ``(weekday, bucket, weeks_seen, share)`` tuples sorted by
    ``weeks_seen`` descending; useful for tests and logs.

    A cell qualifies as a routine when **both** thresholds clear:

    * ``len(weeks) >= min_touches`` — raw recurrence floor (e.g. 3
      different weeks). Stops a single busy week from minting a
      ritual.
    * ``len(weeks) / total_weeks >= min_share`` — proportional floor
      (e.g. 30% of weeks in the rolling window). Tightens the bar
      proportionally as the window grows; without this, a 6-month
      window could mint a "routine" off ~3 weeks.

    The rendered string is empty when no cell qualifies, when no
    cells map to a known label in ``_RITUAL_LABELS``, or when the
    join would exceed the 240-char ``ProfileEntry`` cap (we trim the
    cluster list rather than the string mid-name).
    """
    if total_weeks <= 0:
        return "", []
    qualifying: list[tuple[str, str, int, float]] = []
    for (weekday, bucket), weeks in weekly_seen.items():
        weeks_seen = len(weeks)
        if weeks_seen < min_touches:
            continue
        share = weeks_seen / float(total_weeks)
        if share < min_share:
            continue
        qualifying.append((weekday, bucket, int(weeks_seen), float(share)))
    if not qualifying:
        return "", []
    # Sort by weeks_seen descending; tie-break on weekday order so
    # the rendered output is deterministic across runs.
    weekday_order = {name: idx for idx, name in enumerate(_WEEKDAY_NAMES)}
    qualifying.sort(
        key=lambda item: (
            -item[2],
            weekday_order.get(item[0], 7),
            item[1],
        )
    )
    chosen: list[tuple[str, str, int, float]] = qualifying[:max_active]

    parts: list[str] = []
    seen_labels: set[str] = set()
    for weekday, bucket, _w, _s in chosen:
        label = _RITUAL_LABELS.get((weekday, bucket))
        if label is None or label in seen_labels:
            continue
        # If adding this label would push us past the column cap,
        # stop early — the trailing items are by construction the
        # less-recurrent ones, so dropping them is the right side
        # of the precision/recall trade-off.
        candidate = ", ".join(parts + [label])
        if len(candidate) > _ROUTINES_VALUE_CAP:
            break
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

        counts, weekly_seen = self._bucket_rows_extended(rows)
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

        result: dict[str, Any] = {
            "samples": total,
            "wrote": False,
        }

        # ── G2: usual_hours (coarse daytype phrase) ───────────────────
        if rendered:
            existing = self._profile_store.fields(user_id).get("usual_hours")
            existing_value = (existing.value or "").strip() if existing else ""
            if existing_value == rendered:
                log.info(
                    "schedule-learner upsert skipped: usual_hours unchanged "
                    "(%r, samples=%d)",
                    rendered,
                    total,
                )
                result["reason"] = "unchanged"
                result["value"] = rendered
            else:
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
                    "schedule-learner upsert: usual_hours=%r "
                    "confidence=%.2f wrote=%s",
                    rendered,
                    confidence,
                    bool(wrote),
                )
                result["wrote"] = bool(wrote) or result["wrote"]
                result["value"] = rendered
                result["confidence"] = float(confidence)
        else:
            result["reason"] = "no_dominant_bucket"

        # ── K3: routines (named ritual phrase) ────────────────────────
        # Detection runs against the same window as G2, but recurrence
        # is measured in distinct ISO weeks. ``total_weeks`` is the
        # ceiling of ``window_days / 7`` so a 30-day window gives 5
        # weeks of denominator regardless of where ``now`` lands inside
        # the ISO calendar.
        routines_rendered = ""
        routines_top: list[tuple[str, str, int, float]] = []
        if bool(
            getattr(
                self._agent_settings, "routine_detection_enabled", True,
            )
        ):
            min_touches = max(
                1,
                int(
                    getattr(
                        self._memory_settings,
                        "routine_min_touches",
                        3,
                    )
                ),
            )
            min_share = max(
                0.0,
                min(
                    1.0,
                    float(
                        getattr(
                            self._memory_settings,
                            "routine_min_share",
                            0.30,
                        )
                    ),
                ),
            )
            max_active = max(
                1,
                int(
                    getattr(
                        self._memory_settings,
                        "routine_max_active",
                        5,
                    )
                ),
            )
            total_weeks = max(1, (window_days + 6) // 7)
            routines_rendered, routines_top = _summarize_routines(
                weekly_seen,
                total_weeks=total_weeks,
                min_touches=min_touches,
                min_share=min_share,
                max_active=max_active,
            )
            log.info(
                "schedule-learner routines: total_weeks=%d top=%s rendered=%r",
                total_weeks,
                [(d, b, w) for d, b, w, _s in routines_top],
                routines_rendered or "<none>",
            )

            if routines_rendered:
                existing_routines = self._profile_store.fields(user_id).get(
                    "routines"
                )
                existing_routines_value = (
                    (existing_routines.value or "").strip()
                    if existing_routines
                    else ""
                )
                if existing_routines_value == routines_rendered:
                    log.info(
                        "schedule-learner upsert skipped: routines unchanged "
                        "(%r)",
                        routines_rendered,
                    )
                    result["routines_reason"] = "unchanged"
                    result["routines_value"] = routines_rendered
                else:
                    # Confidence is the max recurrence density of any
                    # chosen cell, capped at 0.95 like
                    # ``_confidence_from_samples``. A cell that
                    # happened in 4-of-5 weeks is "loud"; a cell that
                    # just clears the floor reads as a soft suggestion.
                    max_share = max(
                        (s for _d, _b, _w, s in routines_top), default=0.0,
                    )
                    routines_confidence = max(0.0, min(0.95, max_share))
                    try:
                        wrote_routines = self._profile_store.upsert(
                            user_id,
                            "routines",
                            routines_rendered,
                            routines_confidence,
                        )
                    except Exception:
                        log.warning(
                            "schedule-learner routines upsert failed",
                            exc_info=True,
                        )
                        wrote_routines = False
                    else:
                        log.info(
                            "schedule-learner upsert: routines=%r "
                            "confidence=%.2f wrote=%s",
                            routines_rendered,
                            routines_confidence,
                            bool(wrote_routines),
                        )
                    result["routines_wrote"] = bool(wrote_routines)
                    result["routines_value"] = routines_rendered
                    result["routines_confidence"] = float(routines_confidence)
                    if wrote_routines:
                        result["wrote"] = True

        return result

    # ── helpers ──────────────────────────────────────────────────────

    def _fetch_user_message_timestamps(
        self, cutoff: datetime,
    ) -> list[tuple[Any, ...]]:
        """Pull ``created_at`` for user messages newer than ``cutoff``.

        Served by ``idx_messages_role_created`` (``role, created_at``,
        added for P10): the ``role='user'`` equality + ``created_at >=``
        range maps straight onto the index, so this stays a bounded
        range scan rather than the full table scan it was before the
        index existed.
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
        """Daytype × bucket histogram (G2). See ``_bucket_rows_extended``
        for the per-weekday recurrence picture used by K3.
        """
        counts, _weekly_seen = self._bucket_rows_extended(rows)
        return counts

    def _bucket_rows_extended(
        self, rows: Iterable[tuple[Any, ...]],
    ) -> tuple[
        dict[tuple[str, str], int],
        dict[tuple[str, str], set[tuple[int, int]]],
    ]:
        """Single pass that builds both views the worker needs.

        Returns a tuple of:

        * ``counts`` — ``(daytype, bucket) -> total samples``. Same
          shape G2's ``_summarize_buckets`` consumes.
        * ``weekly_seen`` — ``(weekday_name, bucket) -> set of
          (iso_year, iso_week)``. Drives K3's recurrence detection;
          the set cardinality is the "weeks_seen" axis.
        """
        counts: dict[tuple[str, str], int] = {}
        weekly_seen: dict[tuple[str, str], set[tuple[int, int]]] = {}
        for row in rows:
            if not row:
                continue
            dt = _parse_iso(row[0])
            if dt is None:
                continue
            local = dt.astimezone()
            key = _classify_local(local)
            counts[key] = counts.get(key, 0) + 1

            weekday = _weekday_name_local(local)
            bucket = _HOUR_TO_BUCKET.get(local.hour, "late")
            slot = (weekday, bucket)
            weekly_seen.setdefault(slot, set()).add(_iso_week_key(local))
        return counts, weekly_seen

    def _resolve_user_id(self) -> str:
        try:
            return str(self._user_id_provider() or "").strip()
        except Exception:
            return ""


__all__ = [
    "ScheduleLearner",
    "_classify_local",
    "_summarize_buckets",
    "_summarize_routines",
    "_confidence_from_samples",
    "_RITUAL_LABELS",
    "_WEEKDAY_NAMES",
]
