"""Typed facade for present conversation/world situation awareness."""
from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Any

from app.core.conversation.conversation_situation import (
    ACTIVE,
    ConversationSituationSnapshot,
    ConversationSituationState,
    ConversationWorkingSet,
    OTHER_COMPARISON_HEADER,
    RETURNED_COMPARISON_HEADER,
    WorldSituation,
    decision_action,
    decision_answer_source,
    declines_interest,
    starts_detour,
    state_age_seconds,
)
from app.core.infra import timephrase


_WORD_RE = re.compile(r"[a-z0-9]+")
_PLACE_NOISE = frozenset(
    {"a", "an", "at", "by", "in", "near", "on", "our", "the", "their", "your"}
)
_ACTIVITY_NOISE = frozenset(
    {"a", "an", "and", "are", "is", "the", "to", "together", "user", "with"}
)
_GOAL_NOISE = frozenset(
    {"which", "what", "that", "this", "with", "have", "does", "would", "should"}
)


def _tokens(value: str, noise: frozenset[str]) -> set[str]:
    return {token for token in _WORD_RE.findall((value or "").lower()) if token not in noise}


def _text_matches(reference: str, candidates: tuple[str, ...], *, activity: bool) -> bool:
    noise = _ACTIVITY_NOISE if activity else _PLACE_NOISE
    reference_tokens = _tokens(reference, noise)
    if not reference_tokens:
        return False
    candidate_tokens: set[str] = set()
    for candidate in candidates:
        candidate_tokens.update(_tokens(candidate, noise))
    return bool(reference_tokens & candidate_tokens)


def _working_goal_matches(user_text: str, question: str) -> bool:
    terms = set(re.findall(r"[a-z]{4,}", question.casefold())) - _GOAL_NOISE
    return bool(terms.intersection(re.findall(r"[a-z]{4,}", user_text.casefold())))


_RETURN_TO = re.compile(r"^(?:back to|returning to|about) (?:the |my )?([\w -]+?)(?:[.,:?!—]|$)",
                        re.I)


def _return_subject(notes, user_text):
    match = _RETURN_TO.match(user_text.strip())
    if not match:
        return "", False
    subject = match[1].strip().casefold()
    if (len(subject.split()) < 2 or subject in {"same issue", "same problem", "earlier issue"}
            or re.search(r"\b(?:forget|drop|stop discussing)\b", user_text, re.I)):
        return "", False
    return subject, bool(re.search(rf"\b{re.escape(subject)}\b", notes.question.text, re.I))


def _resumed_comparison(notes: ConversationWorkingSet, user_text: str) -> str:
    """On the first returning turn, recover only the named problem and its evidence."""
    subject, matches = _return_subject(notes, user_text)
    if (not subject or notes.causal_check is None or notes.check_result is None
            or notes.decision_check is None or notes.next_check is None):
        return ""
    if not matches:
        if ("evidence" in user_text.casefold()
                and _working_goal_matches(subject, notes.question.text)):
            return (f"{OTHER_COMPARISON_HEADER}\n"
                    "The earlier check concerns a different problem. "
                    "This comparison supplies no test result for the subject just named. "
                    "Answer from the current subject's own reports only.")
        return ""
    check = notes.causal_check
    source = ", ".join(str(mid) for mid in notes.check_result.evidence_message_ids)
    lines = [RETURNED_COMPARISON_HEADER, f"The user identified the earlier problem: {subject}.",
             f"Earlier question: {notes.question.text}",
             f"Check already performed: {notes.next_check.text}",
             f"Earlier user report [messages {source}]: {notes.check_result.text}"]
    for i, hypothesis in enumerate(notes.hypotheses):
        answer = "yes" if i == check.expects_yes else "no"
        lines.append(f"{hypothesis.text} predicted {answer} assuming {check.assumptions[i]}.")
    correction = re.search(r"\b(?:correction|actually|wrong (?:file|test|input))\b",
                           user_text, re.I)
    if correction:
        lines.append("The user is revising the earlier observation. Base the comparison on the "
                     "new report. A single test shifts which cause is more plausible.")
    elif check.result_applies:
        supported = (check.expects_yes if notes.decision_check.answer == "yes"
                     else 1 - check.expects_yes)
        lines.append(f"Earlier assessment: {notes.hypotheses[supported].text} fits the recorded "
                     f"result better than {notes.hypotheses[1 - supported].text}.")
    else:
        lines.append("That result was inconclusive between the two explanations.")
    lines.append("Answer directly in your own words. Connect the user's observation to the "
                 "explanation it favors, or explain why the observation was inconclusive. "
                 "The user has already answered the test question. Either cause remains possible.")
    return "\n".join(lines)


class ConversationSituationMixin:
    """Assemble one authoritative snapshot for prompts, debug, and guards."""

    def _comparison_current_on_return(self, state, text):
        if not _RETURN_TO.match(text.strip()):
            return False
        rows = self._chat_db.get_messages_after(
            state.session_id, after_id=state.source_message_id, limit=3,
        )
        return not rows or (len(rows) == 1 and rows[0].role == "user"
                            and rows[0].content.strip() == text.strip())

    def reviewed_comparison_reply(self, user_text: str) -> str:
        """Answer a standalone recap of an already assessed check from its signed sources."""
        from app.core.conversation.exchange_recall import _safe_quote

        text = user_text.strip()
        opening = _RETURN_TO.match(text)
        if (not opening or not re.fullmatch(
                r"which (?:cause|explanation|hypothesis) (?:fits|fit)(?: the (?:test|result))? "
                r"(?:best|better)\??", text[opening.end():].strip(), re.I)
                or not getattr(self._settings.agent, "conversation_situation_enabled", True)):
            return ""
        snapshot = self.conversation_situation_snapshot(user_text=text)
        state = snapshot.inferred
        notes = state.working_set if state else None
        if (not notes or not notes.suspended or not state.working_source_revision
                or not notes.check_result or not notes.causal_check or not notes.decision_check
                or not _return_subject(notes, text)[1]
                or snapshot.inferred_stale or not self._comparison_current_on_return(state, text)):
            return ""
        result = notes.check_result.text
        if not all(_safe_quote(s) for s in (result, *(h.text for h in notes.hypotheses))):
            return ""
        ids = notes.check_result.evidence_message_ids
        row = self._chat_db.get_message_row(ids[0]) if len(ids) == 1 else None
        if (row and row.role == "user" and row.session_id == state.session_id
                and row.content.strip() == result):
            report = f"You reported, “{result}”"
        else:
            report = f"The recorded observation was “{result}”."
        if not notes.causal_check.result_applies:
            return f"{report} That leaves both explanations open."
        yes = notes.causal_check.expects_yes
        favored = yes if notes.decision_check.answer == "yes" else 1 - yes
        label = notes.hypotheses[favored].text.rstrip(".!?")
        return f"{report} {label} fits that result better. The cause is still tentative."

    def conversation_situation_snapshot(
        self,
        *,
        user_text: str = "",
        input_mode: str | None = None,
        fresh: bool = False,
    ) -> ConversationSituationSnapshot:
        session_id = self.session_key
        mode_raw = str(input_mode or getattr(self, "_last_turn_mode", "typed") or "typed")
        normalized_mode = "typed" if mode_raw == "typed" else "voice"
        cache_key = (
            int(
                getattr(
                    getattr(self, "_prompt_assembler", None),
                    "_assembly_seq",
                    -1,
                )
            ),
            session_id,
            normalized_mode,
        )
        dialogue_act = ""
        if user_text:
            try:
                from app.core.conversation.dialogue_act_tagger import tag_regex

                dialogue_act = tag_regex(user_text).act
            except Exception:
                dialogue_act = ""
        cached = getattr(self, "_conversation_situation_snapshot_cache", None)
        if not fresh and cached is not None and cached[0] == cache_key:
            snapshot = cached[1]
            if dialogue_act and snapshot.dialogue_act != dialogue_act:
                snapshot = replace(snapshot, dialogue_act=dialogue_act)
                self._conversation_situation_snapshot_cache = (cache_key, snapshot)
            return snapshot

        state = self._read_conversation_situation(session_id)
        world = self._conversation_world_situation()
        world_compatible, conflict_reason = self._situation_world_compatibility(
            state, world
        )
        age = state_age_seconds(state)
        stale_seconds = max(
            300.0,
            float(
                getattr(
                    self._settings.agent,
                    "conversation_situation_stale_seconds",
                    21600.0,
                )
            ),
        )
        inferred_stale = age is None or age > stale_seconds
        shared_commitment_active = bool(
            state is not None
            and state.status == ACTIVE
            and state.shared
            and not inferred_stale
            and world_compatible
        )

        arc = ""
        arc_confidence = 0.0
        arc_store = getattr(self, "_arc_store", None)
        if arc_store is not None:
            try:
                arc_state = arc_store.get(self._user_id)
                if arc_state is not None:
                    arc = str(arc_state.arc)
                    arc_confidence = float(arc_state.confidence)
            except Exception:
                pass

        mood_label = ""
        try:
            affect = self._affect_store.get(self._user_id)
            mood_label = str(getattr(affect, "mood_label", "") or "")
        except Exception:
            pass
        vitality_band = ""
        try:
            vitality_band = str(self.vitality_snapshot().get("band") or "")
        except Exception:
            pass

        last_activity = float(getattr(self, "_last_user_activity_at", 0.0) or 0.0)
        elapsed_ms = (
            max(0, int((time.monotonic() - last_activity) * 1000.0))
            if last_activity > 0
            else 0
        )
        sleep: dict[str, Any] = {"status": "awake"}
        sleep_store = getattr(self, "_sleep_store", None)
        if sleep_store is not None:
            try:
                raw_sleep = sleep_store.snapshot()
                sleep = {
                    key: raw_sleep.get(key)
                    for key in (
                        "status",
                        "generation",
                        "entered_at",
                        "duration_seconds",
                        "current_episode_id",
                        "sleep_kind",
                        "reason_code",
                        "reason_text",
                        "interruption_count",
                        "previous_world",
                    )
                }
            except Exception:
                pass
        snapshot = ConversationSituationSnapshot(
            session_id=session_id,
            generation=int(state.generation if state is not None else 0),
            observed_at=timephrase.utcnow().isoformat(timespec="seconds"),
            input_mode=normalized_mode,
            floor_transition=(
                "user_to_aiko"
                if bool(getattr(self, "_turn_in_progress", False))
                else "neither"
            ),
            dialogue_act=dialogue_act,
            arc=arc,
            arc_confidence=arc_confidence,
            mood_label=mood_label,
            vitality_band=vitality_band,
            user_present=int(getattr(self, "_connected_clients", 0) or 0) > 0,
            user_active_app=(
                str(getattr(self, "_user_active_app", "") or "")
                if bool(
                    getattr(self._settings.agent, "activity_awareness_enabled", False)
                )
                else ""
            ),
            world=world,
            inferred=state,
            inferred_stale=inferred_stale,
            world_compatible=world_compatible,
            conflict_reason=conflict_reason,
            shared_commitment_active=shared_commitment_active,
            since_user_activity_ms=elapsed_ms,
            sleep=sleep,
        )
        overlay = getattr(self, "_live_situation_overlay", None)
        if overlay is not None:
            snapshot = replace(
                snapshot,
                floor_owner=str(getattr(overlay, "floor_owner", snapshot.floor_owner)),
                typing_active=bool(getattr(overlay, "typing_active", False)),
                attention_target=str(
                    getattr(overlay, "attention_target", snapshot.attention_target)
                ),
                attention_mode=str(
                    getattr(overlay, "attention_mode", snapshot.attention_mode)
                ),
                live_frame_generation=int(
                    getattr(overlay, "live_frame_generation", 0) or 0
                ),
            )
        self._conversation_situation_snapshot_cache = (cache_key, snapshot)
        return snapshot

    def _read_conversation_situation(
        self, session_id: str
    ) -> ConversationSituationState | None:
        store = getattr(self, "_conversation_situation_store", None)
        if store is None:
            return None
        try:
            return store.get(session_id)
        except Exception:
            return None

    def _conversation_world_situation(self) -> WorldSituation:
        store = getattr(self, "_world_store", None)
        if store is None:
            return WorldSituation()
        try:
            state = store.get_state()
            location = (
                store.get_location_by_id(state.location_id)
                if state.location_id is not None
                else None
            )
            scene = store.current_scene()
            return WorldSituation(
                scene_id=state.scene_id,
                scene_name=str(getattr(scene, "name", "") or ""),
                location_id=state.location_id,
                location_slug=str(getattr(location, "slug", "") or ""),
                location_name=str(getattr(location, "name", "") or ""),
                posture=str(state.posture or ""),
                activity=str(state.activity or ""),
                updated_at=str(state.updated_at or ""),
            )
        except Exception:
            return WorldSituation()

    @staticmethod
    def _situation_world_compatibility(
        state: ConversationSituationState | None,
        world: WorldSituation,
    ) -> tuple[bool, str]:
        if state is None or state.status != ACTIVE:
            return False, "no_active_situation"
        anchored = False
        if state.place_ref:
            if not _text_matches(
                state.place_ref,
                (world.location_slug, world.location_name, world.scene_name),
                activity=False,
            ):
                return False, "inferred_context_conflicts_with_world"
            anchored = True
        if state.aiko_activity and not _text_matches(
            state.aiko_activity,
            (world.activity, world.posture),
            activity=True,
        ):
            # ``idle`` / ``relaxing`` are world baselines, not proof that
            # Aiko is not also talking or watching something with the user.
            # A non-neutral activity is affirmative contradictory evidence.
            if (world.activity or "").strip().lower() not in {
                "",
                "idle",
                "relaxing",
            }:
                return False, "inferred_context_conflicts_with_world"
        elif state.aiko_activity:
            anchored = True
        if not anchored:
            return False, "no_world_anchor"
        return True, ""

    def recall_information_need(self, user_text: str) -> dict:
        snapshot = self.conversation_situation_snapshot(user_text=user_text)
        state = snapshot.inferred
        if state is None or snapshot.inferred_stale or state.status != ACTIVE:
            return {}
        if not snapshot.world_compatible and not (
            not state.shared and snapshot.conflict_reason == "no_world_anchor"
        ):
            return {}
        notes = state.working_set
        if notes is None or notes.suspended or not notes.recall_needed or notes.unresolved is None:
            return {}
        action = decision_action(notes)
        if action not in (None, "recall"):
            return {}
        if declines_interest(user_text):
            return {}
        if "?" not in user_text and len(user_text.split()) > 6:
            return {}
        if not self._working_goal_relevant(notes, user_text, allow_follow_up=action == "recall"):
            return {}
        if any(note.text.casefold() == notes.unresolved.text.casefold() for note in notes.facts):
            return {}
        return {
            "session_id": state.session_id, "generation": state.generation,
            "evidence_message_ids": list(notes.unresolved.evidence_message_ids),
            "goal_message_ids": list(notes.question.evidence_message_ids),
            "family": "recall",
        }

    def _working_goal_relevant(
        self, notes: ConversationWorkingSet, user_text: str, *, allow_follow_up: bool,
    ) -> bool:
        from app.core.memory.learned_strategies import fresh_goal_context

        if allow_follow_up and re.search(
            r"\b(?:new task|different (?:task|issue|topic))\b", user_text, re.I,
        ):
            return False
        return (_working_goal_matches(user_text, notes.question.text)
                or (allow_follow_up and bool(fresh_goal_context(self, user_text))))

    def _render_working_decision(self, notes: ConversationWorkingSet, user_text: str) -> str:
        check = notes.decision_check
        if (check is None or notes.next_check is None or declines_interest(user_text)
                or not self._working_goal_relevant(notes, user_text, allow_follow_up=True)):
            return ""
        action = decision_action(notes)
        if action == "use_result":
            source = decision_answer_source(notes)
            if source is None:
                return "Decision: use the recorded check result and keep this check closed."
            ids = ",".join(str(mid) for mid in source.evidence_message_ids)
            step = check.if_yes if check.answer == "yes" else check.if_no
            return f"Decision: use the recorded answer [messages {ids}]. Next step: {step}"
        if action == "proceed":
            return f"Decision: both answers lead to the same step. Proceed with: {check.if_yes}"

        paths = f"If yes: {check.if_yes} If no: {check.if_no}"
        if action == "recall":
            if self.recall_information_need(user_text):
                return (
                    "Decision: use a supplied answer when present. Otherwise, look it up once. "
                    f"Check: {notes.next_check.text} "
                    f"If recall leaves it open, give conditional guidance. {paths}"
                )
            return f"Decision: use available evidence or keep both paths open. {paths}"
        balance = getattr(self, "_question_balance_suppressed", None)
        if not (callable(balance) and balance()) and not self._working_check_already_asked(
            notes.next_check.text,
        ):
            return (
                "Decision: use any answer in the latest message or supplied evidence. If still "
                "needed, ask only "
                f"this question, once: {notes.next_check.text} {paths}"
            )
        return f"Decision: use any answer in the latest message or keep both paths open. {paths}"

    def _working_check_already_asked(self, question: str) -> bool:
        db = getattr(self, "_chat_db", None)
        if db is None:
            return False
        asked = " ".join(question.casefold().split())
        try:
            return any(row.role == "assistant" and asked in " ".join(row.content.casefold().split())
                       for row in db.get_messages(self.session_key, limit=8))
        except Exception:
            return True

    def _render_conversation_situation_block(self, user_text: str) -> str:
        from app.core.conversation.social_understanding import current_repair_block

        if not getattr(self._settings.agent, "conversation_situation_enabled", True):
            return ""
        immediate = ""
        if (getattr(self._settings.agent, "clarification_repair_enabled", True)
                and getattr(self, "_conversation_situation_store", None) is not None):
            immediate = current_repair_block(
                user_text, getattr(self, "_chat_db", None), self.session_key,
            )
        prior = self._render_prior_conversation_situation_block(user_text)
        return "\n\n".join(part for part in (prior, immediate) if part)

    def _render_prior_conversation_situation_block(self, user_text: str) -> str:
        from app.core.conversation.social_understanding import social_context_block

        snapshot = self.conversation_situation_snapshot(user_text=user_text)
        state = snapshot.inferred
        if state is None or snapshot.inferred_stale:
            return ""
        notes = state.working_set
        if notes is not None and notes.causal_check is not None and starts_detour(user_text):
            return ""
        if notes is not None and notes.suspended:
            if (not state.working_source_revision or notes.causal_check is None
                    or not self._comparison_current_on_return(state, user_text)):
                return ""
            return _resumed_comparison(notes, user_text)
        if state.status == "ended":
            return ""
        if not snapshot.world_compatible and not (
            (state.working_set is not None or state.pragmatic_frame is not None
             or state.repair_target is not None) and not state.shared
            and snapshot.conflict_reason == "no_world_anchor"
        ):
            return (
                "[Conversation situation]\n"
                f"A prior conversational reading was: {state.summary} "
                "It conflicts with the authoritative room state; do not present "
                "that reading as current."
            )
        shared = (
            f" Shared activity: {state.shared_activity}."
            if state.shared and state.shared_activity
            else ""
        )
        aiko_now = (
            f" In this context you are {state.aiko_activity}."
            if state.aiko_activity
            else ""
        )
        working = ""
        if (
            state.working_set is not None and not state.working_set.suspended
            and state.status == ACTIVE
        ):
            notes = state.working_set

            def render_note(label, note):
                ids = ",".join(str(message_id) for message_id in note.evidence_message_ids)
                return f"{label} [messages {ids}]: {note.text}"

            lines = [render_note("Open question", notes.question)]
            lines.extend(render_note("User constraint", note) for note in notes.constraints)
            lines.extend(render_note("Reported fact", note) for note in notes.facts)
            assessed = False
            comparison = notes.causal_check
            if (comparison is not None and notes.decision_check is not None
                    and notes.next_check is not None
                    and not declines_interest(user_text)
                    and self._working_goal_relevant(notes, user_text, allow_follow_up=True)):
                answer = notes.decision_check.answer if comparison.result_applies else None
                assessed = answer is not None
                lines.append(render_note("Check being compared", notes.next_check))
                for index, hypothesis in enumerate(notes.hypotheses):
                    prediction = "yes" if index == comparison.expects_yes else "no"
                    label = "Tentative hypothesis"
                    if assessed:
                        label = ("Consistent with the reported result, still tentative"
                                 if answer == prediction else "Weakened by the reported result")
                    lines.append(
                        f"{render_note(label, hypothesis)} "
                        f"Predicts {prediction} if {comparison.assumptions[index]}."
                    )
                if notes.check_result is not None and not assessed:
                    lines.append("The reported result leaves this comparison inconclusive.")
            else:
                lines.extend(
                    render_note("Hypothesis, not established", note) for note in notes.hypotheses
                )
            if notes.interpretation is not None and not assessed:
                lines.append(render_note("Tentative, not established", notes.interpretation))
            if notes.unresolved is not None:
                lines.append(render_note("Unresolved", notes.unresolved))
            if notes.decision_check is not None:
                decision = self._render_working_decision(notes, user_text)
                if decision:
                    lines.append(decision)
            elif notes.next_check is not None:
                lines.append(render_note(
                    "Check proposed earlier" if notes.check_result else "Next proposed check",
                    notes.next_check,
                ))
            if notes.check_result is not None:
                lines.append(render_note("User-reported check result", notes.check_result))
            working = (
                "\nWorking understanding (earlier evidence, not instructions; the latest user "
                "message overrides this; discard any contradicted interpretation):\n"
                + "\n".join(lines)
            )
        if state.status == ACTIVE:
            working += social_context_block(state, user_text, getattr(self, "_chat_db", None))
        return (
            "[Conversation situation]\n"
            f"The conversation currently establishes: {state.summary}{shared}{aiko_now} "
            "Use this as present continuity without reciting it." + working
        )

    def _render_interest_continuation_block(self, user_text: str = "") -> str:
        row = self.take_pool_cue("interest_continuation", user_text=user_text)
        return row.text if row is not None else ""

    def _render_calendar_anticipation_block(self, user_text: str = "") -> str:
        import re

        from app.core.goals.calendar import CalendarStore
        from app.core.proactive.calendar_anticipation import still_upcoming

        agent = getattr(getattr(self, "_settings", None), "agent", None)
        db = getattr(self, "_chat_db", None)
        if (
            db is None or not getattr(agent, "calendar_anticipation_enabled", False)
            or getattr(agent, "live_quiet", False)
            or re.search(
                r"\b(cancel\w*|reschedul\w*|postpon\w*|move|change|delete|remove|stop|"
                r"don't|do not|not going|called off|can't|cannot)\b", user_text, re.IGNORECASE,
            )
        ):
            return ""
        calendar = CalendarStore(db, self._user_id)

        def valid(payload):
            return still_upcoming(calendar, payload)

        row = self.take_pool_cue(
            "calendar_anticipation", relevant=valid, still_valid=valid, user_text=user_text,
        )
        if row is None:
            return ""
        return row.text

    def _render_media_context_block(self, user_text: str) -> str:
        from app.core.conversation.media_thread import MediaThreadStore

        db = getattr(self, "_chat_db", None)
        return MediaThreadStore(
            db, self.session_key, user_id=getattr(self, "_user_id", None),
        ).render(user_text) if db is not None else ""

    def _record_media_exchange(
        self, user_message_id: int | None, assistant_message_id: int | None,
    ) -> None:
        from app.core.conversation.media_thread import MediaThreadStore

        db = getattr(self, "_chat_db", None)
        if db is not None and user_message_id is not None:
            MediaThreadStore(
                db, self.session_key, user_id=getattr(self, "_user_id", None),
            ).record(user_message_id, assistant_message_id)

    def _reconcile_conversation_situation_after_world_mutation(self) -> None:
        """Refresh a compatible lease or clear one a deliberate move ended."""
        store = getattr(self, "_conversation_situation_store", None)
        if store is None:
            return
        state = self._read_conversation_situation(self.session_key)
        if state is None or state.status != ACTIVE or not state.shared:
            return
        self._conversation_situation_snapshot_cache = None
        snapshot = self.conversation_situation_snapshot(fresh=True)
        if snapshot.world_compatible:
            store.upsert(
                replace(
                    state,
                    updated_at=timephrase.utcnow().isoformat(timespec="seconds"),
                    miss_count=0,
                )
            )
        else:
            store.clear(self.session_key)
        self._conversation_situation_snapshot_cache = None

    def _renew_conversation_situation_on_turn(
        self, user_message_id: int | None
    ) -> None:
        """Renew an active compatible lease without changing its evidence."""
        if user_message_id is None:
            return
        store = getattr(self, "_conversation_situation_store", None)
        state = self._read_conversation_situation(self.session_key)
        if store is None or state is None or state.status != ACTIVE:
            return
        snapshot = self.conversation_situation_snapshot(fresh=True)
        if not snapshot.world_compatible:
            return
        store.upsert(
            replace(
                state,
                source_message_id=max(state.source_message_id, int(user_message_id)),
                updated_at=timephrase.utcnow().isoformat(timespec="seconds"),
            )
        )
        self._conversation_situation_snapshot_cache = None

    def conversation_situation_diagnostics(self) -> dict[str, Any]:
        snapshot = self.conversation_situation_snapshot(fresh=True)
        worker = getattr(self, "_conversation_situation_worker", None)
        guard = getattr(self, "_world_mutation_guard", None)
        return {
            "snapshot": snapshot.to_payload(),
            "worker": (
                worker.stats(self.session_key) if worker is not None else {}
            ),
            "world_mutation_guard": (
                guard.last_decision() if guard is not None else {}
            ),
        }

    def conversation_judgment_shadow_diagnostics(
        self, *, include_rows: bool = False, include_evidence: bool = False, limit: int = 20,
    ) -> dict[str, Any]:
        from app.core.conversation.judgment_shadow import JudgmentShadowStore

        report = JudgmentShadowStore(self._chat_db).report(
            self.session_key, include_rows=include_rows, include_evidence=include_evidence,
            limit=limit,
        )
        report["enabled"] = bool(
            getattr(self._settings.agent, "conversation_judgment_shadow_enabled", True)
        )
        report["worker_available"] = (
            getattr(self, "_conversation_situation_worker", None) is not None
        )
        return report


__all__ = ["ConversationSituationMixin"]
