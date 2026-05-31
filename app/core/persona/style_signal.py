"""K13 -- Stylometric mirror analyzer.

Tracks Jacob's writing style across recent user turns and emits a
one-line directive so Aiko's register stays calibrated even when the
recent history window doesn't cover yesterday. Sibling shape to the
K6/K18 detectors and the AikoStylePatternTracker (anti-rut) -- pure
rolling-window analyzer with no embedder, no LLM. Five axes:

  - terseness    -- ``1.0 / (1.0 + words / 8.0)`` per turn (high = terse)
  - formality    -- starts capital + ends with sentence-final punct
  - emoji_density -- emojis per word, capped at 1.0
  - slang_density -- closed-list casual markers per word, capped at 1.0
  - question_rate -- 1.0 when the turn ends with ``?``, else 0.0

The window-mean of each per-turn feature, bucketed against per-axis
thresholds, becomes the "labels" surface (``terse`` / ``chatty`` /
``formal`` / ``casual`` / ``emoji-heavy`` / ``slang-heavy`` /
``asks back often``). The persona's "How they write" section pairs
with the rendered cue.

The analyzer is constructed in :class:`SessionController` start-up
(when ``agent.style_signal_enabled``), warmed lazily on first call
from past user messages, fed by the post-turn mixin, persisted via a
tiny ``user_style_signal`` SQLite table, and surfaced as the
``style_signal`` inner-life provider on the prompt assembler.
"""
from __future__ import annotations

import collections
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


log = logging.getLogger("app.style_signal")


# Module-level defaults so tests can construct without a settings stub.
_DEFAULT_WINDOW = 30
_DEFAULT_WARMUP_MIN = 8
_DEFAULT_TERSE_THRESHOLD = 0.55
_DEFAULT_FORMAL_THRESHOLD = 0.55
_DEFAULT_EMOJI_THRESHOLD = 0.05
_DEFAULT_SLANG_THRESHOLD = 0.15
_DEFAULT_QUESTION_THRESHOLD = 0.40


# Closed list of casual chat markers + contractions. Lower-cased word-
# boundary matched per turn. Kept short on purpose -- a wide list
# would over-fire on neutral writing. We bias toward "obviously
# casual" tokens that Jacob using would tip the register.
_SLANG_MARKERS = frozenset({
    "yeah", "yea", "yup", "ya", "nope", "nah", "ok", "okie", "okay",
    "lol", "lmao", "rofl", "lel", "kek", "haha", "hehe", "heh",
    "idk", "ngl", "tbh", "imo", "imho", "irl", "btw", "afaik",
    "gonna", "wanna", "gotta", "kinda", "sorta", "tryna",
    "bro", "dude", "mate", "fam", "bruh",
    "hella", "wtf", "omg", "dunno", "ye", "ig",
})


# Conservative emoji regex covering the common Unicode pictograph
# ranges. Not exhaustive (skin-tone modifiers and ZWJ sequences would
# count their components separately) but the per-word density floor
# means a few miscounts don't change the bucket.
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-A
    "\U0001F000-\U0001F0FF"  # mahjong / cards
    "]"
)


# Sentence-final punctuation, allowing a trailing closing quote /
# paren / bracket so "Hello.\"" still counts as ending in `.`.
_SENT_END_RE = re.compile(r"[.!?][\"'»”\)\]]?\s*$")
_WORD_TOKEN_RE = re.compile(r"[a-z']+")


@dataclass(slots=True, frozen=True)
class _TurnFeatures:
    """One recorded user turn's normalised features (each in [0,1])."""

    terseness: float
    formality: float
    emoji_density: float
    slang_density: float
    is_question: float
    word_count: int


@dataclass(slots=True, frozen=True)
class StyleSignal:
    """Aggregated rolling-window style snapshot.

    Each field except ``window_size`` is a per-axis mean over the
    window in ``[0, 1]``. The signal is bucketed via :meth:`labels`
    to produce the prompt-facing register cue.
    """

    terseness: float
    formality: float
    emoji_density: float
    slang_density: float
    question_rate: float
    window_size: int

    def labels(
        self,
        *,
        terse_threshold: float = _DEFAULT_TERSE_THRESHOLD,
        formal_threshold: float = _DEFAULT_FORMAL_THRESHOLD,
        emoji_threshold: float = _DEFAULT_EMOJI_THRESHOLD,
        slang_threshold: float = _DEFAULT_SLANG_THRESHOLD,
        question_threshold: float = _DEFAULT_QUESTION_THRESHOLD,
    ) -> list[str]:
        """Bucket each axis and return only the non-default labels.

        ``terseness`` and ``formality`` use a symmetric deadzone:
        above ``threshold`` -> high label, below ``1.0 - threshold``
        -> low label, in-between -> no label. ``emoji_density``,
        ``slang_density``, and ``question_rate`` only emit a high
        label (their absence is the default register, not a signal).

        Order is fixed so the rendered string reads naturally:
        terse / chatty -> formal / casual -> emoji -> slang ->
        question.
        """
        out: list[str] = []
        terse_low = max(0.0, 1.0 - terse_threshold)
        formal_low = max(0.0, 1.0 - formal_threshold)
        if self.terseness >= terse_threshold:
            out.append("terse")
        elif self.terseness <= terse_low:
            out.append("chatty")
        if self.formality >= formal_threshold:
            out.append("formal")
        elif self.formality <= formal_low:
            out.append("casual")
        if self.emoji_density >= emoji_threshold:
            out.append("emoji-heavy")
        if self.slang_density >= slang_threshold:
            out.append("slang-heavy")
        if self.question_rate >= question_threshold:
            out.append("asks back often")
        return out


class StyleSignalAnalyzer:
    """Track Jacob's writing style across recent user turns.

    Owns a small ring of per-turn features (no vectors, no LLM). Per-
    turn cost is a few regex scans plus a deque append; ``current_signal``
    is a one-pass mean over the window. Not thread-safe; the post-turn
    pipeline calls :meth:`record_user_turn` on the turn thread and the
    assembler calls :meth:`current_signal` on that same thread.
    """

    def __init__(self, *, agent_settings: Any | None = None) -> None:
        self._agent_settings = agent_settings
        window = max(
            2,
            int(self._setting("style_signal_window", _DEFAULT_WINDOW)),
        )
        self._window: collections.deque[_TurnFeatures] = collections.deque(
            maxlen=window,
        )
        self._warmed = False

    # ── public API ────────────────────────────────────────────────────

    def record_user_turn(self, text: str) -> None:
        """Append features extracted from one user turn.

        Empty / whitespace-only inputs are silently skipped so
        idle pings or blank entries don't drag the averages.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        features = _extract_features(cleaned)
        self._window.append(features)

    def warm_from_history(
        self,
        history: Iterable[tuple[str, str]],
    ) -> None:
        """Lazy cross-session warmup. Replays past *user* turns through
        :meth:`record_user_turn`. Idempotent; only the first invocation
        actually warms.

        ``history`` is an iterable of ``(role, content)`` tuples in
        any order. Non-user rows are skipped.
        """
        if self._warmed:
            return
        self._warmed = True
        for role, content in history:
            if (role or "").lower() != "user":
                continue
            self.record_user_turn(content or "")
        log.debug(
            "style-signal: warmed from history; window=%d",
            len(self._window),
        )

    def current_signal(self) -> StyleSignal | None:
        """Return the rolling-window snapshot, or ``None`` in warmup."""
        warmup = max(
            2,
            int(
                self._setting("style_signal_warmup_min", _DEFAULT_WARMUP_MIN)
            ),
        )
        if len(self._window) < warmup:
            return None
        n = float(len(self._window))
        return StyleSignal(
            terseness=sum(f.terseness for f in self._window) / n,
            formality=sum(f.formality for f in self._window) / n,
            emoji_density=sum(f.emoji_density for f in self._window) / n,
            slang_density=sum(f.slang_density for f in self._window) / n,
            question_rate=sum(f.is_question for f in self._window) / n,
            window_size=int(n),
        )

    def labels_for_signal(self, signal: StyleSignal) -> list[str]:
        """Apply the configured per-axis thresholds to a signal."""
        return signal.labels(
            terse_threshold=float(
                self._setting(
                    "style_signal_terse_threshold",
                    _DEFAULT_TERSE_THRESHOLD,
                )
            ),
            formal_threshold=float(
                self._setting(
                    "style_signal_formal_threshold",
                    _DEFAULT_FORMAL_THRESHOLD,
                )
            ),
            emoji_threshold=float(
                self._setting(
                    "style_signal_emoji_threshold",
                    _DEFAULT_EMOJI_THRESHOLD,
                )
            ),
            slang_threshold=float(
                self._setting(
                    "style_signal_slang_threshold",
                    _DEFAULT_SLANG_THRESHOLD,
                )
            ),
            question_threshold=float(
                self._setting(
                    "style_signal_question_threshold",
                    _DEFAULT_QUESTION_THRESHOLD,
                )
            ),
        )

    # ── persistence (JSON round-trip) ─────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "warmed": bool(self._warmed),
            "window": [
                {
                    "terseness": float(f.terseness),
                    "formality": float(f.formality),
                    "emoji_density": float(f.emoji_density),
                    "slang_density": float(f.slang_density),
                    "is_question": float(f.is_question),
                    "word_count": int(f.word_count),
                }
                for f in self._window
            ],
        }

    def from_dict(self, raw: dict[str, Any] | None) -> None:
        """Restore window state from a persisted dict (best-effort)."""
        if not isinstance(raw, dict):
            return
        self._window.clear()
        rows = raw.get("window") or []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                self._window.append(
                    _TurnFeatures(
                        terseness=float(row.get("terseness", 0.0)),
                        formality=float(row.get("formality", 0.0)),
                        emoji_density=float(row.get("emoji_density", 0.0)),
                        slang_density=float(row.get("slang_density", 0.0)),
                        is_question=float(row.get("is_question", 0.0)),
                        word_count=int(row.get("word_count", 0)),
                    )
                )
            except Exception:
                # Skip malformed rows but keep what we can.
                continue
        self._warmed = bool(raw.get("warmed", False))

    # ── introspection ────────────────────────────────────────────────

    def window_size(self) -> int:
        return len(self._window)

    def is_warmed(self) -> bool:
        return self._warmed

    def recent_word_counts(self) -> list[int]:
        """Return the rolling list of recent user-message word counts.

        Exposes K13's window to other detectors so they don't duplicate
        the rolling buffer (K14 consumes this to z-score per-turn
        length). Returns a copy; mutating it has no effect on the
        analyzer.
        """
        return [int(f.word_count) for f in self._window]

    # ── internals ────────────────────────────────────────────────────

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self._agent_settings, name, default)


def _extract_features(text: str) -> _TurnFeatures:
    """Pure feature extractor; called from :meth:`record_user_turn`."""
    cleaned = (text or "").strip()
    words = cleaned.split()
    word_count = max(1, len(words))

    # Terseness: smooth saturating function of word count. words=4 ->
    # ~0.67; words=8 -> 0.5; words=16 -> ~0.33; words=32 -> ~0.20.
    terseness = 1.0 / (1.0 + word_count / 8.0)

    # Formality: starts with capital + ends with sentence-final
    # punctuation. Half-credit for each.
    starts_capital = False
    if words:
        first_char = words[0][0] if words[0] else ""
        starts_capital = bool(first_char) and first_char.isalpha() and first_char.isupper()
    ends_sentence = bool(_SENT_END_RE.search(cleaned))
    formality = 0.0
    if starts_capital:
        formality += 0.5
    if ends_sentence:
        formality += 0.5

    # Emoji density: emojis-per-word, capped at 1.0.
    emoji_count = len(_EMOJI_RE.findall(cleaned))
    emoji_density = min(1.0, emoji_count / float(word_count))

    # Slang density: closed-list markers per word.
    word_tokens = _WORD_TOKEN_RE.findall(cleaned.lower())
    slang_count = sum(1 for tok in word_tokens if tok in _SLANG_MARKERS)
    slang_density = min(1.0, slang_count / float(word_count))

    # Is-question: 1 when the message ends with '?', tolerating a
    # trailing closing quote / paren.
    tail = cleaned.rstrip().rstrip(")\"'»”] ")
    is_question = 1.0 if tail.endswith("?") else 0.0

    return _TurnFeatures(
        terseness=terseness,
        formality=formality,
        emoji_density=emoji_density,
        slang_density=slang_density,
        is_question=is_question,
        word_count=word_count,
    )


def render_inner_life_block(
    signal: StyleSignal | None,
    labels: list[str] | None = None,
    *,
    user_display_name: str = "Jacob",
) -> str:
    """Render the one-line directive for the prompt.

    Returns ``""`` when ``signal`` is ``None`` (analyzer in warmup) or
    when ``labels`` is empty (every axis sits in the default mid-band
    -- no register cue worth firing).
    """
    if signal is None:
        return ""
    if not labels:
        return ""
    name = (user_display_name or "").strip() or "Jacob"
    return f"How {name} writes lately: " + ", ".join(labels) + "."


class StyleSignalStore:
    """SQLite read / UPSERT for the ``user_style_signal`` table.

    Mirrors the :class:`UserProfileStore` pattern: a tiny adapter
    around ``ChatDatabase`` that round-trips a JSON blob keyed by
    ``user_id``. The blob shape is owned by
    :meth:`StyleSignalAnalyzer.to_dict` / :meth:`StyleSignalAnalyzer.from_dict`
    so we can extend the schema without a column migration.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def load(self, user_id: str) -> dict[str, Any] | None:
        """Return the persisted blob (parsed) or ``None`` when absent."""
        if not user_id:
            return None
        try:
            row = self._db.execute_fetchone(
                "SELECT signal_json FROM user_style_signal WHERE user_id = ?",
                (user_id,),
            )
        except Exception:
            log.debug("style_signal load failed", exc_info=True)
            return None
        if row is None:
            return None
        raw = row[0]
        if not raw:
            return None
        try:
            import json

            data = json.loads(raw)
        except Exception:
            log.debug("style_signal json decode failed", exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def upsert(self, user_id: str, payload: dict[str, Any]) -> None:
        """Replace the per-user blob (UPSERT)."""
        if not user_id or not isinstance(payload, dict):
            return
        try:
            import json
            from datetime import datetime, timezone

            blob = json.dumps(payload, separators=(",", ":"))
            self._db.execute_commit(
                "INSERT INTO user_style_signal (user_id, signal_json, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "signal_json = excluded.signal_json, "
                "updated_at = excluded.updated_at",
                (
                    user_id,
                    blob,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception:
            log.debug("style_signal upsert failed", exc_info=True)


__all__ = [
    "StyleSignal",
    "StyleSignalAnalyzer",
    "StyleSignalStore",
    "render_inner_life_block",
]
