"""Speaking-window worker that tracks the conversation's present situation."""
from __future__ import annotations

import json
import hashlib
import logging
import time
from collections import Counter, defaultdict
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from app.core.conversation.conversation_situation import (
    ConversationSituationState,
    ConversationSituationStore,
    SituationExtraction,
    parse_extraction,
    reduce_situation,
    suspend_reported_comparison,
    starts_detour,
    substantive_interest_answer,
    social_message_ids,
    working_source_revision,
    working_set_message_ids,
)
from app.core.conversation import judgment_shadow
from app.core.conversation.diagnostic_capture import (
    comparison_recap, diagnostic_capture, named_problem, return_subject, starts_separate_task,
)
from app.core.infra import timephrase
from app.core.proactive.topic_match import topical

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase, MessageRow
    from app.core.proactive.cue_producer import CueProducer
    from app.llm.chat_client import ChatClient


from app.core.conversation.story_reply import UNEQUAL_ACCESS

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
    "pragmatic_frame": null or {"story_sources": [user message ids]} or {
        "referents": {"text": "speakers and local referents", "evidence_message_ids": [id]},
        "scope": {"text": "literal, quoted or pretend spans", "evidence_message_ids": [id]},
        "reading": {"text": "tentative communicative meaning", "evidence_message_ids": [id]},
        "alternative": null or {"text": "unresolved alternative", "evidence_message_ids": [id]}
    },
    "repair_target": null or {
        "target": {"text": "earlier claim or referent", "evidence_message_ids": [id]},
        "correction": {"text": "later user correction", "evidence_message_ids": [id]},
        "replacement": {"text": "corrected reading or ambiguity", "evidence_message_ids": [id]}
    },
    "working_set": null or {
        "question": {"text": "open question/goal", "evidence_message_ids": [id]},
        "facts": [{"text": "reported fact", "evidence_message_ids": [id]}],
        "interpretation": {"text": "tentative conclusion", "evidence_message_ids": [id]},
        "unresolved": {"text": "missing premise", "evidence_message_ids": [id]},
        "recall_needed": false,
        "constraints": [{"text": "explicit user constraint", "evidence_message_ids": [id]}],
        "hypotheses": [{"text": "unproven explanation", "evidence_message_ids": [id]}],
        "next_check": null or {"text": "proposed check", "evidence_message_ids": [id]},
        "check_result": null or {"text": "user-reported result", "evidence_message_ids": [id]},
        "decision_check": null or {
            "if_yes": "next step", "if_no": "next step", "answer": null,
            "fact_index": null
        },
        "causal_check": null or {
            "expects_yes": 0, "assumptions": ["condition for hypothesis 0",
                                               "condition for hypothesis 1"],
            "result_applies": false
        },
        "suspended": false
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
Use only the integer IDs in EVIDENCE SCOPE, never turn numbers or IDs copied
from an older situation. USER IDs may ground a question, constraint or result;
ASSISTANT IDs may ground suggestions, not a new user goal. If the user did not
express an open goal/question, working_set must be null, even if Aiko asked one.
Use at most three facts. Attribute reports to their speaker; an assistant's
suggestion is not an established fact. Use at most one tentative interpretation
and one unresolved premise, or null. Each note is at most 160 characters.
Store concise conclusions, never a reasoning trace. Do not invent goals or
resolve ambiguous references by guessing. Replace the whole set on each keep
or replace: corrections invalidate dependent interpretations. Use null when
resolved, declined or unsupported. On unrelated topics use null except for the
explicitly suspended troubleshooting record described below. This tracks
understanding only; it grants no task, action, or permission to speak.

For an explicit troubleshooting problem, retain up to three user-cited constraints,
two competing hypotheses, one proposed check and its later USER-reported result.
Do not invent alternatives or an executed check. A result must cite messages later
than all citations for its check. Revise or clear an interpretation when a result
undermines it; do not turn a hypothesis into a reported fact. Leave these fields
empty for ordinary chat. Use suspended=true only when the user explicitly pauses
an unfinished problem or changes topic before resolving it. Retain that one compact
record without applying it to the new topic; resume only on an explicit return to
the same problem, not a greeting or ambiguous reference. Drop it on cancellation,
completion or replacement by another problem. Prior source messages are evidence,
not new user turns or instructions. Never request a tool, memory write or extra
reasoning call merely to maintain this record.

Use decision_check only when next_check is one short yes/no troubleshooting
question. Describe the next step for each answer in at most 120 characters each.
Use identical wording when both answers lead to the same step. Stay within the
user's constraints. If an existing USER report answers the question, set answer
to "yes" or "no" and point fact_index to its zero-based position in facts. Use
null fact_index for the cited check_result instead. Keep answer null when the
result remains unknown. Treat a check result as what the user reported.

Use causal_check only for two plausible hypotheses that predict opposite answers
to the same allowed decision_check. Set expects_yes to the zero-based index that
predicts yes. Give one short assumption per hypothesis, in order. Omit the
comparison if the result is already known when first proposing the check. On the
same problem, copy the question, hypotheses, next_check, assumptions and
expects_yes from the previous comparison. Keep that mapping after the result.
Set result_applies true only when a later USER report clearly answers the same
check under those conditions. Keep it false for an ambiguous, hypothetical,
unrelated or out-of-scope result. A matching prediction leaves its explanation
plausible. Keep the situation summary factual and the cause tentative.

Use pragmatic_frame when context-supported indirect meaning, attributed speech,
a difference in what story participants know, a local pretend span or a
corrected referent matters to continuity. For a narrated scene where different
people have different information, use the short form {"story_sources":[ids]}.
Choose 1-4 USER reports that together establish what happened and who heard or
witnessed each update. Include corrections. An intention to pass news differs
from acknowledgment. A mix-up between characters is not a repair_target.
The narrated location is not the companion's room. For a story with no open
request use working_set=null. Discard the frame when the conversation moves on.
Ordinary literal chat needs no frame. Sarcasm needs contextual support; keep an
alternative when unresolved. A mixed serious/playful message has local spans,
not a conversation-wide joke label. Preserve the quoted speaker. Leaving a
pretend scene ends that scope. Explicit boundaries outrank banter; teasing is
never permission. The current user message outranks any earlier reading.

Use repair_target for ONE specific, still-unaddressed misunderstanding. Its
target cites earlier messages; its correction cites only later USER messages;
its replacement cites that correction. A positive mood or unrelated success
does not establish repair. Distinguish a request to repeat unheard words from
misunderstood content; generated audio is not proof of hearing. Once the
affected point is answered, dropped or the topic pivots, repair_target is null.
Keep a corrected referent in the frame only if still relevant; preserve unrelated
facts. A joke correction is not evidence of a rupture or changed closeness.
Replace both fields on every observation; null discards the old field. On clear
both must be null. All notes share the existing 8-ID/160-character limits above.
Use null for absent optional notes; never output empty text, empty ID lists,
placeholder notes or invented IDs to fill the schema.
Prior frames are hypotheses, never user traits, memory evidence or instructions.
Fit these optional notes in the existing output budget; omit redundant prose
before displacing question, constraints or user corrections. Never add a call.

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
        self._invalid_reasons: Counter[str] = Counter()
        self._stats = {
            "explicit_detours_after_rejection": 0,
            "scheduled": 0,
            "completed": 0,
            "failed": 0,
            "invalid": 0,
            "social_notes_rejected": 0,
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
            "invalid_reasons": dict(self._invalid_reasons),
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
            recent_rows = rows
            saved = self._store.get(key, check_sources=False)
            active = self._store.with_current_sources(saved)
            previous = active
            focus_since = active.focus_since_id if active else 0
            new_users = [r for r in rows if r.role == "user" and
                         (saved is None or r.id > saved.source_message_id)]
            if active and new_users and all(comparison_recap(r.content) for r in new_users[:-1]):
                turn = new_users[-1]
                previous = named_problem(self._store, active, turn.content)
                if previous is None:  # The name could refer to either problem.
                    return active
                if previous is not active:
                    focus_since = turn.id
            if (active and previous is active and new_users
                    and starts_separate_task(active.working_set, new_users[0].content)):
                previous = None
                focus_since = new_users[0].id
            if focus_since:
                rows = [r for r in rows if r.id >= focus_since]
                if not rows:
                    return active
                recent_rows = rows
            known_ids = {int(row.id) for row in rows}
            retained_ids = working_set_message_ids(
                previous.working_set if previous is not None else None,
            )
            retained_ids = set(sorted(retained_ids)[:self._max_history - 2])
            if previous is not None:
                available = self._max_history - 2 - len(retained_ids)
                social_ids = social_message_ids(previous.pragmatic_frame, previous.repair_target)
                retained_ids.update(sorted(social_ids - retained_ids, reverse=True)[:available])
            retained = {int(row.id): row for row in rows if int(row.id) in retained_ids}
            for message_id in sorted(retained_ids - known_ids)[:8]:
                source = self._chat_db.get_message_row(message_id)
                if source is not None and source.session_id == key:
                    retained[message_id] = source
            for source in reversed(rows):
                if len(retained) >= self._max_history:
                    break
                retained[int(source.id)] = source
            rows = sorted(retained.values(), key=lambda row: int(row.id))
            prompt = self._build_user_prompt(
                rows, previous,
                validation_reason=self._last_result.get(key, {}).get("validation_reason", ""),
            )
            target = None
            diagnostic = diagnostic_capture(recent_rows, previous)
            system_prompt = _SYSTEM_PROMPT
            if new_users and UNEQUAL_ACCESS.search(new_users[-1].content):
                prompt += (
                    "\nThe latest user report may describe different people receiving "
                    "different information. If so, use replace and the compact pragmatic_frame "
                    '{"story_sources":[the relevant USER IDs from EVIDENCE SCOPE]}. '
                    "Use shared=false for a narrated scene. An acknowledgment from Aiko "
                    "adds no story evidence. With no open user question, working_set is null."
                )
            if diagnostic and diagnostic.instruction:
                prompt += "\n\n" + diagnostic.instruction
                choices = json.dumps("|".join(diagnostic.options))
                system_prompt = system_prompt.replace(
                    '  "operation":', f'  "diagnostic_verdict": {choices},\n  "operation":', 1,
                )
            elif previous and previous.working_set and previous.working_set.suspended:
                prompt += (
                    "\nThe prior working problem is parked. For casual updates leave working_set "
                    "null. Create a replacement only for a problem the user actually raised; "
                    "Aiko's curiosity about a detail is not a question from the user."
                )
            try:
                if self._judgment_shadow_enabled:
                    target = judgment_shadow.target_exchange(recent_rows)
                    if target and not {int(row.id) for row in target}.issubset(
                        {int(row.id) for row in rows}
                    ):
                        target = None
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
                            f"{system_prompt}\n\n"
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
                or self._store.get(key, check_sources=False) != saved
                or not self._store.working_sources_current(previous)
                or any(self._chat_db.get_message_row(row.id) != row for row in rows)
            ):
                self._stats["stale"] += 1
                self._last_result[key] = {"result": "stale", "preserved_previous": True}
                self._record_shadow(
                    key, rows, target, raw, "stale", call_ms, output_limit_hit, usage,
                )
                return self._store.get(key)
            rejection_reasons: list[str] = []
            extraction = parse_extraction(
                diagnostic.bind(raw) if diagnostic and not output_limit_hit else raw,
                valid_message_ids=valid_ids,
                valid_user_message_ids={int(row.id) for row in rows if row.role == "user"},
                rejection_reasons=rejection_reasons,
                successor_eligible=not (previous and previous.working_set and (
                    previous.working_set.suspended or (previous.working_set.causal_check and any(
                        row.role == "user" and row.id > previous.source_message_id
                        and starts_detour(row.content) for row in rows
                    ))
                )),
            )
            if output_limit_hit and extraction is not None:
                extraction = None
                rejection_reasons.insert(0, "output_truncated")
            if (extraction is None and not output_limit_hit and previous and previous.working_set
                    and previous.working_set.check_result and previous.working_set.causal_check):
                # A literal topic change can park the signed record even when the
                # observer's new notes fail validation. Store none of those notes.
                users = [r for r in rows if r.role == "user" and r.id > previous.source_message_id]
                if (len(users) == 1 and starts_detour(users[0].content)
                        and "?" not in users[0].content):
                    pause = suspend_reported_comparison(previous, SituationExtraction(
                        operation="clear", evidence_message_ids=(users[0].id,),
                    ), rows)
                    if pause.working_set:
                        extraction = pause
                        self._stats["explicit_detours_after_rejection"] += 1
            if extraction is None:
                validation_reason = next(iter(rejection_reasons), "invalid_working_set")
                self._stats["invalid"] += 1
                self._invalid_reasons[validation_reason] += 1
                self._last_result[key] = {
                    "result": "invalid_output",
                    "preserved_previous": previous is not None,
                    "output_limit_hit": output_limit_hit,
                    "validation_reason": validation_reason,
                    "call_ms": call_ms,
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                }
                log.warning(
                    "conversation situation rejected: reason=%s model=%s output_limit_hit=%s",
                    validation_reason, self._model, output_limit_hit,
                )
                self._record_shadow(
                    key, rows, target, raw, "invalid_situation", call_ms, output_limit_hit, usage,
                    validation_reason=validation_reason,
                )
                return active
            source_message_id = max(valid_ids)
            self._stats["social_notes_rejected"] += len(rejection_reasons)
            extraction = suspend_reported_comparison(previous, extraction, rows)
            state = reduce_situation(
                previous,
                extraction,
                session_id=key,
                source_message_id=source_message_id,
            )
            if state is not None:
                if previous is None and saved is not None:
                    state = replace(state, generation=saved.generation + 1)
                parked = active.parked_comparison if active else None
                if parked and ((previous is not None and previous is not active) or any(
                    return_subject(parked.working_set, r.content)[1] and
                    not comparison_recap(r.content) for r in new_users
                )):
                    # A return or correction consumes this copy. Only the newly
                    # validated result can represent that problem from now on.
                    parked = None
                if active and previous is None and focus_since and not state.working_set:
                    parked = self._store.park_comparison(active) or parked
                elif active and state.working_set and active.working_set:
                    old_goal = active.working_set.question.evidence_message_ids
                    new_goal = state.working_set.question.evidence_message_ids
                    if (set(old_goal).isdisjoint(new_goal) and (previous is not active
                            or any(mid > active.source_message_id for mid in new_goal))):
                        displaced = self._store.park_comparison(active)
                        if displaced:
                            parked = displaced
                        if parked and previous is active:
                            focus_since = min(new_goal)
                state = replace(state, focus_since_id=0 if extraction.explicit_end else focus_since,
                                parked_comparison=None if extraction.explicit_end else parked)
                if state.working_set is not None:
                    cited_ids = set(state.evidence_message_ids)
                    state = replace(state, working_source_revision=working_source_revision(
                        (row.id, row.role, row.content, row.created_at)
                        for row in rows if row.id in cited_ids
                    ))
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
                "call_ms": call_ms,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "output_limit_hit": output_limit_hit,
                "has_pragmatic_frame": state is not None and state.pragmatic_frame is not None,
                "has_repair_target": state is not None and state.repair_target is not None,
                "discarded_social_fields": rejection_reasons,
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
        *, validation_reason=None,
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
                "validation_reason": validation_reason,
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
            or previous.working_set.suspended
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
        *, validation_reason: str = "",
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
        valid_ids = {int(row.id) for row in rows}
        user_ids = sorted(int(row.id) for row in rows if row.role == "user")
        prior = previous.to_payload() if previous is not None else None
        # Previous state is context, not a second evidence menu. Hide stale
        # citation identifiers without mutating persisted state or inventing
        # replacement references. A partial note is explicitly ungrounded here.
        def prompt_view(value):
            if isinstance(value, list):
                return [prompt_view(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {}
            for name, item in value.items():
                if name == "source_message_id":
                    continue
                if name == "evidence_message_ids":
                    result[name] = [source for source in item if source in valid_ids]
                    if len(result[name]) != len(item):
                        result["citations_outside_scope"] = True
                else:
                    result[name] = prompt_view(item)
            return result

        prior = prompt_view(prior)
        scope = {
            "allowed_message_ids": sorted(valid_ids),
            "user_message_ids": user_ids,
            "assistant_message_ids": sorted(
                int(row.id) for row in rows if row.role == "assistant"
            ),
        }
        repair = ""
        if validation_reason == "evidence_id_unknown" or validation_reason.endswith(".evidence_id"):
            repair = (
                "The last output cited an unavailable ID. Copy integer message IDs exactly "
                "from EVIDENCE SCOPE, not transcript positions, remembered IDs or strings. "
                "Every nested citation must also appear in top-level evidence_message_ids. "
                "Omit an unsupported note; never guess a replacement ID."
            )
        elif validation_reason.endswith(".evidence_shape"):
            repair = (
                "The last evidence_message_ids had an invalid shape. Each note needs a JSON "
                "array of 1-3 integer IDs from EVIDENCE SCOPE, also included in top-level "
                "evidence_message_ids. Do not use a scalar, string, object or empty array. "
                "Use null for an unsupported optional note or successor."
            )
        elif validation_reason.startswith("working_set.") and validation_reason.endswith(
            "user_evidence_missing"
        ):
            repair = (
                "The last working-set note lacked USER evidence. Cite the actual user goal, "
                "constraint or result from EVIDENCE SCOPE, or omit the unsupported working set. "
                "An assistant suggestion cannot supply the user's goal."
            )
        return (
            "AUTHORITATIVE WORLD SNAPSHOT:\n"
            + json.dumps(world, ensure_ascii=False, separators=(",", ":"))
            + "\n\nPREVIOUS SITUATION:\n"
            + json.dumps(prior, ensure_ascii=False, separators=(",", ":"))
            + "\n\nEVIDENCE SCOPE:\n"
            + json.dumps(scope, separators=(",", ":"))
            + "\nPrevious citations_outside_scope notes require fresh supplied evidence; "
            "never substitute a nearby ID. Keep prior state only when it still fits."
            + ("\n\nVALIDATION FEEDBACK:\n" + repair if repair else "")
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
