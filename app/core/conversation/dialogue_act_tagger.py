"""Dialogue-act tagging (K4).

Per-turn classification of *what the user is doing on this turn* -- the
intent shape rather than the topic. Six values:

  * ``question`` -- asking, including soft ``request`` -- ``can you / could you``
    is folded into ``question`` for v1; we can split if a downstream
    consumer ever needs it.
  * ``story`` -- a self-contained narrative beat ("today I went to...").
    Default fallback for longer messages with no other signal.
  * ``vent`` -- emotional release / processing. Loud punctuation,
    "I hate / I can't / why does..." stems, ALL-CAPS density.
  * ``banter`` -- playful punch-back, lol / lmao / haha / "you're funny".
  * ``planning`` -- "let's...", "what if we...", "next steps", deadline
    talk; same family as the conversation-arc planning bucket but tagged
    per-turn.
  * ``chitchat`` -- low-info filler beats: "anyway", "so yeah", "btw",
    very short messages with no other handle.

Two-track design (mirrors :mod:`promise_extractor`):

  Track 1 -- Regex hot path (~1ms): runs inline from
  ``post_turn_mixin._post_turn_inner_life`` to stamp
  ``messages.dialogue_act`` synchronously.

  Track 2 -- LLM cold path: when the regex's confidence is low (no
  pattern matched, or only the fallback fired) the post-turn flow
  schedules an LLM upgrade on the speaking-window scheduler. The worker
  re-tags the message and patches ``messages.dialogue_act`` if the LLM
  disagrees with the regex.

Downstream consumers (RAG retriever, ProactiveDirector) read the column
straight from ``messages``; this module is intentionally
write-only-from-the-extractor's perspective.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from app.core.session.session_text_utils import resolve_user_name

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.dialogue_act_tagger")


VALID_DIALOGUE_ACTS: tuple[str, ...] = (
    "question",
    "story",
    "vent",
    "banter",
    "planning",
    "chitchat",
)

# Confidence floor that flags a regex result as "low" so the LLM cold
# path will be scheduled. Fallback ``story`` and a no-match
# ``chitchat`` both sit at this value; a clean regex hit (vent /
# planning / question) lands higher.
_LOW_CONFIDENCE: float = 0.45


# ── regex patterns ───────────────────────────────────────────────────────


_QUESTION_RE = re.compile(
    r"\?|"
    r"^\s*(?:what|where|when|why|how|who|which|whose|whom|"
    r"can|could|would|will|should|do|does|did|is|are|was|were)\b",
    re.IGNORECASE | re.MULTILINE,
)
_PLANNING_RE = re.compile(
    r"\b(?:let'?s\s+(?:plan|figure\s+out|map\s+out|sketch|try|do|"
    r"start|go|build|make|tackle)|"
    r"what\s+if\s+we|"
    r"how\s+(?:do|should)\s+(?:we|i)\s+(?:tackle|approach|do|set\s+up)|"
    r"(?:next|first)\s+steps?|action\s+items?|deadline|"
    r"by\s+(?:tomorrow|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|next\s+week|the\s+end\s+of))\b",
    re.IGNORECASE,
)
_BANTER_RE = re.compile(
    r"\b(?:lol|lmao|rofl|hahaha+|tee?hee+|hehehe+|omg|"
    r"that'?s\s+(?:hilarious|wild|insane)|"
    r"you'?re\s+(?:silly|ridiculous|funny|hilarious|the\s+worst))\b",
    re.IGNORECASE,
)
_VENT_RE = re.compile(
    r"\b(?:i\s+(?:hate|can'?t\s+stand|am\s+so\s+done\s+with)|"
    r"i\s+(?:don'?t|can'?t)\s+(?:cope|handle|deal|even)|"
    r"why\s+(?:does|is|are|do)\s+(?:this|everything|they|he|she)|"
    r"(?:so|really|completely)\s+(?:exhausted|drained|fed\s+up|"
    r"frustrated|over\s+it)|"
    r"(?:rough|bad|hard|tough|terrible|awful)\s+"
    r"(?:day|week|night|morning))\b",
    re.IGNORECASE,
)
# Repeated punctuation / sentence-internal ALL CAPS clusters as a
# secondary vent signal -- evaluated separately from the lexical regex
# so a calm message with "AI" or "USA" doesn't trip it.
_VENT_LOUDNESS_RE = re.compile(
    r"(?:!{2,}|\?{2,}|"
    r"\b[A-Z]{4,}(?:\s+[A-Z]{2,}){1,}\b)",
)
_CHITCHAT_RE = re.compile(
    r"\b(?:anyway|so\s+yeah|btw|by\s+the\s+way|nvm|nm|"
    r"how\s+(?:are\s+you|'s\s+it\s+going|'s\s+life)|"
    r"good\s+(?:morning|night|afternoon|evening)|"
    r"hey(?:\s+there)?|hi(?:ya)?|sup|yo)\b",
    re.IGNORECASE,
)


# Confidence assigned per regex track when it fires. Story is the
# fallback bucket so it sits at the low-confidence floor; the LLM cold
# path will revisit story-tagged turns first.
_REGEX_CONFIDENCE: dict[str, float] = {
    "vent": 0.78,
    "planning": 0.72,
    "banter": 0.68,
    "question": 0.65,
    "chitchat": 0.55,
    "story": _LOW_CONFIDENCE,
}


@dataclass(frozen=True, slots=True)
class DialogueActResult:
    """A single per-turn dialogue act read."""

    act: str
    confidence: float
    source: str  # "regex" | "llm" | "fallback"


# ── regex hot path ───────────────────────────────────────────────────────


def tag_regex(user_text: str) -> DialogueActResult:
    """Classify ``user_text`` with regex only (microseconds).

    Order matters: vent + planning + banter + question are the *loud*
    signals and run first. ``chitchat`` catches short filler beats;
    ``story`` is the catch-all for longer prose with no specific
    handle and sits at the low-confidence floor so the cold path
    upgrades it.
    """
    text = (user_text or "").strip()
    if not text:
        return DialogueActResult(act="chitchat", confidence=0.4, source="fallback")

    if _VENT_RE.search(text) or _VENT_LOUDNESS_RE.search(text):
        return DialogueActResult(
            act="vent",
            confidence=_REGEX_CONFIDENCE["vent"],
            source="regex",
        )
    if _PLANNING_RE.search(text):
        return DialogueActResult(
            act="planning",
            confidence=_REGEX_CONFIDENCE["planning"],
            source="regex",
        )
    if _BANTER_RE.search(text):
        return DialogueActResult(
            act="banter",
            confidence=_REGEX_CONFIDENCE["banter"],
            source="regex",
        )
    if _QUESTION_RE.search(text):
        return DialogueActResult(
            act="question",
            confidence=_REGEX_CONFIDENCE["question"],
            source="regex",
        )
    if _CHITCHAT_RE.search(text) or len(text) < 25:
        return DialogueActResult(
            act="chitchat",
            confidence=_REGEX_CONFIDENCE["chitchat"],
            source="regex",
        )
    # Fallback: longer prose with no specific signal -> story; low
    # confidence so the LLM cold path is the one that confirms it.
    return DialogueActResult(
        act="story",
        confidence=_REGEX_CONFIDENCE["story"],
        source="fallback",
    )


# ── LLM cold path ────────────────────────────────────────────────────────


_LLM_PROMPT = """\
You classify ONE user turn by *intent*, not topic. Return one JSON
object on a single line:

{"act": "<question|story|vent|banter|planning|chitchat>", "confidence": <0..1>}

Definitions:
- question: asking for information / clarification / a soft request.
- story: a self-contained narrative beat ("today I went to...").
- vent: emotional release. Frustration, exhaustion, loud punctuation.
- banter: playful punch-back. lol / haha / teasing.
- planning: "let's...", organising next steps, deadline talk.
- chitchat: short filler. "hey", "anyway", "how's it going".

Rules:
- Pick the BEST single label. If two fit, pick the louder one
  (vent > planning > banter > question > chitchat > story).
- Output ONLY the JSON object. No prose."""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _parse_llm_payload(raw: str) -> DialogueActResult | None:
    text = (raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        m = _JSON_BLOCK_RE.search(text)
        candidate = m.group(0) if m else None
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except Exception:
        log.debug("dialogue_act JSON parse failed", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    act = str(data.get("act") or "").strip().lower()
    if act not in VALID_DIALOGUE_ACTS:
        return None
    try:
        conf = float(data.get("confidence", 0.7))
    except Exception:
        conf = 0.7
    return DialogueActResult(
        act=act,
        confidence=max(0.0, min(1.0, conf)),
        source="llm",
    )


# ── coordinator ──────────────────────────────────────────────────────────


class DialogueActTagger:
    """Coordinates regex (post-turn) + LLM (speaking-window) tracks.

    The regex hot path is a free function (:func:`tag_regex`); this
    class wraps it with the cadence book-keeping and the LLM upgrade.
    Persistence is delegated to ``chat_db.update_message_dialogue_act``
    so this module never touches SQL directly.
    """

    def __init__(
        self,
        *,
        ollama: "OllamaClient" | None = None,
        chat_db: "ChatDatabase | None" = None,
        model: str | None = None,
        llm_min_user_turns: int = 3,
        llm_max_history_chars: int = 1200,
        llm_max_tokens: int = 60,
        low_confidence_threshold: float = _LOW_CONFIDENCE,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._chat_db = chat_db
        self._model = model
        self._llm_min_user_turns = max(1, int(llm_min_user_turns))
        self._llm_max_history_chars = max(400, int(llm_max_history_chars))
        self._llm_max_tokens = max(40, int(llm_max_tokens))
        self._low_threshold = max(0.0, min(1.0, float(low_confidence_threshold)))
        self._user_display_name_provider = user_display_name_provider
        self._user_turns_seen = 0
        self._user_turns_at_last_llm = 0
        self._stats = {
            "regex_calls": 0,
            "llm_scheduled": 0,
            "llm_skipped_throttled": 0,
            "llm_completed": 0,
            "llm_failed": 0,
            "llm_disagreed": 0,
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

    def tag_user_turn(self, user_text: str) -> DialogueActResult:
        """Run the regex hot path and return the result.

        Caller is responsible for persisting the result via
        ``chat_db.update_message_dialogue_act``.
        """
        self._stats["regex_calls"] += 1
        return tag_regex(user_text)

    # ── LLM track ───────────────────────────────────────────────────────

    def should_run_llm(self, *, regex_result: DialogueActResult) -> bool:
        if self._ollama is None or not self._model:
            return False
        if regex_result.confidence > self._low_threshold:
            return False
        return (
            self._user_turns_seen - self._user_turns_at_last_llm
            >= self._llm_min_user_turns
        )

    def maybe_run_llm(
        self,
        *,
        message_id: int,
        user_text: str,
        regex_result: DialogueActResult,
        history_provider: Callable[[], Iterable[tuple[str, str]]] | None = None,
    ) -> DialogueActResult | None:
        """Run the LLM cold path. Persists if the LLM disagrees with regex.

        Returns the LLM result on success, ``None`` when throttled,
        skipped, or the LLM call failed.
        """
        if not self.should_run_llm(regex_result=regex_result):
            self._stats["llm_skipped_throttled"] += 1
            return None
        self._user_turns_at_last_llm = self._user_turns_seen
        self._stats["llm_scheduled"] += 1
        history: list[tuple[str, str]] = []
        if history_provider is not None:
            try:
                history = list(history_provider() or [])
            except Exception:
                log.debug("dialogue_act history provider failed", exc_info=True)
                history = []
        block = _format_block(
            user_text=user_text,
            history=history,
            max_chars=self._llm_max_history_chars,
            user_display_name=resolve_user_name(
                self._user_display_name_provider,
            ),
        )
        if not block:
            self._stats["llm_failed"] += 1
            return None
        try:
            messages = [
                {"role": "system", "content": _LLM_PROMPT},
                {"role": "user", "content": block},
            ]
            assert self._ollama is not None
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.1,
                    "num_predict": self._llm_max_tokens,
                },
                model=self._model,
                surface="dialogue_act_tagger",
            )
        except Exception:
            log.debug("dialogue_act LLM call failed", exc_info=True)
            self._stats["llm_failed"] += 1
            return None
        parsed = _parse_llm_payload(raw)
        if parsed is None:
            self._stats["llm_failed"] += 1
            return None
        self._stats["llm_completed"] += 1
        if parsed.act != regex_result.act:
            self._stats["llm_disagreed"] += 1
            if self._chat_db is not None and message_id > 0:
                try:
                    if self._chat_db.update_message_dialogue_act(
                        int(message_id), parsed.act,
                    ):
                        self._stats["llm_persisted"] += 1
                except Exception:
                    log.debug("dialogue_act persist failed", exc_info=True)
        return parsed


def _format_block(
    *,
    user_text: str,
    history: list[tuple[str, str]],
    max_chars: int,
    user_display_name: str = "Jacob",
) -> str:
    user_text = (user_text or "").strip()
    if not user_text:
        return ""
    user_name = (user_display_name or "").strip() or "the user"
    lines: list[str] = []
    total = 0
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
    convo = "\n".join(lines)
    return (
        f"Recent conversation (oldest first):\n{convo}\n\n"
        f"Classify ONLY this newest turn from {user_name}:\n"
        f"{user_name}: {user_text}"
    )


__all__ = [
    "DialogueActResult",
    "DialogueActTagger",
    "VALID_DIALOGUE_ACTS",
    "tag_regex",
    "_parse_llm_payload",
]
