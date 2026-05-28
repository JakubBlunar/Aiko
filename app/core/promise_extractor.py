"""Promise extraction (Phase 3c).

A two-track design that catches commitments without inflating the hot
path:

  Track 1 — Post-turn regex (fast, ~1ms):
    Matches obvious patterns in the *just-completed* user/assistant text:
      "I'll <verb>"
      "I want to <verb>"
      "I should remember to <verb>"
      "I need to <verb>"
      "remind me to <verb>"
      "let me know <about>"
      "I'll check <X>"
    Each match becomes a ``kind="promise"`` memory with a moderate salience.

  Track 2 — Speaking-window LLM extractor:
    Catches subtler commitments the regex misses:
      "Maybe this weekend I'll finally start running"
      "I keep meaning to call my mom"
    Runs on the SpeakingWindowScheduler at low priority, throttled by
    user-turn cadence. Output is a JSON list of {who, what, deadline?}
    objects which we render into prose memories.

ProactiveDirector consumes the result implicitly via RAG (promise
memories rank high enough to surface when the user is silent) and
explicitly through :func:`pick_pending_for_proactive` which the
director can call to get the freshest unprompted promise.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.session_text_utils import resolve_user_name

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase
    from app.core.memory_store import Memory, MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.promise_extractor")


# ── regex patterns ───────────────────────────────────────────────────────


_USER_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "I'll do X" / "I will do X" / "I'm going to do X"
    re.compile(
        r"\bI(?:'ll| will| am going to| 'm gonna| 'm going to)\s+([a-z][^.!?\n]{4,120})",
        re.IGNORECASE,
    ),
    # "I want to..." / "I'd like to..." / "I plan to..."
    re.compile(
        r"\bI(?:'d| would)? (?:want|plan|hope|intend|mean) to\s+([a-z][^.!?\n]{4,120})",
        re.IGNORECASE,
    ),
    # "I need to..." / "I should..." / "I have to..." (allow one adverb).
    re.compile(
        r"\bI(?:\s+\w+)?\s+(?:need to|should|have to|gotta)\s+([a-z][^.!?\n]{4,120})",
        re.IGNORECASE,
    ),
    # "remind me to X" / "make sure I X"
    re.compile(
        r"\b(?:remind me to|make sure I)\s+([a-z][^.!?\n]{4,120})",
        re.IGNORECASE,
    ),
)

_ASSISTANT_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Assistant: "I'll remind you" / "I'll check on..." / "I'll let you know..."
    re.compile(
        r"\bI(?:'ll| will)\s+(remind you|check (?:on|in)|let you know|follow up)([^.!?\n]{0,120})",
        re.IGNORECASE,
    ),
    # "Let me know how X goes" — assistant asking the user to report back.
    re.compile(
        r"\b(?:let me know|tell me) (?:how|when|if|whether)\s+([a-z][^.!?\n]{4,120})",
        re.IGNORECASE,
    ),
)


@dataclass(slots=True)
class Promise:
    """A single promise extracted from a turn (regex or LLM)."""

    who: str  # "user" | "assistant"
    text: str
    raw_match: str = ""
    source_turn_id: int | None = None
    source: str = "regex"  # "regex" | "llm"
    confidence: float = 0.5

    def to_memory_content(self, user_display_name: str = "Jacob") -> str:
        """Render to a natural-language memory string.

        ``user_display_name`` defaults to "Jacob" for back-compat with
        callers that don't pass a name; the SessionController caller
        threads the configured name through.
        """
        actor = (user_display_name or "the user") if self.who == "user" else "Aiko"
        # Prefix with the actor so "Aiko" promises don't read as the user's.
        return f"{actor} promised: {self.text.strip()}"


def extract_regex(
    *,
    user_text: str,
    assistant_text: str,
    source_turn_id: int | None = None,
) -> list[Promise]:
    """Run the regex extractors over both sides of the turn."""
    out: list[Promise] = []
    seen: set[str] = set()

    def _add(who: str, text: str, raw: str) -> None:
        body = (text or "").strip(" \"'.,;:")
        if len(body) < 4:
            return
        key = (who, body.lower())
        key_str = "|".join(key)
        if key_str in seen:
            return
        seen.add(key_str)
        out.append(Promise(
            who=who,
            text=body[:160],
            raw_match=(raw or "")[:160],
            source_turn_id=source_turn_id,
            source="regex",
            confidence=0.65,
        ))

    for pat in _USER_PROMISE_PATTERNS:
        for m in pat.finditer(user_text or ""):
            _add("user", m.group(1), m.group(0))
    for pat in _ASSISTANT_PROMISE_PATTERNS:
        for m in pat.finditer(assistant_text or ""):
            groups = [g for g in m.groups() if g]
            text = " ".join(groups).strip() if groups else ""
            _add("assistant", text, m.group(0))
    return out


# ── LLM extractor ────────────────────────────────────────────────────────


_LLM_PROMPT = """\
You are extracting concrete commitments from a brief conversation.
Return ONE JSON object on a single line:
{
  "promises": [
    {"who": "user"|"assistant", "what": "<short verb phrase>", "deadline": "<text|null>"}
  ]
}

Rules:
- A "promise" is a concrete intent to do, find out, follow up on, or
  remember something. Vague feelings ("I might..." with no action) are NOT
  promises.
- "what" is a SHORT verb phrase (under 20 words). Do not echo the literal
  sentence — paraphrase to the action.
- "deadline" is null unless the speaker named a specific time/day.
- 0-3 items max. Empty array is fine when nothing fits.
- Output ONLY valid JSON, no prose around it."""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _parse_llm_payload(raw: str) -> list[Promise]:
    text = (raw or "").strip()
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        m = _JSON_BLOCK_RE.search(text)
        candidate = m.group(0) if m else None
    if not candidate:
        return []
    try:
        data = json.loads(candidate)
    except Exception:
        log.debug("promise JSON parse failed", exc_info=True)
        return []
    items = data.get("promises") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[Promise] = []
    for entry in items[:3]:
        if not isinstance(entry, dict):
            continue
        who_raw = str(entry.get("who") or "").lower().strip()
        who = "user" if who_raw in {"user", "jacob"} else (
            "assistant" if who_raw in {"assistant", "aiko"} else "user"
        )
        what = str(entry.get("what") or "").strip()
        if len(what) < 4:
            continue
        deadline = entry.get("deadline")
        deadline_str = ""
        if isinstance(deadline, str):
            deadline_str = deadline.strip()
        body = what
        if deadline_str and deadline_str.lower() not in {"null", "none"}:
            body = f"{what} (by {deadline_str})"
        out.append(Promise(
            who=who,
            text=body[:200],
            source="llm",
            confidence=0.75,
        ))
    return out


# ── extractor coordinator ────────────────────────────────────────────────


class PromiseExtractor:
    """Coordinates regex (post-turn) + LLM (speaking-window) tracks."""

    def __init__(
        self,
        *,
        ollama: "OllamaClient",
        memory_store: "MemoryStore | None",
        embedder: "Embedder | None",
        model: str,
        regex_salience: float = 0.55,
        llm_salience: float = 0.65,
        llm_min_user_turns: int = 4,
        llm_max_history_chars: int = 2000,
        llm_max_tokens: int = 220,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._memory_store = memory_store
        self._embedder = embedder
        self._model = model
        self._regex_salience = max(0.0, min(1.0, float(regex_salience)))
        self._llm_salience = max(0.0, min(1.0, float(llm_salience)))
        self._llm_min_user_turns = max(1, int(llm_min_user_turns))
        self._llm_max_history_chars = max(500, int(llm_max_history_chars))
        self._llm_max_tokens = max(80, int(llm_max_tokens))
        self._user_display_name_provider = user_display_name_provider
        self._user_turns_seen = 0
        self._user_turns_at_last_llm = 0
        self._stats = {
            "regex_matches": 0,
            "regex_persisted": 0,
            "llm_scheduled": 0,
            "llm_skipped_throttled": 0,
            "llm_completed": 0,
            "llm_failed": 0,
            "llm_persisted": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def notify_user_turn(self) -> None:
        self._user_turns_seen += 1

    # ── regex track ─────────────────────────────────────────────────────

    def extract_post_turn(
        self,
        *,
        user_text: str,
        assistant_text: str,
        session_key: str | None,
        source_turn_id: int | None = None,
    ) -> list[Promise]:
        """Run the regex extractor and persist promises. Returns the list."""
        promises = extract_regex(
            user_text=user_text,
            assistant_text=assistant_text,
            source_turn_id=source_turn_id,
        )
        self._stats["regex_matches"] += len(promises)
        if not promises:
            return []
        for p in promises:
            wrote = self._persist(p, session_key=session_key, salience=self._regex_salience)
            if wrote:
                self._stats["regex_persisted"] += 1
        return promises

    # ── LLM track ───────────────────────────────────────────────────────

    def should_run_llm(self) -> bool:
        return (
            self._user_turns_seen - self._user_turns_at_last_llm
            >= self._llm_min_user_turns
        )

    def maybe_run_llm(
        self,
        *,
        session_key: str,
        history_provider: Callable[[], Iterable[tuple[str, str]]],
    ) -> list[Promise] | None:
        if not self.should_run_llm():
            self._stats["llm_skipped_throttled"] += 1
            return None
        self._user_turns_at_last_llm = self._user_turns_seen
        self._stats["llm_scheduled"] += 1
        try:
            history = list(history_provider() or [])
        except Exception:
            log.debug("history_provider failed", exc_info=True)
            history = []
        if not history:
            return []
        block = _format_history(
            history,
            max_chars=self._llm_max_history_chars,
            user_display_name=resolve_user_name(
                self._user_display_name_provider,
            ),
        )
        if not block:
            return []
        try:
            messages = [
                {"role": "system", "content": _LLM_PROMPT},
                {"role": "user", "content": block},
            ]
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.2,
                    "num_predict": self._llm_max_tokens,
                },
                model=self._model,
                surface="promise_extractor",
            )
        except Exception:
            log.debug("promise LLM call failed", exc_info=True)
            self._stats["llm_failed"] += 1
            return None
        promises = _parse_llm_payload(raw)
        for p in promises:
            wrote = self._persist(
                p, session_key=session_key, salience=self._llm_salience,
            )
            if wrote:
                self._stats["llm_persisted"] += 1
        self._stats["llm_completed"] += 1
        return promises

    # ── persistence ─────────────────────────────────────────────────────

    def _persist(
        self,
        promise: Promise,
        *,
        session_key: str | None,
        salience: float,
    ) -> bool:
        store = self._memory_store
        embedder = self._embedder
        if store is None or embedder is None:
            return False
        content = promise.to_memory_content(
            user_display_name=resolve_user_name(
                self._user_display_name_provider,
            ),
        )
        try:
            emb = embedder.embed(content)
        except Exception:
            log.debug("promise embed failed", exc_info=True)
            return False
        try:
            mem = store.add(
                content=content,
                kind="promise",
                embedding=emb,
                salience=salience,
                source_session=session_key,
                source_message_id=promise.source_turn_id,
                # Schema v8: explicit promises are user-visible
                # commitments. Anchor them in long_term immediately so
                # the promotion worker never has a chance to drop them.
                tier="long_term",
                # Schema v9: promises were extracted from a literal
                # user/assistant statement of intent ("I'll do X"), so
                # the confidence floor is meaningfully higher than the
                # MemoryExtractor baseline of 0.7.
                confidence=0.85,
            )
        except Exception:
            log.debug("promise insert failed", exc_info=True)
            return False
        return mem is not None


def _format_history(
    history: list[tuple[str, str]],
    *,
    max_chars: int,
    user_display_name: str = "Jacob",
) -> str:
    if not history:
        return ""
    lines: list[str] = []
    total = 0
    user_name = (user_display_name or "").strip() or "the user"
    for role, content in reversed(history):
        text = (content or "").strip()
        if not text:
            continue
        speaker = user_name if role == "user" else "Aiko"
        line = f"{speaker}: {text}"
        if total + len(line) > max_chars and lines:
            break
        lines.append(line)
        total += len(line) + 1
    lines.reverse()
    return "\n".join(lines)


__all__ = [
    "Promise",
    "PromiseExtractor",
    "extract_regex",
    "_parse_llm_payload",
]
