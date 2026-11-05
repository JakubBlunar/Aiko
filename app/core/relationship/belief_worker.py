"""Infer new, source-linked interpretations without requiring established concepts.

The persisted per-session watermark prevents heartbeat replay. Only actual user
quotes in the budgeted prompt may support a reading; older and assistant turns
provide reference context. Repeated inference never verifies a user's state.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from app.core.relationship.belief_store import (
    BeliefStore,
    SOURCE_WORKER,
)
from app.core.memory.fact_check_privacy import scrub_claim_for_search
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count
from app.core.infra import timephrase
from app.core.proactive.worker_prompt_budget import (
    pack_worker_prompt, report_worker_prompt,
)
from app.core.relationship.belief_interpretation import (
    VERSION, SCOPE_HOURS, Interpretation, system_prompt,
    parse_interpretations, is_interpretation, is_current,
)

if TYPE_CHECKING:
    from app.core.concepts.concept_view import ConceptView
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
# the budget. Two complete, concise interpretations need room for receipts.
_EXTRACT_MAX_TOKENS = 1400


# Maximum entries we accept from one extraction pass, regardless of
# what the model returns. Zero is normal; a noisy turn cannot fill the
# store with several paraphrases of the same familiar feeling.
_MAX_BELIEFS_PER_RUN = 2


# L28: how many durable concept labels reach the extraction prompt. The
# diet's token budget governs how much is *read*; this is how much is
# worth spending prompt on. A prior only has to point the extractor
# somewhere -- a long list of them starts competing with the transcript
# that is supposed to decide.
_MAX_CONCEPT_HINTS = 5


# Ask for an OBJECT wrapping the array, matching the memory extractor.
# Ollama's ``format: "json"`` constrains generation to a JSON object, so a
# prompt asking for a bare array cannot be satisfied: the model reasons to
# the right answer and the grammar then emits ``{}``. See
# :mod:`app.llm.json_answers`.
def _build_system_prompt(user_name: str = "the user") -> str:
    return system_prompt((user_name or "").strip() or "the user")


# ``{{`` / ``}}`` because this template goes through ``str.format`` — a
# literal ``{"beliefs"`` would be read as a replacement field and raise
# ``KeyError`` on every run.
_USER_TEMPLATE = (
    "Transcript (user evidence and adjacent reference context):\n{transcript}\n\n"
    'Return one JSON object shaped {{"beliefs": [...]}}.'
)


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


def _coerce_labels(raw: Any) -> list[str]:
    """Normalise an interest-map payload to an ordered list of labels.

    Accepts anything iterable whose items are bare label strings,
    ``(label, size)`` tuples, or objects exposing a ``.label`` attribute
    (e.g. :class:`app.core.conversation.topic_graph.InterestEntry`).
    Blank labels are dropped; order + de-dupe are preserved so the
    caller's "densest first" ordering survives.
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    try:
        items = list(raw)
    except TypeError:
        return []
    for item in items:
        label: Any = None
        if isinstance(item, str):
            label = item
        elif hasattr(item, "label"):
            label = item.label
        elif isinstance(item, (tuple, list)) and item:
            label = item[0]
        text = str(label or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


# Tiny tokenizer for the reconsider-candidate overlap. Words of length
# >= 3 only, so trivial joiners ("the", "a", "of") don't manufacture a
# match between an interest label and a belief topic.
_TOPIC_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def _topic_words(text: str | None) -> set[str]:
    return set(_TOPIC_WORD_RE.findall((text or "").lower()))


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
        # The **scoped** ``user_id:session_id`` key, not the bare session
        # id: that is what ``messages.session_id`` actually holds, and
        # every chat-db read here keys on it exactly. Wired to the bare id
        # this worker finds no transcript on any run, forever, while
        # reporting the benign-looking "no recent user turns".
        session_key_provider: Callable[[], str | None],
        user_id_provider: Callable[[], str],
        user_names_provider: Callable[[], list[str]] | None = None,
        assistant_name_provider: Callable[[], str | None] | None = None,
        notify_belief_added: Callable[[dict[str, Any]], None] | None = None,
        notify_belief_updated: Callable[[dict[str, Any]], None] | None = None,
        interest_map_provider: Callable[[], Any] | None = None,
        view_provider: Callable[[], "ConceptView | None"] | None = None,
        clock: Callable[[], datetime] | None = None,
        context_window: Callable[[], int | None] | None = None,
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
        self._session_key_provider = session_key_provider
        self._user_id_provider = user_id_provider
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._notify_belief_added = notify_belief_added
        self._notify_belief_updated = notify_belief_updated
        # K65b: returns the K9 interest map (densest topic clusters). Any
        # iterable of items exposing ``.label`` / ``.size`` or ``(label,
        # size)`` tuples / bare label strings is accepted; ``None`` /
        # missing keeps the legacy flat-transcript behaviour.
        self._interest_map_provider = interest_map_provider
        # L28: late-bound ConceptView, read once per run for the durable
        # prior. ``None`` leaves the prompt exactly as K65b built it.
        self._view_provider = view_provider
        self._clock = clock or _utcnow
        self._context_window = context_window or (lambda: None)

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
        """Both enable flags, plus budget for the extraction call."""
        fresh = self._fresh_turns(now, last_run_at)
        return fresh is not None and fresh[0] > 0

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Pressure from transcript that has not been mined yet.

        This worker has no backlog to count — it re-reads the last
        dozen turns and asks a model what the user believes. Run it
        against an unchanged transcript and it spends a generation to
        re-derive tuples it already has, so the honest signal is *new
        material*: how many messages have landed since the last run,
        against the lookback window the extraction actually reads.

        A session with no new user turns is not ready. Heartbeats and process
        restarts cannot re-mine a window already covered by the persisted cursor.
        """
        fresh = self._fresh_turns(now, last_run_at)
        if fresh is None:
            return WorkSignal(pressure=0.0, reason="disabled or no budget")
        new_messages, window = fresh
        if new_messages <= 0:
            return WorkSignal(pressure=0.0, reason="no new turns")
        return WorkSignal(
            pressure=pressure_from_count(new_messages, saturation=window),
            reason=f"{new_messages} new messages",
            needs_llm=True,
        )

    def _fresh_turns(
        self, now: datetime, last_run_at: datetime | None,
    ) -> tuple[int, int] | None:
        """``(new_messages, window)`` if a run could extract, else ``None``.

        ``snapshot`` rather than ``allow``: ``run`` spends the token,
        a probe never may.
        """
        if not bool(
            getattr(self._agent_settings, "belief_tracking_enabled", True)
        ):
            return None
        if not bool(
            getattr(self._agent_settings, "belief_worker_enabled", True)
        ):
            return None
        lookback_turns = int(
            getattr(
                self._belief_settings, "belief_worker_lookback_turns", 12,
            )
        )
        if lookback_turns <= 0:
            return None
        try:
            snapshot = self._rate_limiter.snapshot(now)
            if snapshot["hour_used"] >= snapshot["hour_cap"]:
                return None
            if snapshot["day_used"] >= snapshot["day_cap"]:
                return None
            session_key = (
                self._session_key_provider()
                if self._session_key_provider else None
            )
            if not session_key:
                return None
            watermark = self._watermark(session_key)
            rows = self._chat_db.get_messages(session_key, limit=max(2, lookback_turns * 2))
            fresh = sum(r.role == "user" and r.id > watermark for r in rows)
        except Exception:
            log.debug("belief-worker demand probe failed", exc_info=True)
            return None
        return fresh, max(1, lookback_turns * 2)

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

        session_key = (
            self._session_key_provider() if self._session_key_provider else None
        )
        if not session_key:
            return {"skipped": True, "reason": "no_session"}

        user_id = self._user_id_provider()
        lookback_turns = int(
            getattr(self._belief_settings, "belief_worker_lookback_turns", 12)
        )
        if lookback_turns <= 0:
            return {"skipped": True, "reason": "lookback_zero"}

        now = self._clock()
        source_ids: list[int] = []
        offered: dict[int, dict[str, Any]] = {}
        watermark = self._watermark(session_key)
        transcript = self._snapshot_transcript(
            session_key=session_key, lookback_turns=lookback_turns,
            source_ids=source_ids, offered=offered, watermark=watermark,
        )
        if not transcript:
            # An empty window is normal; a session key that matches *no*
            # message ever is a wiring fault, and the two look identical
            # from here. Separating them costs one COUNT on a path that
            # already decided to do nothing, and the un-separated version
            # hid a mis-scoped key for three months.
            if self._session_is_unknown(session_key):
                log.warning(
                    "belief-worker: session key %r matches no messages at "
                    "all -- expected the scoped 'user_id:session_id' form. "
                    "The worker cannot mine anything until this is fixed.",
                    session_key,
                )
                return {"skipped": True, "reason": "unknown_session"}
            log.info(
                "belief-worker skip: no recent user turns session=%s",
                session_key,
            )
            return {"skipped": True, "reason": "no_user_turns"}

        if not any(mid > watermark for mid in source_ids):
            return {"skipped": True, "reason": "no_new_user_turns"}

        # Rate-limit gate.
        if not self._rate_limiter.allow(now):
            log.info(
                "belief-worker skip: rate-limited session=%s",
                session_key,
            )
            return {"skipped": True, "reason": "rate_limited"}

        user_names = self._user_names_provider() if self._user_names_provider else None
        assistant_name = (
            self._assistant_name_provider() if self._assistant_name_provider else None
        )
        # The belief extractor runs on the LOCAL maintenance model, which
        # the privacy threat model treats as trusted. Scrubbing the
        # transcript with the outbound web-search PII gate would strip the
        # first/second-person pronouns + names the theory-of-mind pass
        # relies on, so it's off by default. Opt in only when the worker
        # model is routed to an untrusted endpoint.
        if bool(
            getattr(self._agent_settings, "belief_worker_scrub_transcript", False)
        ):
            scrubbed = scrub_claim_for_search(
                transcript,
                user_names=user_names,
                assistant_name=assistant_name,
            )
            if not scrubbed:
                log.info(
                    "belief-worker skip: privacy-blocked transcript session=%s "
                    "raw_chars=%d",
                    session_key,
                    len(transcript),
                )
                return {"skipped": True, "reason": "privacy_blocked"}
        else:
            scrubbed = transcript

        log.info(
            "belief-worker start: session=%s lookback_turns=%d "
            "raw_chars=%d scrubbed_chars=%d preview=%r",
            session_key,
            lookback_turns,
            len(transcript),
            len(scrubbed),
            _preview(scrubbed),
        )

        # K65b: bias extraction toward the user's densest K9 interests and
        # fold a few "still true?" re-checks into the *same* LLM call. Both
        # are best-effort and degrade to the legacy flat prompt on a cold
        # store (no labelled clusters / no active beliefs).
        interest_labels = self._interest_labels()
        interest_hint = ""
        reconsider_block = ""
        reconsider_count = 0
        if interest_labels:
            clean_labels = self._scrub_terms(
                interest_labels, user_names, assistant_name,
            )
            if clean_labels:
                interest_hint = ", ".join(clean_labels)
            reconsider_topics = self._reconsider_topics(
                user_id=self._user_id_provider(), labels=interest_labels,
            )
            clean_reconsider = self._scrub_terms(
                reconsider_topics, user_names, assistant_name,
            )
            reconsider_count = len(clean_reconsider)
            if clean_reconsider:
                reconsider_block = "; ".join(clean_reconsider)
            if interest_hint or reconsider_block:
                log.info(
                    "belief-worker interest-bias: interests=%d reconsider=%d",
                    len(interest_labels),
                    reconsider_count,
                )

        # L28: the durable layer as a third prior. K2 beliefs stay
        # transient -- nothing here touches the store, its confidence
        # semantics or its lifecycle; the concepts only shape what the
        # extractor goes looking for.
        concept_hint = self._concept_hint(user_names, assistant_name)
        if concept_hint:
            log.info(
                "belief-worker concept-bias: chars=%d", len(concept_hint),
            )

        existing_rows = self._belief_store.list_recent(
            user_id=user_id, limit=80,
        )
        live_words = _topic_words(transcript)
        existing_rows.sort(key=lambda b: (
            len(_topic_words(b.topic + " " + b.predicted_state) & live_words),
            is_interpretation(b.metadata), b.observed_at,
        ), reverse=True)
        existing_rows = existing_rows[:8]
        existing_hint = self._existing_hint(existing_rows, user_names, assistant_name)
        # Captured rows are also used after generation; only offered ids may be merged.
        candidates = {b.id: b for b in existing_rows}
        self._offered_source_ids = set()
        t0 = time.monotonic()
        dropped_shape: list[str] = []
        tuples = self._extract_with_llm(
            scrubbed,
            interest_hint=interest_hint,
            reconsider_block=reconsider_block,
            concept_hint=concept_hint,
            existing_hint=existing_hint,
            dropped=dropped_shape,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if tuples is None:
            log.info(
                "belief-worker llm-unparseable elapsed_ms=%.0f session=%s",
                elapsed_ms,
                session_key,
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
        dropped_invalid = 0
        skipped_self_tag = 0
        if self._user_id_provider() != user_id or self._session_key_provider() != session_key:
            return {"skipped": True, "reason": "context_changed"}
        for t in tuples[:_MAX_BELIEFS_PER_RUN]:
            if self._cancel_event.is_set():
                return {"skipped": True, "reason": "cancelled_before_write"}
            evidence = self._validate_evidence(t, offered, watermark)
            if evidence is None:
                dropped_invalid += 1
                continue
            target = candidates.get(t.existing_id) if t.existing_id is not None else None
            if t.existing_id is not None and (
                t.existing_id not in self._offered_existing_ids or target is None
                or not is_interpretation(target.metadata)
                or target.status not in ("active", "confirmed")
                or not is_current(target, now)
                or target.metadata.get("session_key") != session_key
                or target.metadata.get("scope") != t.scope
                or target.metadata.get("manually_edited")
                or t.interpretation != target.predicted_state
                or any(getattr(t, key) != target.metadata.get(key)
                       for key in ("situation", "uncertainty", "revise_when"))
            ):
                dropped_invalid += 1
                continue
            if t.existing_id is None:
                duplicate = next((b for b in existing_rows
                                  if is_interpretation(b.metadata)
                                  and b.status in ("active", "confirmed")
                                  and b.metadata.get("session_key") == session_key
                                  and b.metadata.get("scope") == t.scope
                                  and all(getattr(t, key) == b.metadata.get(key)
                                          for key in ("situation", "uncertainty", "revise_when"))
                                  and " ".join(b.predicted_state.lower().split())
                                      == " ".join(t.interpretation.lower().split())), None)
                if duplicate is not None:
                    t.topic = duplicate.topic
                    t.kind = duplicate.kind
            # A topic collision is a revision, not another observation. The
            # store preserves the old interpretation's bounded receipt/history.
            topic = target.topic if target is not None else t.topic
            embedding = None
            try:
                embedding = self._embedder.embed(t.interpretation)
            except Exception:
                log.debug("belief-worker: interpretation embedding failed", exc_info=True)
            if target is not None:
                # Preserve the exact canonical claim and qualifiers, rather
                # than letting a similarity score erase a material distinction.
                old = self._belief_store.get(target.id)
                if (old is None or old.predicted_state != target.predicted_state
                        or old.status != target.status or old.metadata != target.metadata):
                    dropped_invalid += 1
                    continue
                topic = old.topic
                t.kind = old.kind
                t.interpretation = old.predicted_state
            try:
                support_time = max(timephrase.to_aware(datetime.fromisoformat(e["created_at"]))
                                   for e in evidence)
            except (TypeError, ValueError):
                dropped_invalid += 1
                continue
            checked_at = timephrase.to_aware(self._clock())
            end = support_time + timedelta(hours=SCOPE_HOURS[t.scope])
            if support_time > checked_at or end <= checked_at:
                dropped_invalid += 1
                continue
            support_at, expires_at = support_time.isoformat(), end.isoformat()
            metadata = {
                "interpretation_version": VERSION, "reason": t.reason,
                "situation": t.situation, "uncertainty": t.uncertainty,
                "revise_when": t.revise_when, "response_hint": t.response_hint,
                "scope": t.scope, "novelty": t.novelty, "evidence": evidence,
                "session_key": session_key, "last_supported_at": support_at,
                "expires_at": expires_at, "source_window_message_ids": source_ids,
            }
            belief = self._belief_store.upsert(
                user_id=user_id, kind=t.kind, topic=topic,
                predicted_state=t.interpretation, confidence=t.confidence,
                source=SOURCE_WORKER,
                source_message_id=max(e["message_id"] for e in evidence),
                metadata=metadata, topic_embedding=embedding, observed_at=support_at,
            )
            if belief is None:
                dropped_invalid += 1
                continue
            upserted += 1
            existed = any(b.id == belief.id for b in existing_rows)
            existing_rows = [b for b in existing_rows if b.id != belief.id] + [belief]
            notify = self._notify_belief_updated if existed else self._notify_belief_added
            if notify is None:
                notify = self._notify_belief_added
            if notify is not None:
                try:
                    notify(belief.to_payload())
                except Exception:
                    log.debug("belief-worker: notify raised", exc_info=True)

        # An empty, valid result is processed too. Failed calls and cancellation
        # remain retryable; restarts retain the same intake boundary.
        processed_ids = self._offered_source_ids & set(source_ids)
        unusable_format = bool(dropped_shape) and not tuples
        if not self._cancel_event.is_set() and processed_ids and not unusable_format:
            self._chat_db.kv_set(self._watermark_key(session_key), str(max(processed_ids)))

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
            "dropped_shape": len(dropped_shape),
            "retryable_format": unusable_format,
            "pruned": pruned,
            "llm_ms": round(elapsed_ms, 1),
        }
        if dropped_shape:
            # Reasons, not just a count: "the model keeps writing dates"
            # and "the model keeps writing clauses" want different fixes,
            # and the prompt is where either is actually fixed.
            result["shape_faults"] = sorted(
                {f.split(":")[0] for f in dropped_shape}
            )
        log.info("belief-worker done: %s", result)
        return result

    def _watermark_key(self, session_key: str) -> str:
        return f"belief_worker.v2.watermark:{self._user_id_provider()}:{session_key}"

    def _watermark(self, session_key: str) -> int:
        try:
            return int(self._chat_db.kv_get(self._watermark_key(session_key)) or 0)
        except (ValueError, TypeError):
            return 0

    def _existing_hint(self, rows, user_names, assistant_name) -> str:
        lines = []
        for b in rows:
            text = (f"id={b.id} [{b.status}; supported {b.observed_at}] "
                    f"{b.topic}: {b.predicted_state}; scope={b.metadata.get('scope', 'unknown')}; "
                    f"situation={b.metadata.get('situation', 'legacy, scope unknown')}; "
                    f"uncertainty={b.metadata.get('uncertainty', 'unknown')}; "
                    f"revise_when={b.metadata.get('revise_when', 'unknown')}")
            if bool(getattr(self._agent_settings, "belief_worker_scrub_transcript", False)):
                text = scrub_claim_for_search(text, user_names=user_names,
                                              assistant_name=assistant_name)
            if text:
                lines.append(text)
        return "\n".join(lines)

    def _validate_evidence(self, t, offered, watermark):
        evidence = []
        for e in t.evidence:
            row = offered.get(e["message_id"])
            if (row is None or e["message_id"] not in self._offered_source_ids
                    or row["role"] != "user" or e["quote"] not in row["content"]
                    or e["quote"] not in self._offered_source_text.get(e["message_id"], "")
                    or not row.get("created_at")):
                return None
            evidence.append({"message_id": e["message_id"], "quote": e["quote"],
                             "created_at": row["created_at"]})
        return evidence if any(e["message_id"] > watermark for e in evidence) else None

    # ── K65b: interest-map bias helpers ──────────────────────────────

    def _interest_labels(self) -> list[str]:
        """Top high-mass K9 cluster labels, or ``[]`` when disabled/cold."""
        if not bool(
            getattr(self._agent_settings, "belief_interest_bias_enabled", True)
        ):
            return []
        if self._interest_map_provider is None:
            return []
        top_n = int(
            getattr(self._belief_settings, "belief_worker_interest_top_n", 5)
        )
        if top_n <= 0:
            return []
        try:
            raw = self._interest_map_provider()
        except Exception:
            log.debug(
                "belief-worker: interest_map_provider raised", exc_info=True
            )
            return []
        return _coerce_labels(raw)[:top_n]

    def _reconsider_topics(
        self, *, user_id: str, labels: list[str]
    ) -> list[str]:
        """Stalest believed topics that sit on a top interest.

        ``list_believed`` is ``observed_at DESC`` so we walk it in reverse
        (oldest-observed first) and keep the first ``reconsider_max`` whose
        topic shares a content word with any high-mass interest label.
        Corroborated beliefs are included: they are the ones Aiko actually
        says out loud, so they are the ones worth re-checking.
        """
        max_n = int(
            getattr(self._belief_settings, "belief_worker_reconsider_max", 3)
        )
        if max_n <= 0 or not labels:
            return []
        try:
            active = self._belief_store.list_believed(user_id=user_id, limit=200)
        except Exception:
            log.debug("belief-worker: list_believed raised", exc_info=True)
            return []
        if not active:
            return []
        label_words = _topic_words(" ".join(labels))
        if not label_words:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for belief in reversed(active):
            topic = (getattr(belief, "topic", "") or "").strip()
            key = topic.lower()
            if not topic or key in seen:
                continue
            if _topic_words(topic) & label_words:
                out.append(topic)
                seen.add(key)
                if len(out) >= max_n:
                    break
        return out

    def _concept_hint(
        self,
        user_names: list[str] | None,
        assistant_name: str | None,
    ) -> str:
        """L28: what she durably holds about him, as an extraction prior.

        The two K65b hints are both *topic* signals -- which subjects he
        keeps returning to and which earlier beliefs are due a re-check.
        Neither says anything about what he is *like*, so the extractor
        infers a passing mood with no model of the person it is inferring
        about, and the same transcript reads the same way whether he is
        someone who withdraws under pressure or someone who gets louder.

        Scrubbed on the same path as the interest labels, because a
        concept label is free text about a named person and the worker may
        be routed to an untrusted endpoint.

        Deliberately one-directional: this reads concepts and writes
        nothing back. K2's transient half stays transient -- a belief is a
        prediction about right now, and promoting one on the strength of
        agreeing with a durable concept is how a layer starts confirming
        itself.
        """
        if self._view_provider is None:
            return ""
        try:
            view = self._view_provider()
        except Exception:
            log.debug("belief-worker: view_provider raised", exc_info=True)
            return ""
        if view is None or not getattr(view, "enabled", False):
            return ""
        try:
            rows = view.for_consumer("belief_inference")
        except Exception:
            log.debug("belief-worker: for_consumer failed", exc_info=True)
            return ""
        labels = [
            " ".join(str(getattr(c, "label", "") or "").split())
            for c in rows[:_MAX_CONCEPT_HINTS]
        ]
        clean = self._scrub_terms(
            [line for line in labels if line], user_names, assistant_name,
        )
        return "; ".join(clean)

    def _scrub_terms(
        self,
        terms: list[str],
        user_names: list[str] | None,
        assistant_name: str | None,
    ) -> list[str]:
        """Privacy-scrub each short term; drop any that scrub to empty."""
        out: list[str] = []
        for term in terms:
            s = (term or "").strip()
            if not s:
                continue
            scrubbed = scrub_claim_for_search(
                s, user_names=user_names, assistant_name=assistant_name,
            )
            if scrubbed and scrubbed.strip():
                out.append(scrubbed.strip())
        return out

    # ── transcript snapshot ──────────────────────────────────────────

    def _resolve_user_name(self) -> str:
        """First configured user name, or ``"the user"`` fallback."""
        names = (
            self._user_names_provider() if self._user_names_provider else None
        )
        if names:
            first = (str(names[0]) or "").strip()
            if first:
                return first
        return "the user"

    def _session_is_unknown(self, session_key: str) -> bool:
        """True when the key names a session the message store never saw."""
        try:
            return int(self._chat_db.get_message_count(session_key)) <= 0
        except Exception:
            log.debug("belief-worker session probe failed", exc_info=True)
            return False

    def _snapshot_transcript(
        self, *, session_key: str, lookback_turns: int,
        source_ids: list[int] | None = None,
        offered: dict[int, dict[str, Any]] | None = None, watermark: int = 0,
    ) -> str:
        rows = self._chat_db.get_messages(session_key, limit=lookback_turns * 2)
        users = [r for r in rows if r.role == "user"][-lookback_turns:]
        if not users:
            return ""
        first_id = users[0].id
        # Include the question just before the oldest offered user response.
        preceding = next((r.id for r in reversed(rows)
                          if r.id < first_id and r.role == "assistant"), first_id)
        first_id = preceding
        assistant_name = (self._assistant_name_provider()
                          if self._assistant_name_provider else None) or "Aiko"
        chunks = []
        for row in rows:
            if row.id < first_id or row.role not in ("user", "assistant"):
                continue
            text = " ".join((row.content or "").splitlines()).strip()[:600]
            if not text:
                continue
            record = {"role": row.role, "content": text, "created_at": row.created_at}
            if offered is not None:
                offered[row.id] = record
            if row.role == "user" and source_ids is not None:
                source_ids.append(row.id)
            tag = "NEW" if row.role == "user" and row.id > watermark else "context only"
            rendered = timephrase.format_transcript(
                [record], self._clock(),
                role_labels={"user": self._resolve_user_name(), "assistant": assistant_name},
            )
            chunks.append(f"[message_id={row.id}; {tag}] {rendered}")
        return "\n".join(chunks)

    # ── LLM extractor ────────────────────────────────────────────────

    def _extract_with_llm(
        self,
        scrubbed_transcript: str,
        *,
        interest_hint: str = "",
        reconsider_block: str = "",
        concept_hint: str = "",
        existing_hint: str = "",
        dropped: list[str] | None = None,
    ) -> list[Interpretation] | None:
        system_content = (
            f"{timephrase.today_anchor(self._clock())}\n\n"
            f"{_build_system_prompt(self._resolve_user_name())}\n\n"
            f"{timephrase.STORED_TEXT_TIME_RULE}"
        )
        lines = scrubbed_transcript.splitlines()
        newest_first = list(reversed(lines))
        optional_hints = sum(bool(hint) for hint in (
            interest_hint, reconsider_block, concept_hint,
        ))

        def render(selected) -> list[dict[str, str]]:
            sections = [_USER_TEMPLATE.format(transcript="\n".join(reversed(selected)))]
            if existing_hint:
                sections.append(
                    "Existing readings (novelty baseline, NOT evidence; do not "
                    "rediscover paraphrases; preserve meaningful qualifiers):\n" + existing_hint
                )
            if interest_hint:
                sections.append(
                    "Topics this user keeps returning to (prioritise beliefs "
                    f"about these when the transcript supports it): {interest_hint}."
                )
            if concept_hint:
                sections.append(
                    "What you durably hold about this user (a prior on what to "
                    "look for, not evidence -- the transcript decides, and it "
                    "may well contradict one of these): "
                    f"{concept_hint}."
                )
            if reconsider_block:
                sections.append(
                    "Also re-check whether these earlier beliefs still hold: if a "
                    "transcript turn speaks to one, return an updated belief for "
                    f"that topic; otherwise ignore it: {reconsider_block}."
                )
            return [
                {"role": "system", "content": system_content},
                {"role": "user", "content": "\n\n".join(sections)},
            ]

        try:
            context_window = self._context_window()
        except Exception:
            log.warning("belief_worker: context window lookup failed", exc_info=True)
            context_window = None
        if not context_window or context_window <= 0:
            log.warning("belief_worker: unknown context window; using 4096")
        output_tokens = _EXTRACT_MAX_TOKENS + max(
            0, int(getattr(self._ollama, "_think_headroom", 0)),
        )
        packed = None
        selected_optional = optional_hints
        for optional_count in range(6, -1, -1):
            candidate = pack_worker_prompt(
                newest_first, render, context_window=context_window,
                output_tokens=output_tokens, surface="belief_worker", log=log,
                report=False,
            )
            if candidate is not None and (
                packed is None or candidate.item_count > packed.item_count
            ):
                packed = candidate
                selected_optional = sum(bool(hint) for hint in (
                    interest_hint, reconsider_block, concept_hint,
                ))
            if packed is not None and packed.item_count == len(newest_first):
                break
            if optional_count == 6:
                reconsider_block = ""
            elif optional_count == 5:
                interest_hint = ""
            elif optional_count == 4:
                concept_hint = ""
            elif optional_count == 3:
                existing_hint = "\n".join(existing_hint.splitlines()[:3])
            elif optional_count == 2:
                existing_hint = "\n".join(existing_hint.splitlines()[:1])
            elif optional_count == 1:
                existing_hint = ""
        if packed is None:
            log.warning("belief_worker: no transcript line fits the prompt budget")
            return None
        report_worker_prompt(packed, len(newest_first), surface="belief_worker", log=log)
        if selected_optional < optional_hints:
            log.warning(
                "belief_worker: omitted %d optional hints",
                optional_hints - selected_optional,
            )
        messages = packed.messages
        # Receipts must refer to the actual budgeted/scrubbed prompt, not all
        # rows fetched before packing. Optional hints contain no message_id tags.
        self._offered_source_ids = {
            int(mid) for mid in re.findall(r"\[message_id=(\d+);", messages[1]["content"])
        }
        user_content = messages[1]["content"]
        self._offered_source_text = {
            int(match.group(1)): match.group(0)
            for match in re.finditer(r"(?m)^\[message_id=(\d+);[^\n]*", user_content)
        }
        self._offered_existing_ids = {
            int(mid) for mid in re.findall(r"(?m)^id=(\d+) ", user_content)
        }
        if "message_id=" in scrubbed_transcript and not any(
            "; NEW]" in line for line in self._offered_source_text.values()
        ):
            log.warning("belief_worker: no new user evidence fits the prompt")
            return None
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "belief-worker extract prompt: model=%s prompt_chars=%d "
                "user_payload=%r",
                self._chat_model,
                len(user_content) + len(system_content),
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
            # An empty answer is a failed call, not "no beliefs": the
            # reasoning trace consumed the whole num_predict budget before
            # the answer began (the client already retried once without
            # it). Reporting [] would make the failure indistinguishable
            # from a quiet transcript.
            log.info("belief-worker llm-empty-answer: no answer tokens at all")
            return None
        log.debug(
            "belief-worker extract raw: chars=%d preview=%r",
            len(raw),
            _preview(raw),
        )
        return self._parse_tuples(raw, dropped=dropped)

    @staticmethod
    def _parse_tuples(raw: str, *, dropped: list[str] | None = None):
        return parse_interpretations(raw, dropped if dropped is not None else [])
