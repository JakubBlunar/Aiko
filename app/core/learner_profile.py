"""Background English-tutor profile updater.

Runs the chat model in JSON mode every N successful turns, asking it to
extract structured updates from the most recent exchange. Only writes to the
existing ``personality_notes`` table — no new schema. Crucially, this is an
*incremental upsert* worker: missing categories are not erased and there is no
"decay-on-failure" path (the previous personality judge had one and routinely
wiped useful data when the model produced bad JSON).
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.chat_database import ChatDatabase
from app.core.session_text_utils import extract_json_object
from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.learner_profile")

DEFAULT_TEMPLATE_PATH = Path("data/persona/learner_profile.template.json")


_SYSTEM_PROMPT = """You are an English-tutoring memory model.

Read the latest exchange between Aiko (assistant) and Jacob (learner). Update
the learner profile JSON. Only include keys where you have new evidence
from THIS exchange — leave the rest absent. Never fabricate. Empty array is
fine for list categories.

Categories:
- english_level: one of A2, B1, B2, C1, C2, unknown
- recurring_mistakes: short phrases describing repeated grammar/usage errors
- vocabulary_taught: words/phrases the learner used or was taught (single tokens)
- topics_user_enjoys: short topic tags (e.g. "coding", "anime")
- correction_style_preference: one of minimal, inline, end_of_turn, unknown
- facts_about_user: short concrete facts (job, location, hobbies, names)

Output format (valid JSON, nothing else):
{
  "english_level": "B1" | omit,
  "recurring_mistakes": ["..."] | omit,
  "vocabulary_taught": ["..."] | omit,
  "topics_user_enjoys": ["..."] | omit,
  "correction_style_preference": "minimal" | omit,
  "facts_about_user": ["..."] | omit
}
"""


_VALID_LEVELS = {"A2", "B1", "B2", "C1", "C2", "unknown"}
_VALID_STYLES = {"minimal", "inline", "end_of_turn", "unknown"}
_LIST_CATEGORIES = {
    "recurring_mistakes",
    "vocabulary_taught",
    "topics_user_enjoys",
    "facts_about_user",
}
_SINGLE_CATEGORIES = {
    "english_level",
    "correction_style_preference",
}


@dataclass(slots=True)
class LearnerProfileResult:
    updated_categories: list[str]
    inserted_notes: int
    raw: str


class LearnerProfile:
    def __init__(
        self,
        db: ChatDatabase,
        ollama: OllamaClient,
        *,
        model: str,
        template_path: Path | str = DEFAULT_TEMPLATE_PATH,
        update_every_n_turns: int = 8,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._db = db
        self._ollama = ollama
        self._model = model
        self._template_path = Path(template_path)
        self._every_n = max(1, int(update_every_n_turns))
        self._timeout = float(timeout_seconds)
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    @property
    def update_every_n_turns(self) -> int:
        return self._every_n

    def maybe_update_async(self, session_key: str) -> bool:
        """Spawn a background update if no run is already in flight."""
        with self._inflight_lock:
            if session_key in self._inflight:
                return False
            self._inflight.add(session_key)
        threading.Thread(
            target=self._run_safe,
            args=(session_key,),
            daemon=True,
            name=f"learner-profile-{session_key[:6]}",
        ).start()
        return True

    # ── internals ────────────────────────────────────────────────────────

    def _run_safe(self, session_key: str) -> None:
        try:
            result = self._run(session_key)
            if result is not None:
                log.info(
                    "learner profile updated (%d notes inserted, categories=%s)",
                    result.inserted_notes,
                    result.updated_categories,
                )
        except Exception as exc:
            log.warning("learner profile update failed: %s", exc)
        finally:
            with self._inflight_lock:
                self._inflight.discard(session_key)

    def _run(self, session_key: str) -> LearnerProfileResult | None:
        recent = self._db.get_messages(session_key, limit=12)
        if not recent:
            return None

        transcript_lines: list[str] = []
        for row in recent:
            speaker = "Jacob" if row.role == "user" else "Aiko"
            transcript_lines.append(f"{speaker}: {row.content.strip()}")
        transcript = "\n".join(transcript_lines)

        existing = self._db.get_personality_notes(session_key)
        existing_summary = self._format_existing_for_prompt(existing)

        user_prompt = (
            f"Existing profile:\n{existing_summary or '(empty)'}\n\n"
            f"Latest exchange:\n{transcript}\n\n"
            "Return the updates as JSON."
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        raw, usage = self._ollama.chat_json(
            messages,
            model=self._model,
            timeout_seconds=self._timeout,
        )
        log.debug(
            "learner profile call: %d/%d tokens, %.0f ms eval",
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.eval_duration_ms,
        )

        payload = extract_json_object(raw)
        if not isinstance(payload, dict):
            log.info("learner profile produced non-JSON output, skipping")
            return LearnerProfileResult(updated_categories=[], inserted_notes=0, raw=raw)

        updated, inserted = self._apply_updates(session_key, payload)
        return LearnerProfileResult(
            updated_categories=updated, inserted_notes=inserted, raw=raw,
        )

    @staticmethod
    def _format_existing_for_prompt(notes: list[Any]) -> str:
        if not notes:
            return ""
        from collections import defaultdict
        grouped: dict[str, list[str]] = defaultdict(list)
        for n in notes:
            cat = getattr(n, "category", "") or "general"
            note = getattr(n, "note", "") or ""
            if note:
                grouped[cat].append(note)
        lines = []
        for cat in sorted(grouped.keys()):
            lines.append(f"- {cat}: {', '.join(grouped[cat][:6])}")
        return "\n".join(lines)

    def _apply_updates(
        self,
        session_key: str,
        payload: dict[str, Any],
    ) -> tuple[list[str], int]:
        updated: list[str] = []
        inserted = 0

        for category in _SINGLE_CATEGORIES:
            value = payload.get(category)
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if not normalized:
                continue
            if category == "english_level" and normalized not in _VALID_LEVELS:
                continue
            if category == "correction_style_preference" and normalized not in _VALID_STYLES:
                continue
            self._replace_single(session_key, category, normalized)
            updated.append(category)
            inserted += 1

        for category in _LIST_CATEGORIES:
            items = payload.get(category)
            if not isinstance(items, list):
                continue
            cleaned = [
                str(item).strip() for item in items if str(item).strip()
            ]
            cleaned = list(dict.fromkeys(cleaned))[:8]
            if not cleaned:
                continue
            for item in cleaned:
                self._db.upsert_personality_note(
                    session_key, category, item, confidence=0.9,
                )
                inserted += 1
            updated.append(category)

        return updated, inserted

    def _replace_single(self, session_key: str, category: str, value: str) -> None:
        """For single-value categories, drop any existing rows and insert one."""
        existing = [
            n for n in self._db.get_personality_notes(session_key)
            if getattr(n, "category", "") == category
        ]
        if existing:
            keep = self._db.get_personality_notes(session_key)
            replacement: list[tuple[str, str, float]] = []
            for n in keep:
                if n.category == category:
                    continue
                replacement.append((n.category, n.note, n.confidence))
            replacement.append((category, value, 0.95))
            self._db.replace_personality_notes(session_key, replacement)
        else:
            self._db.upsert_personality_note(session_key, category, value, 0.95)

    # ── unused helper kept for potential debug callers ───────────────────

    def template(self) -> dict[str, Any]:
        try:
            return json.loads(self._template_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
