"""Once-per-episode diary continuity for notable completed sleep."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal


log = logging.getLogger("app.post_sleep_diary")


class PostSleepDiaryWorker:
    """Write one concrete-date diary memory after a notable sleep episode."""

    name = "post_sleep_diary"

    def __init__(
        self,
        *,
        sleep_store: Any,
        memory_store: Any,
        embedder: Any,
        client: Any,
        model_provider: Callable[[], str],
        user_name_provider: Callable[[], str],
        session_provider: Callable[[], str],
        min_hours: float = 3.0,
        short_sleep_hours: float = 4.0,
        interval_seconds: float = 600.0,
    ) -> None:
        self._sleep_store = sleep_store
        self._memory_store = memory_store
        self._embedder = embedder
        self._client = client
        self._model_provider = model_provider
        self._user_name_provider = user_name_provider
        self._session_provider = session_provider
        self._min_hours = max(0.0, float(min_hours))
        self._short_hours = max(self._min_hours, float(short_sleep_hours))
        self._interval = max(60.0, float(interval_seconds))

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def _candidate(self) -> tuple[Any | None, float]:
        episodes = self._sleep_store.list_episodes(limit=8)
        for episode in episodes:
            if not episode.ended_at or episode.diary_memory_id is not None:
                continue
            started = timephrase.parse_iso(episode.started_at)
            ended = timephrase.parse_iso(episode.ended_at)
            if started is None or ended is None:
                continue
            hours = max(0.0, (ended - started).total_seconds() / 3600.0)
            if hours < self._min_hours:
                continue
            local_start = timephrase.to_aware(started).astimezone()
            late = local_start.hour >= 1 and local_start.hour < 5
            notable = bool(
                episode.interruptions
                or episode.reason_text
                or episode.reason_code == "model_authored_goodnight"
                or late
                or hours <= self._short_hours
            )
            if notable:
                return episode, hours
        return None, 0.0

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        del now, last_run_at
        episode, _hours = self._candidate()
        return episode is not None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal:
        del now, last_run_at
        episode, _hours = self._candidate()
        return WorkSignal(
            1.0 if episode is not None else 0.0,
            "closed_sleep_episode",
            needs_llm=True,
        )

    def run(self) -> dict[str, Any]:
        episode, hours = self._candidate()
        if episode is None:
            return {"written": False, "reason": "no_notable_episode"}
        if not self._sleep_store.reserve_episode_output(episode.id, kind="diary"):
            return {"written": False, "reason": "already_claimed"}
        started = timephrase.parse_iso(episode.started_at)
        ended = timephrase.parse_iso(episode.ended_at)
        if started is None or ended is None:
            self._sleep_store.finish_episode_output(
                episode.id, kind="diary", memory_id=None
            )
            return {"written": False, "reason": "invalid_dates"}
        facts = {
            "start": timephrase.to_aware(started).astimezone().strftime(
                "%Y-%m-%d %H:%M %Z"
            ),
            "end": timephrase.to_aware(ended).astimezone().strftime(
                "%Y-%m-%d %H:%M %Z"
            ),
            "hours": round(hours, 1),
            "kind": episode.kind,
            "reason": episode.reason_text or episode.reason_code or "tired",
            "interruptions": len(episode.interruptions),
            "outcome": episode.outcome or "awake",
        }
        prompt = (
            timephrase.today_anchor()
            + "\n\n"
            + "Write one private first-person diary sentence as Aiko about this "
            "recorded sleep episode. Keep it concrete, natural, and under 55 words. "
            "Mention only details in the facts. Do not use brackets, tags, or JSON.\n"
            + timephrase.STORED_TEXT_TIME_RULE
            + "\n\n"
            + f"User name: {self._user_name_provider()}\nFacts: {facts}"
        )
        try:
            raw = self._client.chat(
                [{"role": "user", "content": prompt}],
                model=self._model_provider(),
                options={"temperature": 0.45, "num_predict": 120},
                think=False,
                surface="post_sleep_diary",
            )
            content = " ".join(str(raw or "").strip().strip("\"'`").split())[:500]
            if len(content) < 12 or timephrase.has_relative_deictic(content):
                raise ValueError("unsafe or empty post-sleep diary output")
            embedding = self._embedder.embed(content)
            memory = self._memory_store.add(
                content=content,
                kind="diary",
                embedding=embedding,
                salience=0.68,
                source_session=self._session_provider(),
                metadata={"sleep_episode_id": episode.id},
                tier="long_term",
                temporal_type="past_event",
                event_time=episode.ended_at,
                skip_dedupe=True,
            )
            if memory is None:
                raise RuntimeError("post-sleep diary insert returned no row")
            self._sleep_store.finish_episode_output(
                episode.id, kind="diary", memory_id=int(memory.id)
            )
            return {"written": True, "memory_id": int(memory.id), "episode_id": episode.id}
        except Exception:
            self._sleep_store.finish_episode_output(
                episode.id, kind="diary", memory_id=None
            )
            log.debug("post-sleep diary pass failed", exc_info=True)
            return {"written": False, "reason": "generation_failed"}


__all__ = ["PostSleepDiaryWorker"]
