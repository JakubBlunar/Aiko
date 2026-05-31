"""Per-turn user-state estimator + store (Phase 3a).

This is the *fast* sibling of UserProfileWorker: pure regex / heuristic
inference of how Jacob seems *right now* (mood, energy, focus, latest
topic). Runs after every user turn (no LLM, ~0.5ms) and overwrites the
single-row ``user_state_now`` table per user.

The prompt block this produces is one short line, biased toward signals
the LLM can use to adjust tone (e.g. "Jacob seems a bit terse — be
concise"). When a signal is unclear we omit it rather than guess; the
prompt block silently shrinks instead of fabricating data.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.user_state")


# Mood-detection patterns. Order matters: the first match wins, with
# negative matches earlier than positive so "not great" doesn't read as
# "great".
_MOOD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(
        r"\b(?:not (?:great|good|well|happy)|bad|awful|terrible|"
        r"stress(?:ed|ful)?|anxious|tired|exhausted|frustrated|sad|"
        r"upset|down|annoyed|overwhelmed|burned out)\b",
        re.IGNORECASE,
    ), "low"),
    (re.compile(
        r"\b(?:great|good|fine|happy|excited|amazing|wonderful|"
        r"glad|stoked|pumped|fantastic)\b",
        re.IGNORECASE,
    ), "high"),
    (re.compile(
        r"\b(?:okay|ok|alright|fine|so-so|meh)\b",
        re.IGNORECASE,
    ), "neutral"),
)

_HIGH_ENERGY_RE = re.compile(
    r"!|\b(?:wow|whoa|love it|hyped|fired up|amazing|let's go)\b",
    re.IGNORECASE,
)
_LOW_ENERGY_RE = re.compile(
    r"\b(?:tired|exhausted|sleepy|drained|wiped|low energy|burned out)\b",
    re.IGNORECASE,
)

_FOCUS_QUESTION_RE = re.compile(r"\?")
_FOCUS_TASK_RE = re.compile(
    r"\b(?:fix|debug|implement|design|deploy|ship|finish|build|figure out|"
    r"try to|need to|trying to|working on)\b",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class UserStateNow:
    user_id: str
    perceived_mood: str = "unknown"
    perceived_energy: str = "unknown"
    perceived_focus: str = "unknown"
    last_topic: str = ""
    updated_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "perceived_mood": self.perceived_mood,
            "perceived_energy": self.perceived_energy,
            "perceived_focus": self.perceived_focus,
            "last_topic": self.last_topic,
            "updated_at": self.updated_at,
        }


class UserStateStore:
    """SQLite single-row CRUD for ``user_state_now``."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def get(self, user_id: str) -> UserStateNow:
        if not user_id:
            return UserStateNow(user_id="")
        row = self._db.execute_fetchone(
            "SELECT user_id, perceived_mood, perceived_energy, perceived_focus, "
            "last_topic, updated_at FROM user_state_now WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return UserStateNow(user_id=user_id)
        return UserStateNow(
            user_id=str(row[0] or user_id),
            perceived_mood=str(row[1] or "unknown"),
            perceived_energy=str(row[2] or "unknown"),
            perceived_focus=str(row[3] or "unknown"),
            last_topic=str(row[4] or ""),
            updated_at=str(row[5] or ""),
        )

    def upsert(self, state: UserStateNow) -> None:
        if not state.user_id:
            return
        now = state.updated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._db.execute_commit(
            "INSERT INTO user_state_now (user_id, perceived_mood, "
            "perceived_energy, perceived_focus, last_topic, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "perceived_mood=excluded.perceived_mood, "
            "perceived_energy=excluded.perceived_energy, "
            "perceived_focus=excluded.perceived_focus, "
            "last_topic=excluded.last_topic, "
            "updated_at=excluded.updated_at",
            (
                state.user_id,
                state.perceived_mood,
                state.perceived_energy,
                state.perceived_focus,
                state.last_topic,
                now,
            ),
        )

    def render_block(
        self,
        user_id: str,
        *,
        user_display_name: str = "the user",
    ) -> str:
        s = self.get(user_id)
        bits: list[str] = []
        if s.perceived_mood and s.perceived_mood != "unknown":
            bits.append(f"mood reads as {s.perceived_mood}")
        if s.perceived_energy and s.perceived_energy != "unknown":
            bits.append(f"energy {s.perceived_energy}")
        if s.perceived_focus and s.perceived_focus != "unknown":
            bits.append(f"focus on {s.perceived_focus}")
        topic = (s.last_topic or "").strip()
        if not bits and not topic:
            return ""
        line = (
            f"Right now {user_display_name}: " + ", ".join(bits)
            if bits
            else ""
        )
        if topic:
            if line:
                line += f" — last topic: {topic}"
            else:
                line = f"Last topic from {user_display_name}: {topic}"
        return line


class UserStateEstimator:
    """Pure heuristic: turn user_text + last reaction into a UserStateNow."""

    def __init__(self, store: UserStateStore) -> None:
        self._store = store

    def estimate(
        self,
        user_id: str,
        *,
        user_text: str,
        previous: UserStateNow | None = None,
    ) -> UserStateNow:
        text = (user_text or "").strip()
        prev = previous or self._store.get(user_id)
        if not text:
            return prev

        mood = _detect_mood(text) or prev.perceived_mood
        energy = _detect_energy(text) or prev.perceived_energy
        focus = _detect_focus(text) or prev.perceived_focus
        topic = _extract_topic(text) or prev.last_topic
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return UserStateNow(
            user_id=user_id,
            perceived_mood=mood,
            perceived_energy=energy,
            perceived_focus=focus,
            last_topic=topic,
            updated_at=now,
        )

    def apply_turn(
        self,
        user_id: str,
        *,
        user_text: str,
    ) -> UserStateNow:
        new_state = self.estimate(user_id, user_text=user_text)
        try:
            self._store.upsert(new_state)
        except Exception:
            log.debug("user state upsert failed", exc_info=True)
        return new_state


def _detect_mood(text: str) -> str | None:
    for pattern, label in _MOOD_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _detect_energy(text: str) -> str | None:
    if _LOW_ENERGY_RE.search(text):
        return "low"
    if _HIGH_ENERGY_RE.search(text):
        return "high"
    word_count = len(text.split())
    if word_count <= 3:
        return "low"
    if word_count >= 30:
        return "high"
    return None


def _detect_focus(text: str) -> str | None:
    if _FOCUS_QUESTION_RE.search(text):
        return "asking"
    if _FOCUS_TASK_RE.search(text):
        return "working"
    return None


_TOPIC_TRIM_RE = re.compile(r"\s+")


def _extract_topic(text: str) -> str | None:
    text = _TOPIC_TRIM_RE.sub(" ", text).strip()
    if not text:
        return None
    # Cap to ~80 chars and trim to a sentence boundary if convenient.
    snippet = text[:80]
    if len(text) > 80:
        for sep in (". ", "? ", "! "):
            cut = snippet.rfind(sep)
            if cut > 30:
                snippet = snippet[: cut + 1]
                break
        else:
            snippet = snippet.rstrip(",;:") + "…"
    return snippet


__all__ = [
    "UserStateNow",
    "UserStateStore",
    "UserStateEstimator",
]
