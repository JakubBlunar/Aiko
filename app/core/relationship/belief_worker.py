"""Belief inference worker (K2 personality backlog).

Periodic background worker that mines Aiko's recent conversation for
fresh theory-of-mind beliefs about the user -- mood predictions
("Jacob is excited about the tokyo trip") and topical opinions
("Jacob thinks Rust is overhyped") -- and upserts them into the
:class:`app.core.relationship.belief_store.BeliefStore`.

Pipeline (one tick = one ``run`` call):

1. Snapshot the last ``belief_worker_lookback_turns`` (default 12)
   user messages from the active session via
   :meth:`ChatDatabase.get_messages`.
2. Privacy-scrub the lookback transcript via
   :func:`fact_check_privacy.scrub_claim_for_search` so any PII /
   private tokens never reach the LLM prompt. Mirrors F1 / G3.
3. Spend one LLM call through the dedicated
   :class:`FactCheckRateLimiter`
   (``state_key='belief_worker.rate_state'``) asking for a JSON
   **array** of belief tuples ``{kind, topic, predicted_state,
   confidence}``.
4. For each accepted tuple: compute a topic embedding via the
   provided :class:`Embedder`, then call
   :meth:`BeliefStore.upsert`. The store handles its own
   (user_id, kind, topic) dedupe + fuzzy-topic merge.
5. Cap the user's active-belief count at
   ``belief_max_active_per_user`` via
   :meth:`BeliefStore.prune_to_cap`.

The self-tag fast path (``[[predict:...]]``) wins over the worker:
:meth:`BeliefStore.upsert` for an existing ``self_tag`` row simply
refreshes its state; the worker writes ``source='worker'`` for
brand-new beliefs only. Higher-confidence self-tag rows are not
overwritten by lower-confidence worker rows because ``upsert``
always overwrites with the latest value -- so we apply the
self-tag wins guard inside the worker loop (skip upserting if a
self-tagged active belief already exists for the topic).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.relationship.belief_store import (
    BeliefStore,
    KIND_MOOD,
    KIND_OPINION,
    SOURCE_SELF_TAG,
    SOURCE_WORKER,
    VALID_KINDS,
)
from app.core.memory.fact_check_privacy import scrub_claim_for_search
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.infra.settings import AgentSettings
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.belief_worker")


# Cap on how much of any text we render in a single log line. Mirrors
# the F1 / F5 / G3 worker convention.
_LOG_PREVIEW_CHARS = 200


# Cap on the LLM response so a malformed answer can't run away with
# the budget. ~6 beliefs * ~50 chars each = ~300 tokens.
_EXTRACT_MAX_TOKENS = 350


# Maximum entries we accept from one extraction pass, regardless of
# what the model returns. Tuned so a single noisy turn can't flood
# the store; the next tick picks up anything we dropped.
_MAX_BELIEFS_PER_RUN = 6


# Match the F1 / F5 / G3 prompt convention: a JSON array on one line.
_SYSTEM_PROMPT = (
    "You read a short chat transcript and infer what the user "
    "believes or feels about specific topics. Return ONE JSON array "
    "(no prose, no markdown) of zero or more belief objects. Each "
    "object has these fields:\n"
    "  - kind: 'mood' (predicts how the user feels about the topic) "
    "or 'opinion' (predicts what the user thinks about the topic).\n"
    "  - topic: 2-60 char short topic phrase, lowercase, no quotes.\n"
    "  - predicted_state: 2-80 char state phrase (e.g. 'excited', "
    "'nervous', 'overhyped', 'a clever idea').\n"
    "  - confidence: 0.0-1.0 decimal -- how sure you are.\n"
    "Be conservative: skip the turn if the transcript doesn't "
    "actually let you predict anything. Never invent beliefs from "
    "thin air. Return an empty array `[]` when there's nothing to "
    "report. Output the JSON array and nothing else."
)


_USER_TEMPLATE = (
    "Transcript (most recent user turns):\n{transcript}\n\n"
    "Return one JSON array of beliefs the user holds."
)


_JSON_ARRAY_RE = re.compile(r"\[.*\]", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


@dataclass(slots=True)
class _BeliefTuple:
    """One belief returned by the LLM extractor."""

    kind: str
    topic: str
    predicted_state: str
    confidence: float


class BeliefInferenceWorker:
    """IdleWorker that mines recent turns for theory-of-mind beliefs."""

    name = "belief_worker"

    def __init__(
        self,
        *,
        belief_store: BeliefStore,
        chat_db: "ChatDatabase",
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        belief_settings: Any,
        session_id_provider: Callable[[], str | None],
        user_id_provider: Callable[[], str],
        user_names_provider: Callable[[], list[str]] | None = None,
        assistant_name_provider: Callable[[], str | None] | None = None,
        notify_belief_added: Callable[[dict[str, Any]], None] | None = None,
        notify_belief_updated: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._belief_store = belief_store
        self._chat_db = chat_db
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._belief_settings = belief_settings
        self._session_id_provider = session_id_provider
        self._user_id_provider = user_id_provider
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._notify_belief_added = notify_belief_added
        self._notify_belief_updated = notify_belief_updated
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._belief_settings,
                "belief_worker_interval_seconds",
                3600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(
            getattr(self._agent_settings, "belief_tracking_enabled", True)
        ):
            return False
        if not bool(
            getattr(self._agent_settings, "belief_worker_enabled", True)
        ):
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        snapshot = self._rate_limiter.snapshot(now)
        if snapshot["hour_used"] >= snapshot["hour_cap"]:
            return False
        if snapshot["day_used"] >= snapshot["day_cap"]:
            return False
        return True

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "belief_tracking_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled_tracking"}
        if not bool(
            getattr(self._agent_settings, "belief_worker_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled_worker"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        session_id = self._session_id_provider() if self._session_id_provider else None
        if not session_id:
            return {"skipped": True, "reason": "no_session"}

        lookback_turns = int(
            getattr(
                self._belief_settings,
                "belief_worker_lookback_turns",
                12,
            )
        )
        if lookback_turns <= 0:
            return {"skipped": True, "reason": "lookback_zero"}

        now = self._clock()
        transcript = self._snapshot_transcript(
            session_id=session_id, lookback_turns=lookback_turns,
        )
        if not transcript:
            log.info(
                "belief-worker skip: no recent user turns session=%s",
                session_id,
            )
            return {"skipped": True, "reason": "no_user_turns"}

        # Rate-limit gate.
        if not self._rate_limiter.allow(now):
            log.info(
                "belief-worker skip: rate-limited session=%s",
                session_id,
            )
            return {"skipped": True, "reason": "rate_limited"}

        # Privacy scrub the joined transcript. If the scrubber blocks
        # the whole thing (only PII), we bail without spending the LLM
        # call -- but the rate-limit token has been consumed, which is
        # fine and mirrors the F1 fact-checker contract.
        user_names = self._user_names_provider() if self._user_names_provider else None
        assistant_name = (
            self._assistant_name_provider() if self._assistant_name_provider else None
        )
        scrubbed = scrub_claim_for_search(
            transcript,
            user_names=user_names,
            assistant_name=assistant_name,
        )
        if not scrubbed:
            log.info(
                "belief-worker skip: privacy-blocked transcript session=%s "
                "raw_chars=%d",
                session_id,
                len(transcript),
            )
            return {"skipped": True, "reason": "privacy_blocked"}

        log.info(
            "belief-worker start: session=%s lookback_turns=%d "
            "raw_chars=%d scrubbed_chars=%d preview=%r",
            session_id,
            lookback_turns,
            len(transcript),
            len(scrubbed),
            _preview(scrubbed),
        )

        t0 = time.monotonic()
        tuples = self._extract_with_llm(scrubbed)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if tuples is None:
            log.info(
                "belief-worker llm-unparseable elapsed_ms=%.0f session=%s",
                elapsed_ms,
                session_id,
            )
            return {
                "skipped": True,
                "reason": "llm_unparseable",
                "llm_ms": round(elapsed_ms, 1),
            }

        log.info(
            "belief-worker llm done: tuples=%d elapsed_ms=%.0f",
            len(tuples),
            elapsed_ms,
        )

        upserted = 0
        skipped_self_tag = 0
        dropped_invalid = 0
        user_id = self._user_id_provider()
        for t in tuples[:_MAX_BELIEFS_PER_RUN]:
            if self._cancel_event.is_set():
                break
            if t.kind not in VALID_KINDS:
                dropped_invalid += 1
                continue
            # Self-tag wins guard: if Aiko already self-tagged a belief
            # for this exact (kind, topic) and it's active, leave it
            # alone -- her deliberate guess outranks the worker's
            # inference.
            existing = self._belief_store.list_recent(
                user_id=user_id,
                kind=t.kind,
                limit=1,
            )
            if existing:
                row = existing[0]
                if (
                    row.topic == t.topic.lower()
                    and row.status == "active"
                    and row.source == SOURCE_SELF_TAG
                    and row.confidence >= t.confidence
                ):
                    skipped_self_tag += 1
                    continue
            embedding = None
            try:
                embedding = self._embedder.embed(t.topic)
            except Exception:
                log.debug(
                    "belief-worker: embedder raised for topic=%r",
                    t.topic,
                    exc_info=True,
                )
            belief = self._belief_store.upsert(
                user_id=user_id,
                kind=t.kind,
                topic=t.topic,
                predicted_state=t.predicted_state,
                confidence=float(t.confidence),
                source=SOURCE_WORKER,
                topic_embedding=embedding,
                observed_at=now.isoformat(),
            )
            if belief is None:
                dropped_invalid += 1
                continue
            upserted += 1
            log.info(
                "belief-worker upsert: id=%s kind=%s topic=%r state=%r "
                "confidence=%.2f",
                belief.id,
                belief.kind,
                belief.topic,
                belief.predicted_state,
                belief.confidence,
            )
            if self._notify_belief_added is not None:
                try:
                    self._notify_belief_added(belief.to_payload())
                except Exception:
                    log.debug(
                        "belief-worker: notify_belief_added raised",
                        exc_info=True,
                    )

        # Prune any per-user excess. Cap is a hard ceiling on
        # ``active`` rows; we don't touch confirmed / contradicted /
        # stale audit history.
        cap = int(
            getattr(
                self._belief_settings,
                "belief_max_active_per_user",
                200,
            )
        )
        pruned = self._belief_store.prune_to_cap(user_id=user_id, cap=cap)
        if pruned:
            log.info(
                "belief-worker pruned %d rows to cap=%d for user=%s",
                pruned,
                cap,
                user_id,
            )

        result = {
            "tuples_returned": len(tuples),
            "upserted": upserted,
            "skipped_self_tag": skipped_self_tag,
            "dropped_invalid": dropped_invalid,
            "pruned": pruned,
            "llm_ms": round(elapsed_ms, 1),
        }
        log.info("belief-worker done: %s", result)
        return result

    # ── transcript snapshot ──────────────────────────────────────────

    def _snapshot_transcript(
        self,
        *,
        session_id: str,
        lookback_turns: int,
    ) -> str:
        """Join the last N user messages into one prompt block.

        Assistant turns are intentionally omitted: the worker mines
        user beliefs, not Aiko's own speech. We cap each user message
        at 600 chars so a long rant can't blow the budget.
        """
        rows = self._chat_db.get_messages(session_id, limit=lookback_turns * 2)
        user_msgs = [r for r in rows if r.role == "user"]
        if not user_msgs:
            return ""
        user_msgs = user_msgs[-lookback_turns:]
        chunks: list[str] = []
        for row in user_msgs:
            text = (row.content or "").strip()
            if not text:
                continue
            if len(text) > 600:
                text = text[:597] + "\u2026"
            chunks.append("- " + text)
        return "\n".join(chunks)

    # ── LLM extractor ────────────────────────────────────────────────

    def _extract_with_llm(self, scrubbed_transcript: str) -> list[_BeliefTuple] | None:
        user_content = _USER_TEMPLATE.format(transcript=scrubbed_transcript)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "belief-worker extract prompt: model=%s prompt_chars=%d "
                "user_payload=%r",
                self._chat_model,
                len(user_content) + len(_SYSTEM_PROMPT),
                _preview(user_content),
            )
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _EXTRACT_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                # Inferring what the user believes/feels from recent
                # messages is a theory-of-mind judgement; reasoning lifts
                # quality. num_predict stays the answer budget.
                think=True,
                surface="belief_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("belief-worker extract call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return []
        log.debug(
            "belief-worker extract raw: chars=%d preview=%r",
            len(raw),
            _preview(raw),
        )
        return self._parse_tuples(raw)

    @staticmethod
    def _parse_tuples(raw: str) -> list[_BeliefTuple] | None:
        """Parse the LLM's JSON-array response into typed tuples.

        Returns ``None`` only when the response is fundamentally
        un-parseable (no JSON array found at all). An empty array
        returns ``[]`` -- a perfectly valid "nothing to report" turn.
        """
        match = _JSON_ARRAY_RE.search(raw or "")
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        out: list[_BeliefTuple] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            topic = str(item.get("topic", "")).strip().lower()
            state = str(item.get("predicted_state", "")).strip()
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            if kind not in (KIND_MOOD, KIND_OPINION):
                continue
            if not topic or len(topic) > 60:
                continue
            if not state or len(state) > 120:
                continue
            confidence = max(0.0, min(1.0, confidence))
            out.append(
                _BeliefTuple(
                    kind=kind,
                    topic=topic,
                    predicted_state=state,
                    confidence=confidence,
                )
            )
        return out
