"""Shared-moment extraction.

Two production tracks mirroring :mod:`app.core.memory.promise_extractor`:

  Track 1 — Inline ``[[moment:vibe:summary]]`` tag (cheap, Aiko-curated):
    The persona can mark something she wants to remember as a shared
    moment by emitting the tag. We strip the tag from the spoken/visible
    text upstream (alongside ``[[remember:…]]`` etc.) and persist a
    ``shared_moment`` memory row with structured ``(when, what, vibe)``
    JSON metadata.

  Track 2 — Speaking-window LLM detector (catches subtler moments):
    Runs only when a cheap signal fires (strong reaction tag, milestone
    crossed, promise kept, gift received) AND a per-turn cadence has
    elapsed. The LLM is asked to return ONE JSON object describing the
    moment, or ``null`` when nothing genuinely worth remembering happened.

  Track 3 — Manual UI ("Mark as moment"):
    Not handled here — the REST endpoint goes directly through
    :class:`SharedMomentsStore`.

The detector is intentionally conservative. We'd rather miss a small
moment than spam the timeline with noise; the user always has the manual
button as an escape hatch.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.session.session_text_utils import resolve_user_name, speaker_label

if TYPE_CHECKING:
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.shared_moment_extractor")


# Closed vibe vocabulary. The LLM detector is asked to pick from this set
# and the inline-tag parser maps unknown vibes onto ``"general"`` so we
# don't get a flood of one-off labels polluting the timeline filter.
VIBE_VOCABULARY: tuple[str, ...] = (
    "warm",
    "playful",
    "tender",
    "proud",
    "silly",
    "milestone",
    "gift",
    "comfort",
    "victory",
    "creative",
    "vulnerable",
    "general",
)


def normalise_vibe(raw: str | None) -> str:
    """Coerce a raw vibe string onto the closed vocabulary."""
    text = (raw or "").strip().lower()
    if not text:
        return "general"
    # Strip noise punctuation but keep underscores/dashes for matching.
    text = re.sub(r"[^a-z0-9_\-]", "", text)
    if text in VIBE_VOCABULARY:
        return text
    # A few common synonyms map to canonical vibes so Aiko can write
    # naturally without us re-tagging every turn.
    synonyms = {
        "funny": "playful",
        "joke": "playful",
        "joy": "playful",
        "joyful": "playful",
        "intimate": "tender",
        "soft": "tender",
        "loving": "tender",
        "love": "tender",
        "achievement": "victory",
        "win": "victory",
        "celebration": "victory",
        "present": "gift",
        "gifted": "gift",
        "creative_work": "creative",
        "vulnerability": "vulnerable",
        "honest": "vulnerable",
    }
    return synonyms.get(text, "general")


# ── data ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SharedMomentCandidate:
    """One extracted moment, awaiting persistence."""

    summary: str
    vibe: str
    when: str | None = None  # ISO8601; None means "use created_at"
    source_message_ids: list[int] | None = None
    source: str = "tag"  # "tag" | "llm" | "manual"
    confidence: float = 0.7

    def to_metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "vibe": self.vibe,
            "what": self.summary,
            "source": self.source,
            "confidence": float(self.confidence),
        }
        if self.when:
            meta["when"] = self.when
        if self.source_message_ids:
            meta["source_message_ids"] = list(self.source_message_ids)
        return meta

    def to_memory_content(self) -> str:
        return f"Shared moment ({self.vibe}): {self.summary.strip()}"


# ── Track 1: inline tag extraction ───────────────────────────────────────


# Matches ``[[moment:vibe:short summary]]``. Vibe must be a short slug
# (letters / digits / dash / underscore). Summary can contain any
# non-bracket characters, including spaces and punctuation, but must be at
# least 4 chars and at most 200.
_MOMENT_TAG_RE = re.compile(
    r"\[\[moment:([a-z][a-z0-9_\-]{0,20}):([^\[\]\n]{4,200}?)\]\]",
    re.IGNORECASE,
)


def extract_inline_tags(text: str) -> list[SharedMomentCandidate]:
    """Pull every ``[[moment:vibe:summary]]`` from text."""
    candidates: list[SharedMomentCandidate] = []
    seen: set[str] = set()
    for match in _MOMENT_TAG_RE.finditer(text or ""):
        vibe = normalise_vibe(match.group(1))
        summary = match.group(2).strip(" \"'.,;:")
        if len(summary) < 4:
            continue
        key = f"{vibe}|{summary.lower()}"
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            SharedMomentCandidate(
                summary=summary[:200],
                vibe=vibe,
                source="tag",
                confidence=0.85,
            )
        )
    return candidates


def strip_inline_tags(text: str) -> str:
    """Remove every ``[[moment:vibe:summary]]`` from text. Used by the
    response stripper so tags never reach the user-visible transcript.
    """
    return _MOMENT_TAG_RE.sub("", text or "")


# ── Track 2: LLM detector ────────────────────────────────────────────────


def _build_llm_prompt(user_display_name: str = "the user") -> str:
    """Detector prompt, templated on the user's display name."""
    name = user_display_name or "the user"
    return (
        f"You watch a brief exchange between Aiko (the AI companion) and "
        f"{name} (her user). Decide whether this turn contains a genuine "
        '"shared moment" worth remembering: a flash of laughter, tenderness, '
        "pride, a small victory, something tender that mattered. Casual "
        "chat is NOT a shared moment.\n"
        "\n"
        "Return ONE JSON object on a single line:\n"
        "{\n"
        "  \"moment\": {\n"
        f"    \"summary\": \"<short prose, under 25 words, written about *you and {name}*>\",\n"
        "    \"vibe\": \"<one of: warm | playful | tender | proud | silly | milestone | gift | comfort | victory | creative | vulnerable | general>\"\n"
        "  }\n"
        "}\n"
        "\n"
        "Rules:\n"
        "- If nothing genuinely memorable happened, return {\"moment\": null}. Be strict.\n"
        f"- \"summary\" is written from Aiko's first-person perspective, e.g. \"{name}\n"
        "  and I laughed about the cookie jar misunderstanding\".\n"
        "- 0-1 moments per call. Never invent details that aren't in the exchange.\n"
        "- Output ONLY valid JSON, no prose around it."
    )


# Back-compat constant. New code passes the resolved name to
# ``_build_llm_prompt``.
_LLM_PROMPT = _build_llm_prompt()


_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _parse_llm_payload(raw: str) -> SharedMomentCandidate | None:
    text = (raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        match = _JSON_BLOCK_RE.search(text)
        candidate = match.group(0) if match else None
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except Exception:
        log.debug("shared-moment JSON parse failed", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    moment = data.get("moment")
    if moment is None:
        return None
    if not isinstance(moment, dict):
        return None
    summary = str(moment.get("summary") or "").strip()
    if len(summary) < 8:
        return None
    vibe = normalise_vibe(moment.get("vibe"))
    return SharedMomentCandidate(
        summary=summary[:200],
        vibe=vibe,
        source="llm",
        confidence=0.7,
    )


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
    for role, content in reversed(history):
        text = (content or "").strip()
        if not text:
            continue
        speaker = speaker_label(role, user_display_name)
        line = f"{speaker}: {text}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    lines.reverse()
    return "\n".join(lines)


# Reaction tags whose presence in the turn we treat as a "moment candidate"
# signal for Track 2. Tuned to high-affect signals; everyday "neutral" or
# "thinking" reactions don't qualify on their own.
_MOMENT_REACTION_TAGS: frozenset[str] = frozenset({
    "laugh",
    "giggle",
    "warm",
    "tender",
    "love",
    "loving",
    "awe",
    "surprise",
    "joy",
    "joyful",
    "proud",
    "blush",
    "shy",
    "vulnerable",
    "sad",
    "sadness",
})

_REACTION_TAG_RE = re.compile(r"\[\[reaction:([a-z][a-z0-9_]{0,30})\]\]", re.IGNORECASE)


def detect_moment_reaction_tags(text: str) -> set[str]:
    """Return the set of moment-candidate reaction tags found in ``text``.

    Matches against :data:`_MOMENT_REACTION_TAGS`. Used by the session
    controller to gate the Track 2 LLM job (cheap signal — if none of
    these fire AND no milestone/promise/gift signal landed, we skip the
    LLM entirely).
    """
    if not text:
        return set()
    found: set[str] = set()
    for match in _REACTION_TAG_RE.finditer(text):
        tag = match.group(1).lower()
        if tag in _MOMENT_REACTION_TAGS:
            found.add(tag)
    return found


# ── extractor coordinator ────────────────────────────────────────────────


class MomentDetector:
    """Track 2 scheduler. Mirrors :class:`PromiseExtractor` shape.

    Holds the LLM call, the throttle counter, and the gating decision. The
    actual persistence is delegated to ``persist_callback`` so the session
    controller can route the result through :class:`SharedMomentsStore`.
    """

    def __init__(
        self,
        *,
        ollama: "OllamaClient",
        model: str,
        persist_callback: Callable[[SharedMomentCandidate], Any] | None = None,
        min_turn_gap: int = 5,
        cooldown_seconds: float = 300.0,
        llm_max_history_chars: int = 1600,
        llm_max_tokens: int = 180,
        user_display_name_provider: Callable[[], str] | None = None,
    ) -> None:
        self._ollama = ollama
        self._model = model
        self._persist = persist_callback
        self._min_turn_gap = max(1, int(min_turn_gap))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._llm_max_history_chars = max(400, int(llm_max_history_chars))
        self._llm_max_tokens = max(80, int(llm_max_tokens))
        self._user_display_name_provider = user_display_name_provider
        self._user_turns_seen = 0
        self._user_turns_at_last_run = 0
        self._last_run_monotonic: float | None = None
        self._stats: dict[str, int] = {
            "tag_persisted": 0,
            "llm_scheduled": 0,
            "llm_skipped_throttled": 0,
            "llm_skipped_no_signal": 0,
            "llm_completed": 0,
            "llm_failed": 0,
            "llm_returned_null": 0,
            "llm_persisted": 0,
        }

    # ── lifecycle ──

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(
        self,
        *,
        model: str | None = None,
        min_turn_gap: int | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if min_turn_gap is not None:
            self._min_turn_gap = max(1, int(min_turn_gap))
        if cooldown_seconds is not None:
            self._cooldown_seconds = max(0.0, float(cooldown_seconds))

    def notify_user_turn(self) -> None:
        self._user_turns_seen += 1

    # ── gating ──

    def should_run_llm(
        self,
        *,
        reaction_signal: bool,
        milestone_signal: bool,
        gift_signal: bool,
        promise_kept_signal: bool,
        now_monotonic: float,
    ) -> bool:
        """Cheap gate before scheduling the LLM call."""
        # Cadence: at least ``min_turn_gap`` user turns since last run.
        if (
            self._user_turns_seen - self._user_turns_at_last_run
            < self._min_turn_gap
        ):
            return False
        # Wall-clock cooldown.
        if (
            self._last_run_monotonic is not None
            and (now_monotonic - self._last_run_monotonic) < self._cooldown_seconds
        ):
            return False
        # At least one moment-worthy signal must be present.
        if not (
            reaction_signal
            or milestone_signal
            or gift_signal
            or promise_kept_signal
        ):
            return False
        return True

    def maybe_run_llm(
        self,
        *,
        history_provider: Callable[[], Iterable[tuple[str, str]]],
        now_monotonic: float,
        reaction_signal: bool,
        milestone_signal: bool,
        gift_signal: bool,
        promise_kept_signal: bool,
    ) -> SharedMomentCandidate | None:
        """Run the LLM call if gates pass. Returns the candidate (or None)."""
        if not self.should_run_llm(
            reaction_signal=reaction_signal,
            milestone_signal=milestone_signal,
            gift_signal=gift_signal,
            promise_kept_signal=promise_kept_signal,
            now_monotonic=now_monotonic,
        ):
            # Refine the skip counter so stats tell us *why*.
            cadence_ok = (
                self._user_turns_seen - self._user_turns_at_last_run
                >= self._min_turn_gap
            )
            cooldown_ok = (
                self._last_run_monotonic is None
                or (now_monotonic - self._last_run_monotonic) >= self._cooldown_seconds
            )
            if not cadence_ok or not cooldown_ok:
                self._stats["llm_skipped_throttled"] += 1
            else:
                self._stats["llm_skipped_no_signal"] += 1
            return None

        self._user_turns_at_last_run = self._user_turns_seen
        self._last_run_monotonic = now_monotonic
        self._stats["llm_scheduled"] += 1

        try:
            history = list(history_provider() or [])
        except Exception:
            log.debug("moment history_provider failed", exc_info=True)
            history = []
        if not history:
            self._stats["llm_returned_null"] += 1
            return None
        block = _format_history(
            history,
            max_chars=self._llm_max_history_chars,
            user_display_name=resolve_user_name(
                self._user_display_name_provider,
            ),
        )
        if not block:
            self._stats["llm_returned_null"] += 1
            return None

        try:
            messages = [
                {
                    "role": "system",
                    "content": _build_llm_prompt(
                        resolve_user_name(self._user_display_name_provider),
                    ),
                },
                {"role": "user", "content": block},
            ]
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.2,
                    "num_predict": self._llm_max_tokens,
                },
                model=self._model,
                surface="shared_moments",
            )
        except Exception:
            log.debug("moment LLM call failed", exc_info=True)
            self._stats["llm_failed"] += 1
            return None

        candidate = _parse_llm_payload(raw)
        self._stats["llm_completed"] += 1
        if candidate is None:
            self._stats["llm_returned_null"] += 1
            return None
        candidate.when = datetime.now(timezone.utc).isoformat()
        if self._persist is not None:
            try:
                self._persist(candidate)
                self._stats["llm_persisted"] += 1
            except Exception:
                log.debug("moment persist callback raised", exc_info=True)
        return candidate

    def note_tag_persisted(self) -> None:
        """Stat-only hook so the inline-tag path stays accountable."""
        self._stats["tag_persisted"] += 1
