from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re


DEFAULT_PERSONA_PATH = Path(__file__).resolve().parents[2] / "data" / "persona_profile.json"


@dataclass(slots=True)
class PersonaProfile:
    assistant_background: str
    user_notes: list[str]
    response_style: str
    tts_length_scale: float
    updated_at: str


class PersonaProfileStore:
    def __init__(self, path: Path | None = None, assistant_background: str | None = None) -> None:
        self._path = path or DEFAULT_PERSONA_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._profile = self._load()

        initial_background = str(assistant_background or "").strip()
        if initial_background and not self._profile.assistant_background:
            self._profile.assistant_background = initial_background
            self._profile.updated_at = datetime.now(timezone.utc).isoformat()
            self._save()

    def get_assistant_background(self) -> str:
        return self._profile.assistant_background

    def get_user_notes(self, max_notes: int = 6) -> list[str]:
        count = max(1, int(max_notes))
        return list(self._profile.user_notes[-count:])

    def get_response_style(self) -> str:
        return str(self._profile.response_style or "balanced").strip().lower() or "balanced"

    def get_tts_length_scale(self) -> float:
        try:
            value = float(self._profile.tts_length_scale)
        except Exception:
            return 1.0
        return max(0.65, min(value, 1.35))

    def update_from_user_text(self, user_text: str) -> bool:
        text = " ".join(str(user_text or "").split())
        if not text:
            return False

        changed = False
        pref_changed = self._update_preferences_from_text(text)
        changed = changed or pref_changed

        candidate = self._extract_user_note(text)
        if not candidate:
            if changed:
                self._profile.updated_at = datetime.now(timezone.utc).isoformat()
                self._save()
            return changed

        normalized_candidate = candidate.lower()
        for existing in self._profile.user_notes:
            normalized_existing = existing.lower()
            if normalized_candidate == normalized_existing:
                if changed:
                    self._profile.updated_at = datetime.now(timezone.utc).isoformat()
                    self._save()
                return changed
            if normalized_candidate in normalized_existing or normalized_existing in normalized_candidate:
                if changed:
                    self._profile.updated_at = datetime.now(timezone.utc).isoformat()
                    self._save()
                return changed

        self._profile.user_notes.append(candidate)
        self._profile.user_notes = self._profile.user_notes[-20:]
        self._profile.updated_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

    def _update_preferences_from_text(self, text: str) -> bool:
        lowered = text.lower()
        updated = False

        concise_triggers = (
            "be concise",
            "short reply",
            "shorter reply",
            "short replies",
            "keep replies short",
            "reply shortly",
            "one short sentence",
            "keep it short",
            "respond quickly",
            "faster response",
        )
        detailed_triggers = (
            "more detail",
            "longer reply",
            "explain more",
            "go deeper",
            "more explanation",
        )
        faster_tts_triggers = (
            "speak faster",
            "talk faster",
            "too slow",
            "faster voice",
            "speed up",
        )
        slower_tts_triggers = (
            "speak slower",
            "talk slower",
            "too fast",
            "slow down",
            "slower voice",
        )

        if any(trigger in lowered for trigger in concise_triggers):
            if self._profile.response_style != "concise":
                self._profile.response_style = "concise"
                updated = True
        elif any(trigger in lowered for trigger in detailed_triggers):
            if self._profile.response_style != "detailed":
                self._profile.response_style = "detailed"
                updated = True

        if any(trigger in lowered for trigger in faster_tts_triggers):
            if abs(self._profile.tts_length_scale - 0.9) > 1e-6:
                self._profile.tts_length_scale = 0.9
                updated = True
        elif any(trigger in lowered for trigger in slower_tts_triggers):
            if abs(self._profile.tts_length_scale - 1.12) > 1e-6:
                self._profile.tts_length_scale = 1.12
                updated = True

        return updated

    def _extract_user_note(self, text: str) -> str | None:
        lowered = text.lower()

        patterns: list[tuple[str, str]] = [
            (r"\bmy name is\s+([^.!?]{2,50})", "User's name is {value}."),
            (r"\bi(?:'m| am) working on\s+([^.!?]{3,120})", "User is working on {value}."),
            (r"\bmy goal is\s+([^.!?]{3,120})", "User's goal is {value}."),
            (r"\bi(?:'m| am) trying to\s+([^.!?]{3,120})", "User is trying to {value}."),
            (r"\bi prefer\s+([^.!?]{3,100})", "User prefers {value}."),
            (r"\bi like\s+([^.!?]{3,100})", "User likes {value}."),
        ]

        for pattern, template in patterns:
            match = re.search(pattern, lowered, flags=re.IGNORECASE)
            if not match:
                continue
            raw_value = match.group(1).strip(" ,;:\t\n\r")
            if not raw_value:
                continue
            cleaned_value = self._normalize_fragment(raw_value)
            if len(cleaned_value) < 3:
                continue
            return template.format(value=cleaned_value)

        if len(text) <= 80 and any(phrase in lowered for phrase in (" i ", " i'm ", " my ")):
            return f"User said: {self._normalize_fragment(text)}"
        return None

    @staticmethod
    def _normalize_fragment(value: str) -> str:
        cleaned = value.replace("`", "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = cleaned[:120].rstrip()
        return cleaned

    def _load(self) -> PersonaProfile:
        if not self._path.exists():
            return PersonaProfile(
                assistant_background="",
                user_notes=[],
                response_style="balanced",
                tts_length_scale=1.0,
                updated_at="",
            )

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return PersonaProfile(
                assistant_background="",
                user_notes=[],
                response_style="balanced",
                tts_length_scale=1.0,
                updated_at="",
            )

        if not isinstance(payload, dict):
            return PersonaProfile(
                assistant_background="",
                user_notes=[],
                response_style="balanced",
                tts_length_scale=1.0,
                updated_at="",
            )

        background = str(payload.get("assistant_background", "") or "").strip()
        notes_raw = payload.get("user_notes", [])
        notes: list[str] = []
        if isinstance(notes_raw, list):
            for item in notes_raw:
                text = str(item or "").strip()
                if text:
                    notes.append(text)
        response_style = str(payload.get("response_style", "balanced") or "balanced").strip().lower()
        if response_style not in {"balanced", "concise", "detailed"}:
            response_style = "balanced"
        try:
            tts_length_scale = float(payload.get("tts_length_scale", 1.0) or 1.0)
        except Exception:
            tts_length_scale = 1.0
        tts_length_scale = max(0.65, min(tts_length_scale, 1.35))
        updated_at = str(payload.get("updated_at", "") or "").strip()
        return PersonaProfile(
            assistant_background=background,
            user_notes=notes,
            response_style=response_style,
            tts_length_scale=tts_length_scale,
            updated_at=updated_at,
        )

    def _save(self) -> None:
        payload = {
            "assistant_background": self._profile.assistant_background,
            "user_notes": self._profile.user_notes,
            "response_style": self._profile.response_style,
            "tts_length_scale": self._profile.tts_length_scale,
            "updated_at": self._profile.updated_at,
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
