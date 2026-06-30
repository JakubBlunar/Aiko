"""Conversation arc tracker (Phase 4c).

A conversation drifts through "arcs": casual_check_in, support, planning,
reflection, playful, silly. Aiko sounds more present when she
acknowledges where she is in that drift, e.g. mode-matching prosody and
turning down the suggestion volume when the user is venting.

Three-track design:

  * **Hot path (regex-only)**: :class:`ArcEstimator` runs per user turn,
    inspects the current message + a tiny rolling buffer, and emits a
    candidate arc with confidence ~0.5. Cost is microseconds.

  * **Self-tag (H1)**: when Aiko emits ``[[arc:X]]`` in her reply
    (parsed by ``response_text_service``) the response-text dispatch
    site calls :meth:`ArcStore.set_from_self_tag` with confidence 0.85.
    Sits between regex and smoother on purpose -- her own read of the
    room outranks a cheap regex but defers to the periodic LLM smoother.

  * **Cold path (LLM smoothing)**: :class:`ArcSmootherWorker` runs every
    N turns on the speaking-window scheduler. Looks at a wider history
    slice, asks the model to confirm or change the arc, and writes the
    result back via :class:`ArcStore` with confidence ~0.95.

Confidence ordering (regex 0.5 < self-tag 0.85 < smoother 0.95) is
enforced by :meth:`ArcEstimator.apply_turn`: a regex hit never
overwrites a self-tag- or smoother-set arc.

All three paths persist into the existing ``conversation_arc`` table:

    user_id TEXT PRIMARY KEY,
    arc TEXT NOT NULL DEFAULT 'casual_check_in',
    since_turn INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.5,
    updated_at TEXT NOT NULL
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from app.core.session.session_text_utils import resolve_user_name, speaker_label

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.conversation_arc")


VALID_ARCS: tuple[str, ...] = (
    "casual_check_in",
    "support",
    "planning",
    "reflection",
    "playful",
    "silly",
)


@dataclass(slots=True, frozen=True)
class ArcState:
    user_id: str
    arc: str
    since_turn: int
    confidence: float
    updated_at: str

    def to_payload(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "arc": self.arc,
            "since_turn": int(self.since_turn),
            "confidence": round(float(self.confidence), 3),
            "updated_at": self.updated_at,
        }


# ── store ────────────────────────────────────────────────────────────────


class ArcStore:
    """SQLite CRUD over the ``conversation_arc`` table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get(self, user_id: str) -> ArcState | None:
        if not user_id:
            return None
        row = self._db.execute_fetchone(
            "SELECT user_id, arc, since_turn, confidence, updated_at "
            "FROM conversation_arc WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return None
        return ArcState(
            user_id=str(row[0] or user_id),
            arc=str(row[1] or "casual_check_in"),
            since_turn=int(row[2] or 0),
            confidence=float(row[3] or 0.5),
            updated_at=str(row[4] or self._now_iso()),
        )

    def get_or_default(self, user_id: str) -> ArcState:
        existing = self.get(user_id)
        if existing is not None:
            return existing
        return ArcState(
            user_id=user_id,
            arc="casual_check_in",
            since_turn=0,
            confidence=0.5,
            updated_at=self._now_iso(),
        )

    def set_from_self_tag(
        self,
        user_id: str,
        arc: str,
        *,
        since_turn: int,
    ) -> ArcState:
        """Write Aiko's ``[[arc:X]]`` self-tag at confidence ``0.85``.

        Sits between regex (~0.5) and the LLM smoother (~0.95) on
        purpose: her own read of the room outranks a cheap regex hit
        but defers to the periodic smoothing pass. ``arc`` is validated
        against :data:`VALID_ARCS`; an unknown value is rejected and
        returns the existing state unchanged.

        Same-arc emissions don't reset ``since_turn`` -- a string of
        ``[[arc:support]]`` tags across consecutive replies represents
        one continuous support arc, not many.
        """
        normalised = (arc or "").strip().lower()
        if normalised not in VALID_ARCS:
            log.debug("ignoring self-tag with unknown arc %r", arc)
            return self.get_or_default(user_id)
        prior = self.get(user_id)
        anchor_turn = (
            prior.since_turn
            if prior is not None and prior.arc == normalised
            else int(since_turn)
        )
        return self.upsert(
            user_id,
            arc=normalised,
            since_turn=anchor_turn,
            confidence=0.85,
        )

    def upsert(
        self,
        user_id: str,
        *,
        arc: str,
        since_turn: int,
        confidence: float,
    ) -> ArcState:
        if arc not in VALID_ARCS:
            arc = "casual_check_in"
        confidence = max(0.0, min(1.0, float(confidence)))
        now = self._now_iso()
        self._db.execute_commit(
            "INSERT INTO conversation_arc (user_id, arc, since_turn, "
            "confidence, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET arc=excluded.arc, "
            "since_turn=excluded.since_turn, "
            "confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (user_id, arc, int(since_turn), confidence, now),
        )
        return ArcState(
            user_id=user_id,
            arc=arc,
            since_turn=int(since_turn),
            confidence=confidence,
            updated_at=now,
        )

    def render_block(
        self,
        user_id: str,
        *,
        current_turn: int = 0,
        user_display_name: str = "the user",
    ) -> str:
        state = self.get(user_id)
        if state is None or state.arc == "casual_check_in" and state.confidence < 0.55:
            return ""
        elapsed = max(0, int(current_turn) - int(state.since_turn))
        descriptor = arc_descriptor(state.arc, user_display_name)
        if elapsed > 0:
            return f"Conversation arc: {descriptor} (last ~{elapsed} turns)."
        return f"Conversation arc: {descriptor}."


# Arc descriptors with a ``{name}`` placeholder filled when the
# descriptor is read; only the support arc references the user by name
# in its surface line. ``arc_descriptor()`` does the substitution so
# callers don't have to.
_ARC_DESCRIPTORS: dict[str, str] = {
    "casual_check_in": "casual check-in",
    "support": "{name} is venting / needs support — listen more, fix less",
    "planning": "we're planning something concrete",
    "reflection": "reflective / introspective stretch",
    "playful": "playful banter",
    "silly": "silly / lighthearted goofing-around stretch",
}


def arc_descriptor(arc: str, user_display_name: str = "the user") -> str:
    """Return the descriptor for ``arc`` with the user's name folded in."""
    template = _ARC_DESCRIPTORS.get(arc, "casual check-in")
    if "{name}" in template:
        return template.format(name=user_display_name or "the user")
    return template


# ── hot-path estimator ──────────────────────────────────────────────────


_SUPPORT_RE = re.compile(
    r"\b(?:i\s+(?:feel|am)\s+(?:so\s+|really\s+|kinda\s+|sorta\s+)?"
    r"(?:tired|exhausted|stressed|sad|anxious|down|overwhelmed|lonely|"
    r"frustrated|burned\s*out|stuck|miserable)|"
    r"i\s+(?:don'?t|can'?t)\s+(?:cope|handle|deal)|"
    r"(?:rough|bad|hard|tough)\s+(?:day|week|night))\b",
    re.IGNORECASE,
)
_PLANNING_RE = re.compile(
    r"\b(?:let'?s\s+(?:plan|figure\s+out|map\s+out|sketch)|"
    r"how\s+(?:do|should)\s+(?:we|i)\s+(?:tackle|approach|do|set\s+up)|"
    r"(?:next|first)\s+steps?|action\s+items?|deadline)\b",
    re.IGNORECASE,
)
_PLAYFUL_RE = re.compile(
    r"\b(?:lol|lmao|rofl|hahaha+|tee?hee+|"
    r"that'?s\s+(?:hilarious|wild|insane)|"
    r"you'?re\s+(?:ridiculous|funny))\b",
    re.IGNORECASE,
)
_SILLY_RE = re.compile(
    r"\b(?:what\s+if\s+(?:we|i|you|the)\s+(?:just\s+)?"
    r"(?:turned|made|became|tried|invented)|"
    r"imagine\s+(?:if|a\s+world|a\s+universe)|"
    r"(?:ridiculous|absurd|wacky|wild|goofy|silly)\s+"
    r"(?:idea|thought|notion|plan)|"
    r"this\s+is\s+(?:so\s+|kinda\s+)?(?:silly|goofy|absurd|ridiculous)|"
    r"would\s+be\s+(?:hilarious|silly|wild|absurd)\s+if|"
    r"(?:you'?re|you\s+are)\s+(?:so\s+)?silly)\b",
    re.IGNORECASE,
)
_REFLECTION_RE = re.compile(
    r"\b(?:i'?ve\s+been\s+thinking|been\s+wondering|"
    r"(?:i\s+)?realized|i'?m\s+starting\s+to\s+(?:think|see)|"
    r"looking\s+back|in\s+hindsight)\b",
    re.IGNORECASE,
)


# Regex confidences are intentionally capped at ~0.55 so the hot path
# sits below the self-tag (0.85) and smoother (0.95) cliffs. Same arc
# regex hits still bump confidence via the same-arc path in apply_turn,
# but a regex never overwrites a higher-confidence prior on its own.
_ARC_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("support", _SUPPORT_RE, 0.55),
    ("planning", _PLANNING_RE, 0.5),
    ("reflection", _REFLECTION_RE, 0.5),
    ("silly", _SILLY_RE, 0.5),
    ("playful", _PLAYFUL_RE, 0.5),
)


def estimate_arc(user_text: str) -> str | None:
    """Pure regex arc guess for a single utterance, or ``None``.

    Shares the exact ``_ARC_PATTERNS`` ladder :class:`ArcEstimator` uses
    on the hot path, but with no store / confidence bookkeeping — handy
    for offline passes (e.g. K73 shared-ritual shape derivation) that
    only need the coarse arc label of a message.
    """
    text = (user_text or "").strip()
    if not text:
        return None
    for arc, pattern, _conf in _ARC_PATTERNS:
        if pattern.search(text):
            return arc
    return None


class ArcEstimator:
    """Regex-only classifier (microseconds) that proposes an arc per turn."""

    def __init__(
        self,
        store: ArcStore,
        *,
        sticky_confidence: float = 0.78,
        decay_per_turn: float = 0.02,
    ) -> None:
        self._store = store
        self._sticky = max(0.5, min(0.99, float(sticky_confidence)))
        self._decay = max(0.0, float(decay_per_turn))

    def estimate(self, user_text: str) -> tuple[str, float] | None:
        text = (user_text or "").strip()
        if not text:
            return None
        for arc, pattern, conf in _ARC_PATTERNS:
            if pattern.search(text):
                return arc, conf
        return None

    def apply_turn(
        self,
        user_id: str,
        *,
        user_text: str,
        current_turn: int,
    ) -> ArcState:
        candidate = self.estimate(user_text)
        prior = self._store.get_or_default(user_id)
        # Decay the prior confidence a bit each turn so old signals fade.
        decayed = max(0.0, prior.confidence - self._decay)
        if candidate is None:
            # No fresh signal: keep arc, gently decay confidence.
            return self._store.upsert(
                user_id,
                arc=prior.arc,
                since_turn=prior.since_turn,
                confidence=decayed,
            )
        new_arc, new_conf = candidate
        # Confidence-ladder guard: regex (~0.5) must never overwrite a
        # self-tag (0.85) or smoother (0.95)-set arc on a different
        # value. The +0.1 buffer means a same-arc bump from a stronger
        # regex still works, but a switch to a different arc only fires
        # when prior confidence is below the sticky cliff. Self-tags
        # land at 0.85 which sits above the default sticky 0.78, so
        # they're naturally protected; the explicit 0.85 floor below
        # documents the H1 contract regardless of `sticky_confidence`.
        if (
            prior.arc != new_arc
            and (
                prior.confidence >= 0.85
                or prior.confidence >= self._sticky
            )
            and new_conf < prior.confidence + 0.1
        ):
            return self._store.upsert(
                user_id,
                arc=prior.arc,
                since_turn=prior.since_turn,
                confidence=decayed,
            )
        # Same arc as before: bump confidence (capped) and keep since_turn.
        if prior.arc == new_arc:
            return self._store.upsert(
                user_id,
                arc=prior.arc,
                since_turn=prior.since_turn,
                confidence=min(1.0, max(prior.confidence, new_conf)),
            )
        # Different arc: switch with the candidate's confidence.
        return self._store.upsert(
            user_id,
            arc=new_arc,
            since_turn=int(current_turn),
            confidence=new_conf,
        )


# ── cold-path smoother ──────────────────────────────────────────────────


def _build_smooth_prompt(user_display_name: str = "the user") -> str:
    name = user_display_name or "the user"
    return (
        "You are Aiko's quiet smoothing routine. You will receive (1) the "
        "current arc tag we have on file and (2) a slice of recent "
        "conversation. Confirm or change the arc and emit a single JSON "
        "object:\n"
        "\n"
        "{\"arc\": \"<one of: casual_check_in | support | planning | "
        "reflection | playful | silly>\", \"confidence\": <0..1>}\n"
        "\n"
        "Rules:\n"
        "- Pick the arc that best describes the *current vibe*, not the topic.\n"
        f"- \"support\" wins when {name} is venting / asking for empathy.\n"
        "- \"planning\" wins when we're concretely organising next steps.\n"
        "- \"reflection\" wins on introspective / looking-back stretches.\n"
        "- \"playful\" is for warm banter; \"silly\" is for absurd, "
        "low-stakes goofing around (\"what if cats ran the post office\").\n"
        "- \"casual_check_in\" is the default. Use it freely if nothing else fits.\n"
        "- Output ONLY the JSON object. No prose."
    )


_SMOOTH_PROMPT = _build_smooth_prompt()


class ArcSmootherWorker:
    """Speaking-window LLM smoother for the conversation arc."""

    def __init__(
        self,
        *,
        ollama: "OllamaClient",
        store: ArcStore,
        model: str,
        every_n_turns: int = 6,
        max_history_chars: int = 2000,
        max_tokens: int = 80,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._store = store
        self._model = model
        self._every_n = max(1, int(every_n_turns))
        self._max_history_chars = max(400, int(max_history_chars))
        self._max_tokens = max(40, int(max_tokens))
        self._user_display_name_provider = user_display_name_provider
        self._user_turns_seen = 0
        self._user_turns_at_last_smooth = 0
        self._stats = {
            "scheduled": 0,
            "skipped_throttled": 0,
            "skipped_no_history": 0,
            "completed": 0,
            "failed": 0,
            "switches": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def notify_user_turn(self) -> None:
        self._user_turns_seen += 1

    def should_run(self) -> bool:
        return (
            self._user_turns_seen - self._user_turns_at_last_smooth
            >= self._every_n
        )

    def maybe_run(
        self,
        user_id: str,
        *,
        history_provider: Callable[[], Iterable[tuple[str, str]]],
        current_turn: int,
    ) -> ArcState | None:
        if not self.should_run():
            self._stats["skipped_throttled"] += 1
            return None
        self._user_turns_at_last_smooth = self._user_turns_seen
        self._stats["scheduled"] += 1
        try:
            history = list(history_provider() or [])
        except Exception:
            log.debug("history provider failed", exc_info=True)
            history = []
        if not history:
            self._stats["skipped_no_history"] += 1
            return None
        prior = self._store.get_or_default(user_id)
        block = _format_smooth_block(
            prior,
            history,
            max_chars=self._max_history_chars,
            user_display_name=resolve_user_name(
                self._user_display_name_provider,
            ),
        )
        try:
            messages = [
                {
                    "role": "system",
                    "content": _build_smooth_prompt(
                        resolve_user_name(self._user_display_name_provider),
                    ),
                },
                {"role": "user", "content": block},
            ]
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.1,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                surface="conversation_arc",
            )
        except Exception:
            log.debug("arc smoother LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        parsed = _parse_smooth_output(raw)
        if parsed is None:
            self._stats["failed"] += 1
            return None
        new_arc, new_conf = parsed
        if new_arc == prior.arc:
            new_state = self._store.upsert(
                user_id,
                arc=prior.arc,
                since_turn=prior.since_turn,
                confidence=max(prior.confidence, new_conf),
            )
        else:
            new_state = self._store.upsert(
                user_id,
                arc=new_arc,
                since_turn=int(current_turn),
                confidence=new_conf,
            )
            self._stats["switches"] += 1
        self._stats["completed"] += 1
        return new_state


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_smooth_output(raw: str) -> tuple[str, float] | None:
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
        return None
    if not isinstance(data, dict):
        return None
    arc = str(data.get("arc") or "").strip().lower()
    if arc not in VALID_ARCS:
        return None
    try:
        conf = float(data.get("confidence", 0.6))
    except Exception:
        conf = 0.6
    return arc, max(0.0, min(1.0, conf))


def _format_smooth_block(
    prior: ArcState,
    history: list[tuple[str, str]],
    *,
    max_chars: int,
    user_display_name: str = "Jacob",
) -> str:
    msg_lines: list[str] = []
    total = 0
    for role, content in reversed(history):
        text = (content or "").strip()
        if not text:
            continue
        speaker = speaker_label(role, user_display_name)
        line = f"{speaker}: {text}"
        if total + len(line) > max_chars and msg_lines:
            break
        msg_lines.append(line)
        total += len(line) + 1
    msg_lines.reverse()
    convo = "\n".join(msg_lines)
    return (
        f"Current arc on file: {prior.arc} "
        f"(confidence={prior.confidence:.2f}, since_turn={prior.since_turn}).\n\n"
        f"Recent conversation:\n{convo}"
    )


__all__ = [
    "ArcEstimator",
    "ArcSmootherWorker",
    "ArcState",
    "ArcStore",
    "VALID_ARCS",
    "estimate_arc",
    "_format_smooth_block",
    "_parse_smooth_output",
]
