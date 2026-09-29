"""Speaking-window worker that tracks the conversation's present situation."""
from __future__ import annotations

import json
import hashlib
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable

from app.core.conversation.conversation_situation import (
    ConversationSituationState,
    ConversationSituationStore,
    parse_extraction,
    reduce_situation,
    substantive_interest_answer,
)
from app.core.conversation import judgment_shadow
from app.core.infra import timephrase
from app.core.proactive.topic_match import topical

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase, MessageRow
    from app.core.proactive.cue_producer import CueProducer
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.conversation_situation")

_SYSTEM_PROMPT = """\
You are Aiko's present-situation observer. Read a short, age-tagged transcript,
the authoritative structured-world snapshot, and the previous observation.
Track what is genuinely happening in the conversation now.

The situation vocabulary is open-ended. Do not force it into a catalogue.
Capture a shared activity/place only when the recent transcript establishes
that Aiko and the user are participating together. A remembered ritual or a
possible plan is not current evidence. The world snapshot wins: never claim
that Aiko is already somewhere or doing something contradicted by it.

Return ONE compact JSON object without indentation or commentary:
{
  "operation": "keep|replace|clear",
  "summary": "short durable description with no relative time words",
  "shared": true|false,
  "shared_activity": "open-ended activity or empty string",
  "place_ref": "place named in the conversation or empty string",
  "aiko_activity": "what Aiko is doing in this situation or empty string",
  "evidence_message_ids": [integer ids from the supplied transcript],
    "explicit_end": true|false,
    "successor": null or {
        "kind": "comparison|shared_project|aiko_pursuit",
        "subject": "specific interest, 2-6 words",
        "change": {"text": "what the answer changed", "evidence_message_ids": [id]}
    },
    "working_set": null or {
        "question": {"text": "open question/goal", "evidence_message_ids": [id]},
        "facts": [{"text": "reported fact", "evidence_message_ids": [id]}],
        "interpretation": {"text": "tentative conclusion", "evidence_message_ids": [id]},
        "unresolved": {"text": "missing premise", "evidence_message_ids": [id]},
        "recall_needed": false
    }
}

Use replace when recent evidence establishes or materially changes the present
situation. Use keep when the previous observation still fits and nothing
material changed. Use clear when it no longer describes the present; set
explicit_end=true only when the transcript or authoritative world state
directly ends/contradicts it. Never include a confidence field.

Maintain a working_set only for one explicit, still-open user/shared question.
Set recall_needed true only when the unresolved premise is material to that
question, is absent from the supplied facts, and could be answered by prior
conversation or memory. Never use it for curiosity, an external search, an
action, or a premise that the user has just supplied. Prefer false when unsure.
Its question must cite a user message. Every note must cite 1-3 supplied message
IDs, all also included in the top-level evidence_message_ids (at most 8).
Use at most three facts. Attribute reports to their speaker; an assistant's
suggestion is not an established fact. Use at most one tentative interpretation
and one unresolved premise, or null. Each note is at most 160 characters.
Store concise conclusions, never a reasoning trace. Do not invent goals or
resolve ambiguous references by guessing. Replace the whole set on each keep
or replace: corrections invalidate dependent interpretations. Use null when
resolved, declined, changed to an unrelated topic, or unsupported. This tracks
understanding only; it grants no task, action, or permission to speak.

Usually successor is null. Only when a previous working-set question about
an explicitly shared project/comparison or Aiko-owned pursuit is answered
with substantive new information, close that question and optionally name
ONE new observation the answer earns. Cite the new USER answer and include
its IDs in the top-level evidence list. Never repeat the old ask, draft a
new question, treat agreement as new information, or claim an activity was
performed. A pivot, refusal, acknowledgement or resolved routine request
earns no successor. A successor is optional material, not an obligation.
"""


class ConversationSituationWorker:
    """Periodic worker-model interpretation with deterministic persistence."""

    def __init__(
        self,
        *,
        client: "ChatClient | None",
        chat_db: "ChatDatabase",
        store: ConversationSituationStore,
        model: str | None,
        world_snapshot_provider: Callable[[], dict[str, Any]],
        context_token_provider: Callable[[], tuple[str, int]] | None = None,
        successor_producer: "CueProducer | None" = None,
        every_n_user_turns: int = 2,
        max_history_messages: int = 14,
        max_tokens: int = 1536,
        judgment_shadow_enabled: bool = True,
    ) -> None:
        self._client = client
        self._chat_db = chat_db
        self._store = store
        self._model = model
        self._world_snapshot_provider = world_snapshot_provider
        self._context_token_provider = context_token_provider
        self._successor_producer = successor_producer
        self._every_n = max(1, int(every_n_user_turns))
        self._max_history = max(4, int(max_history_messages))
        self._max_tokens = max(100, int(max_tokens))
        self._judgment_shadow_enabled = judgment_shadow_enabled
        self._judgment_shadow = judgment_shadow.JudgmentShadowStore(chat_db)
        self._turns_seen: dict[str, int] = defaultdict(int)
        self._turns_at_last_run: dict[str, int] = defaultdict(int)
        self._last_user_message_id: dict[str, int] = {}
        self._last_result: dict[str, dict[str, Any]] = {}
        self._last_live_run_at: dict[str, float] = {}
        self._stats = {
            "scheduled": 0,
            "completed": 0,
            "failed": 0,
            "invalid": 0,
            "output_limit_hits": 0,
            "stale": 0,
            "successors_queued": 0,
            "kept": 0,
            "replaced": 0,
            "cleared": 0,
            "shadow_recorded": 0,
            "shadow_failed": 0,
        }

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def notify_user_turn(
        self, session_id: str, *, message_id: int | None = None
    ) -> None:
        key = str(session_id)
        if message_id is not None:
            current_id = int(message_id)
            if self._last_user_message_id.get(key) == current_id:
                return
            self._last_user_message_id[key] = current_id
        self._turns_seen[key] += 1

    def should_run(self, session_id: str) -> bool:
        key = str(session_id)
        if self._client is None or not self._model:
            return False
        return (
            self._turns_seen[key] - self._turns_at_last_run[key]
        ) >= self._every_n

    def mark_scheduled(self, session_id: str) -> None:
        key = str(session_id)
        self._turns_at_last_run[key] = self._turns_seen[key]
        self._stats["scheduled"] += 1

    def should_run_live(
        self,
        session_id: str,
        *,
        inferred_stale: bool,
        has_new_journal: bool,
        min_interval_s: float = 180.0,
        now_mono: float | None = None,
    ) -> bool:
        """Wall-clock Live cadence. Does not count as a user turn."""
        if self._client is None or not self._model:
            return False
        if not (inferred_stale or has_new_journal):
            return False
        key = str(session_id)
        now = time.monotonic() if now_mono is None else float(now_mono)
        last = self._last_live_run_at.get(key)
        if last is not None and (now - last) < max(30.0, float(min_interval_s)):
            return False
        return True

    def mark_live_run(self, session_id: str, *, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else float(now_mono)
        self._last_live_run_at[str(session_id)] = now
        self._stats["scheduled"] += 1

    def stats(self, session_id: str | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {
            **self._stats,
            "every_n_user_turns": self._every_n,
            "judgment_shadow_enabled": self._judgment_shadow_enabled,
        }
        if session_id is not None:
            key = str(session_id)
            report.update(
                {
                    "turns_seen": self._turns_seen[key],
                    "turns_since_run": (
                        self._turns_seen[key] - self._turns_at_last_run[key]
                    ),
                    "last_result": dict(self._last_result.get(key, {})),
                }
            )
        return report

    def judgment_shadow_report(
        self, session_id: str, *, include_rows=False, include_evidence=False, limit=20,
    ):
        return {
            **self._judgment_shadow.report(
                session_id, include_rows=include_rows,
                include_evidence=include_evidence, limit=limit,
            ),
            "enabled": self._judgment_shadow_enabled,
        }

    def run(self, session_id: str) -> ConversationSituationState | None:
        key = str(session_id)
        rows = []
        target = None
        started = time.monotonic()
        try:
            token = self._context_token_provider() if self._context_token_provider else None
            if token is not None and token[0] != key:
                self._stats["stale"] += 1
                return self._store.get(key)
            rows = self._chat_db.get_messages(key, limit=self._max_history)
            if not rows:
                return self._store.get(key)
            previous = self._store.get(key)
            prompt = self._build_user_prompt(rows, previous)
            target = None
            try:
                if self._judgment_shadow_enabled:
                    target = judgment_shadow.target_exchange(rows)
                    if target and self._judgment_shadow.has_review(key, target[1].id):
                        target = None
            except Exception:
                self._stats["shadow_failed"] += 1
                target = None
            shadow_prompt = judgment_shadow.PROMPT if target else ""
            if target:
                prompt += "\n\nSHADOW TARGET:\n" + json.dumps({
                    "user_message_id": target[0].id, "assistant_message_id": target[1].id,
                })
            started = time.monotonic()
            raw, usage = self._client.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            f"{timephrase.today_anchor()}\n\n"
                            f"{_SYSTEM_PROMPT}\n\n"
                            f"{timephrase.STORED_TEXT_TIME_RULE}"
                            + ("\n\n" + shadow_prompt if shadow_prompt else "")
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={
                    "temperature": 0,
                    "num_predict": self._max_tokens + (
                        judgment_shadow.OUTPUT_BUDGET if target else 0
                    ),
                },
                think=False,
                surface="conversation_situation",
            )
            call_ms = round((time.monotonic() - started) * 1000)
            output_limit_hit = getattr(usage, "done_reason", "") == "length"
            if output_limit_hit:
                self._stats["output_limit_hits"] += 1
            valid_ids = {int(row.id) for row in rows}
            latest_rows = self._chat_db.get_messages(key, limit=1)
            current_token = self._context_token_provider() if self._context_token_provider else None
            if (
                current_token != token
                or any(int(row.id) > max(valid_ids) for row in latest_rows)
                or self._store.get(key) != previous
            ):
                self._stats["stale"] += 1
                self._last_result[key] = {"result": "stale", "preserved_previous": True}
                self._record_shadow(
                    key, rows, target, raw, "stale", call_ms, output_limit_hit, usage,
                )
                return self._store.get(key)
            extraction = parse_extraction(
                raw, valid_message_ids=valid_ids,
                valid_user_message_ids={int(row.id) for row in rows if row.role == "user"},
            )
            if extraction is None:
                self._stats["invalid"] += 1
                self._last_result[key] = {
                    "result": "invalid_output",
                    "preserved_previous": previous is not None,
                    "output_limit_hit": output_limit_hit,
                }
                self._record_shadow(
                    key, rows, target, raw, "invalid_situation", call_ms, output_limit_hit, usage,
                )
                return previous
            source_message_id = max(valid_ids)
            state = reduce_situation(
                previous,
                extraction,
                session_id=key,
                source_message_id=source_message_id,
            )
            if state is not None:
                self._store.upsert(state)
            self._sync_interest_successor(key, previous, extraction, rows)
            self._stats["completed"] += 1
            self._stats[
                {
                    "keep": "kept",
                    "replace": "replaced",
                    "clear": "cleared",
                }[extraction.operation]
            ] += 1
            self._last_result[key] = {
                "result": extraction.operation,
                "status": state.status if state is not None else "empty",
                "generation": state.generation if state is not None else 0,
                "source_message_id": source_message_id,
            }
            self._record_shadow(key, rows, target, raw, "fresh", call_ms, output_limit_hit, usage)
            return state
        except Exception:
            self._stats["failed"] += 1
            self._record_shadow(
                key, rows, target, "", "call_failed",
                round((time.monotonic() - started) * 1000), False,
            )
            try:
                preserved = self._store.get(key)
            except Exception:
                preserved = None
            self._last_result[key] = {
                "result": "failed",
                "preserved_previous": preserved is not None,
            }
            log.debug("conversation situation worker failed", exc_info=True)
            return preserved

    def _record_shadow(
        self, key, rows, target, raw, outcome, call_ms, output_limit_hit, usage=None,
    ):
        if target is None:
            return
        try:
            if outcome == "fresh":
                for source in rows:
                    current = self._chat_db.get_message_row(source.id)
                    if current is None or (
                        current.session_id, current.role, current.content
                    ) != (source.session_id, source.role, source.content):
                        outcome = "changed_evidence"
                        break
            replay = judgment_shadow.heuristic_replay(rows, target)
            review = (
                judgment_shadow.parse_review(raw, rows, target)
                if outcome == "fresh" and not output_limit_hit
                else {"status": "discarded", "observations": {}, "invalid_fields": []}
            )
            recorded = self._judgment_shadow.record(key, {
                **review, "user_message_id": target[0].id,
                "assistant_message_id": target[1].id,
                "input_message_ids": [row.id for row in rows],
                "input_hash": hashlib.sha256(json.dumps(
                    [(row.id, row.role, row.content) for row in rows],
                    ensure_ascii=False, separators=(",", ":"),
                ).encode()).hexdigest(),
                "model": self._model, "call_ms": call_ms,
                "backend_type": type(self._client).__name__,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "max_output_tokens": self._max_tokens + judgment_shadow.OUTPUT_BUDGET,
                "situation_outcome": outcome, "output_limit_hit": output_limit_hit,
                "heuristic": replay, "comparisons": judgment_shadow.comparisons(review, replay),
            })
            self._stats["shadow_recorded"] += int(recorded)
        except Exception:
            self._stats["shadow_failed"] += 1
            log.debug("judgment shadow recording failed", exc_info=True)

    def _sync_interest_successor(self, key, previous, extraction, rows) -> None:
        producer = self._successor_producer
        if producer is None or producer.store() is None:
            return
        store = producer.store()
        newest_user_id = max((int(row.id) for row in rows if row.role == "user"), default=0)
        for row in producer.stock_rows():
            if (
                row.payload.get("session_id") == key
                and newest_user_id > int(row.payload.get("answer_message_id", 0))
            ):
                store.expire(row.id, evidence="new_conversation_evidence")
        successor = extraction.successor
        if (
            previous is None or previous.working_set is None or successor is None
            or extraction.working_set is not None or producer.stock() >= 1
        ):
            return
        answer_rows = [
            row for row in rows if row.role == "user"
            and int(row.id) in successor.change.evidence_message_ids
            and int(row.id) > previous.source_message_id
            and substantive_interest_answer(row.content)
            and topical(successor.change.text, row.content)[0]
        ]
        if not answer_rows or max(int(row.id) for row in answer_rows) != newest_user_id:
            return
        question_ids = previous.working_set.question.evidence_message_ids
        source_id = key + ":question:" + ",".join(map(str, sorted(question_ids)))
        if store.has_source(producer.cue_type, source_id):
            return
        cue_id = producer.publish(
            successor.subject,
            "[Interest continued]\nAnswer-linked observation: " + successor.change.text,
            payload={
                "subject": successor.subject, "source_id": source_id, "session_id": key,
                "kind": successor.kind,
                "answer_message_id": newest_user_id,
                "evidence_message_ids": list(successor.change.evidence_message_ids),
            },
        )
        self._stats["successors_queued"] += bool(cue_id)

    def _build_user_prompt(
        self,
        rows: list["MessageRow"],
        previous: ConversationSituationState | None,
    ) -> str:
        now = timephrase.now()
        tagged_rows = [
            {
                "role": row.role,
                "content": f"[message_id={int(row.id)}] {row.content}",
                "created_at": row.created_at,
            }
            for row in rows
        ]
        transcript = timephrase.format_transcript(tagged_rows, now_dt=now)
        try:
            world = self._world_snapshot_provider() or {}
        except Exception:
            world = {}
        world = self._bounded_world_snapshot(world)
        prior = previous.to_payload() if previous is not None else None
        return (
            "AUTHORITATIVE WORLD SNAPSHOT:\n"
            + json.dumps(world, ensure_ascii=False, separators=(",", ":"))
            + "\n\nPREVIOUS SITUATION:\n"
            + json.dumps(prior, ensure_ascii=False, separators=(",", ":"))
            + "\n\nRECENT TRANSCRIPT:\n"
            + transcript
        )

    @staticmethod
    def _bounded_world_snapshot(world: dict[str, Any]) -> dict[str, Any]:
        state = world.get("state") if isinstance(world, dict) else {}
        state = state if isinstance(state, dict) else {}
        location_id = state.get("location_id")
        scene_id = state.get("scene_id")
        locations = world.get("locations")
        scenes = world.get("scenes")
        current_location = next(
            (
                value
                for value in (locations if isinstance(locations, list) else [])
                if isinstance(value, dict) and value.get("id") == location_id
            ),
            None,
        )
        current_scene = next(
            (
                value
                for value in (scenes if isinstance(scenes, list) else [])
                if isinstance(value, dict) and value.get("id") == scene_id
            ),
            None,
        )
        return {
            "state": state,
            "current_location": current_location,
            "current_scene": current_scene,
        }


__all__ = ["ConversationSituationWorker"]
