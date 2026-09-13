"""Window-aware Live policy prompt. Not a second Aiko brain."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.concepts.concept_diets import (
    ConceptDiet,
    DietTuning,
    diet_for,
    resolve_budget,
)
from app.core.infra import timephrase
from app.core.live.frame import LiveSituationFrame
from app.core.live.impulse import LiveImpulse
from app.core.live.main_wake import TRANSCRIPT_FLOOR_TOKENS, TRANSCRIPT_MAX_ROWS
from app.core.live.proposal import LIVE_POLICY_INTENTS
from app.core.live.urge import LiveUrge
from app.core.session.prompt_support import clip_text_to_tokens
from app.llm.token_utils import chars_per_token, estimate_tokens

_SAFETY_TOKENS = 256
_INSTRUCTIONS = """You are a Live behavior policy, not a companion.
Choose exactly one intent for this moment. Output a complete JSON object.
reason_code is a short snake_case tag. You may put one or two sentences
of arguments.reasoning; keep it brief so the JSON finishes.
World truth in SITUATION is authoritative. Do not invent a competing
scene (for example coding versus watching anime) when the frame already
names what is happening.
Do not quote dialogue. Do not write a conversational reply.
Never emit Live2D Param IDs, .exp3, .motion3, or motion_group names.
Prefer wait or noop when nothing new has happened.
When the user just sent a message or the turn is active, prefer
attend or acknowledge_user; wait and noop are for idle.
A micro_utterance is a permitted delivery for acknowledge_user,
react_affectively, or backchannel_user only. Put the spoken line in
arguments.text (at most eight words, no question, no facts, no names)
and set arguments.delivery to micro_utterance. Skip that delivery
when speech_budget is forbidden or rare.
context_refs may be empty, or copy an id shown in this prompt
(generation number, urge:…, impulse:…, concept:…). Invented refs
are ignored.
request_main_speech is only for a substantive contribution the main
companion model should make — never for a backchannel or acknowledgement.
Use LAST CONVERSATION to judge whether a main-wake would continue the
thread or interrupt it. Still do not quote dialogue in the JSON.
Intents: {intents}.
""".replace("{intents}", ", ".join(LIVE_POLICY_INTENTS))


@dataclass(frozen=True, slots=True)
class LivePolicyPrompt:
    messages: list[dict[str, str]]
    region_tokens: dict[str, int]
    total_tokens: int
    available_tokens: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "region_tokens": dict(self.region_tokens),
            "total_tokens": self.total_tokens,
            "available_tokens": self.available_tokens,
        }


@dataclass
class LivePolicyPromptAssembler:
    """Fill named regions against the live_policy context window.

    Does not call PromptAssembler.assemble_with_budget (that would pull
    persona, T3, and tools). Overflow drops from the bottom.
    """

    diet: ConceptDiet | None = field(default_factory=lambda: diet_for("live_policy"))

    def assemble(
        self,
        *,
        frame: LiveSituationFrame,
        concepts: Sequence[Any] = (),
        transcript_rows: Sequence[Any] = (),
        impulses: Sequence[LiveImpulse] = (),
        journal_entries: Sequence[Any] = (),
        ledger: Sequence[Any] = (),
        urges: Sequence[LiveUrge] = (),
        context_window: int,
        max_tokens: int,
        prompt_ceiling: int,
        diet_tuning: DietTuning | None = None,
    ) -> LivePolicyPrompt:
        window = max(1024, int(context_window or 0))
        output_cap = max(1, int(max_tokens or 512))
        ceiling = max(2000, min(24000, int(prompt_ceiling or 12000)))
        available = min(
            ceiling,
            max(0, window - output_cap - _SAFETY_TOKENS),
        )
        instructions = _INSTRUCTIONS.strip()
        situation = self._render_situation(frame)
        instr_tokens = estimate_tokens(instructions)
        sit_tokens = estimate_tokens(situation)
        reserved = instr_tokens + sit_tokens
        remain = max(0, available - reserved)

        # Transcript floor is reserved before concepts/impulses so a fat
        # concept fill cannot starve LAST CONVERSATION.
        rows = list(transcript_rows or [])[-TRANSCRIPT_MAX_ROWS:]
        transcript_floor = min(TRANSCRIPT_FLOOR_TOKENS, remain) if rows else 0
        work = max(0, remain - transcript_floor)

        tuning = diet_tuning or DietTuning(context_window=window)
        concept_allowance = 0
        if self.diet is not None:
            concept_allowance = resolve_budget(self.diet, tuning)
        concept_budget = min(concept_allowance, work)
        concepts_text = self._render_concepts(concepts, concept_budget)
        concept_tokens = estimate_tokens(concepts_text) if concepts_text else 0
        remain_work = max(0, work - concept_tokens)

        transcript = self._render_transcript(rows, transcript_floor)
        transcript_tokens = estimate_tokens(transcript) if transcript else 0

        tail = self._render_tail(
            urges=urges,
            impulses=impulses,
            journal_entries=journal_entries,
            ledger=ledger,
            budget=remain_work,
        )
        tail_tokens = estimate_tokens(tail) if tail else 0

        body_parts = [instructions, "SITUATION:\n" + situation]
        extras: list[str] = []
        if concepts_text:
            extras.append("CONCEPTS:\n" + concepts_text)
        if transcript:
            extras.append("LAST CONVERSATION:\n" + transcript)
        if tail:
            extras.append("IMPULSES AND HISTORY:\n" + tail)
        extra_text = "\n\n".join(extras)
        extra_budget = max(0, available - reserved)
        if extra_text and estimate_tokens(extra_text) > extra_budget:
            extra_text = _clip_tail(extra_text, extra_budget)
        text = "\n\n".join(body_parts + ([extra_text] if extra_text else []))
        total = estimate_tokens(text)
        return LivePolicyPrompt(
            messages=[{"role": "user", "content": text}],
            region_tokens={
                "instructions": instr_tokens,
                "situation": sit_tokens,
                "concepts": concept_tokens,
                "transcript": transcript_tokens,
                "impulses": tail_tokens,
            },
            total_tokens=total,
            available_tokens=available,
        )

    def _render_situation(self, frame: LiveSituationFrame) -> str:
        inferred = str(frame.shared.inferred.label or "unknown")
        sharing = str(frame.shared.sharing or "unknown")
        app = str(frame.shared.user_active_app or "none")
        os_idle = str(frame.shared.os_idle or "missing")
        session_s = int(frame.shared.session_duration_s or 0)
        activity = str(frame.shared.world_activity or "idle")
        attention = f"{frame.attention.target} {frame.attention.mode}".strip()
        sleep = str(frame.constraints.sleep_status or "awake")
        budget = str(frame.constraints.speech_budget or "normal")
        epoch = str(frame.epoch.kind or "data_only")
        commit = str(frame.commitment.intention or "wait")
        rails = "; ".join(frame.continuity.behavior_rails)
        return (
            f"generation={frame.generation} epoch={epoch}\n"
            f"world_truth inferred={inferred} sharing={sharing} "
            f"app={app} os_idle={os_idle} session_s={session_s} "
            f"sessions={int(frame.shared.session_count or 0)} "
            f"idle_s={int(frame.shared.idle_span_s or 0)} "
            f"lock_s={int(frame.shared.lock_span_s or 0)} "
            f"activity={activity}\n"
            f"attention={attention} commitment={commit}\n"
            f"sleep={sleep} speech_budget={budget} "
            f"live_quiet={frame.constraints.dnd}\n"
            f"rails={rails or 'none'}"
        )

    def _render_concepts(self, concepts: Sequence[Any], budget: int) -> str:
        if budget <= 0:
            return ""
        lines: list[str] = []
        spent = 0
        for item in concepts:
            lane = str(getattr(item, "lane", "") or "")
            kind = str(getattr(item, "kind", "") or "")
            label = str(getattr(item, "label", "") or "").strip()
            if not label:
                continue
            if lane == "rail":
                line = f"Hold ({kind}): {label}"
            else:
                line = f"Situation ({kind}): {label}"
            cost = estimate_tokens(line) + 1
            if spent + cost > budget:
                break
            lines.append(line)
            spent += cost
        return "\n".join(lines)

    def _render_transcript(self, rows: Sequence[Any], budget: int) -> str:
        if budget <= 0 or not rows:
            return ""
        anchor = timephrase.today_anchor()
        body = timephrase.format_transcript(rows)
        text = f"{anchor}\n{body}" if body else anchor
        return _clip_tail(text, budget)

    def _render_tail(
        self,
        *,
        urges: Sequence[LiveUrge],
        impulses: Sequence[LiveImpulse],
        journal_entries: Sequence[Any],
        ledger: Sequence[Any],
        budget: int,
    ) -> str:
        if budget <= 0:
            return ""
        lines: list[str] = []
        now_ms = 0.0
        if impulses:
            now_ms = max(float(item.monotonic_ms) for item in impulses)
        for urge in urges:
            age_ms = 0
            if now_ms and urge.created_monotonic_ms:
                age_ms = max(0, int(now_ms - float(urge.created_monotonic_ms)))
            salience = 0.0
            if urge.salience_inputs:
                salience = sum(float(v) for v in urge.salience_inputs.values())
            parked = " parked" if urge.state == "parked" else ""
            lines.append(
                f"urge id={urge.urge_id} kind={urge.kind} "
                f"state={urge.state}{parked} salience={salience:.2f} "
                f"age_ms={age_ms}"
            )
        for impulse in list(impulses)[-8:]:
            lines.append(
                f"impulse {impulse.kind} gen={impulse.mode_generation}"
            )
        for entry in list(journal_entries)[-6:]:
            kind = str(getattr(entry, "kind", "") or "")
            text = str(getattr(entry, "text", "") or "")[:120]
            if kind:
                lines.append(f"journal {kind}: {text}")
        for row in list(ledger)[-4:]:
            action = str(getattr(row, "proposed_action", "") or "")
            result = str(getattr(row, "arbiter_result", "") or "")
            if action or result:
                lines.append(f"ledger proposed={action} arbiter={result}")
        return _clip_tail("\n".join(lines), budget)


def _clip_tail(text: str, max_tokens: int) -> str:
    """Keep the most recent tokens. History overflow must not starve situation."""
    if max_tokens <= 0:
        return ""
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    budget_chars = max(1, int(max_tokens * chars_per_token()))
    if len(text) <= budget_chars:
        return clip_text_to_tokens(text, max_tokens)
    return text[-budget_chars:]


__all__ = ["LivePolicyPrompt", "LivePolicyPromptAssembler"]
