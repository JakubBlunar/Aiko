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

    def update_from_user_text(self, user_text: str) -> bool:
        text = " ".join(str(user_text or "").split())
        if not text:
            return False

        candidate = self._extract_user_note(text)
        if not candidate:
            return False

        normalized_candidate = candidate.lower()
        for existing in self._profile.user_notes:
            normalized_existing = existing.lower()
            if normalized_candidate == normalized_existing:
                return False
            if normalized_candidate in normalized_existing or normalized_existing in normalized_candidate:
                return False

        self._profile.user_notes.append(candidate)
        self._profile.user_notes = self._profile.user_notes[-20:]
        self._profile.updated_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

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
            return PersonaProfile(assistant_background="", user_notes=[], updated_at="")

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return PersonaProfile(assistant_background="", user_notes=[], updated_at="")

        if not isinstance(payload, dict):
            return PersonaProfile(assistant_background="", user_notes=[], updated_at="")

        background = str(payload.get("assistant_background", "") or "").strip()
        notes_raw = payload.get("user_notes", [])
        notes: list[str] = []
        if isinstance(notes_raw, list):
            for item in notes_raw:
                text = str(item or "").strip()
                if text:
                    notes.append(text)
        updated_at = str(payload.get("updated_at", "") or "").strip()
        return PersonaProfile(assistant_background=background, user_notes=notes, updated_at=updated_at)

    def _save(self) -> None:
        payload = {
            "assistant_background": self._profile.assistant_background,
            "user_notes": self._profile.user_notes,
            "updated_at": self._profile.updated_at,
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
