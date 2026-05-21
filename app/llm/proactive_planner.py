"""Background 'director' JSON for proactive speech + optional hints for the main model."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProactiveDirectorPlan:
    """Parsed output from the small planner model."""

    speak: bool = False
    kind: str = ""
    utterance_seed: str = ""
    draft_line: str = ""
    hints_for_next_user_turn: str = ""
    avoid: list[str] = field(default_factory=list)
    suggested_steps: list[str] = field(default_factory=list)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return text


def parse_director_json(raw: str) -> ProactiveDirectorPlan:
    """Parse planner JSON; on failure return speak=False."""
    text = _strip_json_fences(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ProactiveDirectorPlan()
    if not isinstance(data, dict):
        return ProactiveDirectorPlan()
    speak = bool(data.get("speak", False))
    kind = str(data.get("kind", "") or "").strip()[:80]
    utterance_seed = str(data.get("utterance_seed", "") or "").strip()[:500]
    draft_line = str(data.get("draft_line", "") or "").strip()[:400]
    hints = str(data.get("hints_for_next_user_turn", "") or "").strip()
    if len(hints) > 1200:
        hints = hints[:1200] + "…"
    avoid_raw = data.get("avoid", [])
    avoid: list[str] = []
    if isinstance(avoid_raw, list):
        for item in avoid_raw[:12]:
            s = str(item).strip()
            if s:
                avoid.append(s[:120])
    steps_raw = data.get("suggested_steps", [])
    suggested_steps: list[str] = []
    if isinstance(steps_raw, list):
        for item in steps_raw[:8]:
            if isinstance(item, dict):
                label = str(item.get("label", "") or "").strip()
                detail = str(item.get("detail", "") or "").strip()
                line = f"{label}: {detail}".strip(": ").strip()
                if line:
                    suggested_steps.append(line[:200])
            else:
                s = str(item).strip()
                if s:
                    suggested_steps.append(s[:200])
    return ProactiveDirectorPlan(
        speak=speak,
        kind=kind,
        utterance_seed=utterance_seed,
        draft_line=draft_line,
        hints_for_next_user_turn=hints,
        avoid=avoid,
        suggested_steps=suggested_steps,
    )


DIRECTOR_SYSTEM_PROMPT = """You are a JSON-only conversation director for a voice assistant.
You MUST respond with a single JSON object and nothing else. No markdown, no commentary.

Your job: read the recent transcript and optional personality notes, then decide:
- Whether the assistant should proactively SPEAK during silence (only meaningful if user is in a live voice session — the client will ignore speak otherwise).
- Short hints for the MAIN model on the user's NEXT message (tone, follow-ups, what to avoid repeating).

Be conservative: default speak to false unless a light, specific interjection clearly fits.

JSON shape (all keys optional except speak):
{
  "speak": false,
  "kind": "silence_banter|follow_up|new_thread|",
  "utterance_seed": "short phrase the assistant could say if speak is true",
  "draft_line": "if speak true, optional 1-2 sentences ready to speak (plain text, no tags)",
  "hints_for_next_user_turn": "brief guidance for the main model on the next reply",
  "avoid": ["topic strings to avoid repeating"],
  "suggested_steps": ["short optional advisory steps — not executed as tools"]
}

Rules:
- speak: boolean, default false.
- If speak is false, leave draft_line and utterance_seed short or empty.
- hints_for_next_user_turn: max ~400 characters of useful steering; can be empty.
- Do not include medical/legal instructions; do not instruct harmful content.
"""


def build_director_user_message(
    *,
    time_ctx: str,
    transcript_lines: list[str],
    note_lines: list[str],
    topic_list: str,
    live_voice: bool,
) -> str:
    notes_block = "\n".join(note_lines) if note_lines else "(none)"
    trans_block = "\n".join(transcript_lines) if transcript_lines else "(empty)"
    return (
        f"Current time: {time_ctx}\n"
        f"Live voice session active (proactive speech allowed): {live_voice}\n\n"
        f"Recent transcript (oldest first):\n{trans_block}\n\n"
        f"Personality notes:\n{notes_block}\n\n"
        f"Recent topics to avoid repeating: {topic_list}\n\n"
        "Output the JSON object now."
    )


def build_utterance_expand_prompt(*, seed: str, avoid: list[str]) -> str:
    avoid_s = ", ".join(avoid[:6]) if avoid else "(none)"
    return (
        "Expand the following seed into 1-2 short spoken sentences for a voice assistant. "
        "Casual, natural, no [[reaction:...]] tags, no markdown.\n\n"
        f"Seed: {seed}\n"
        f"Avoid mentioning: {avoid_s}\n\n"
        "Reply with only the spoken text, nothing else."
    )


def extract_json_object_from_text(text: str) -> str:
    """If model wrapped extra text, try to isolate {...}."""
    text = _strip_json_fences(text)
    if text.startswith("{"):
        return text
    match = re.search(r"\{[\s\S]*\}", text)
    return match.group(0) if match else text
