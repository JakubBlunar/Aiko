"""K13 -- Stylometric mirror analyzer.

Tracks how Jacob is writing *right now* against how he usually writes,
and emits a one-line directive so Aiko's register follows him. Sibling
shape to the K6/K18 detectors and the AikoStylePatternTracker (anti-rut)
-- pure rolling-window analyzer with no embedder, no LLM. Five axes:

  - terseness   -- ``1.0 / (1.0 + words / 8.0)`` per turn (high = terse)
  - punctuation -- starts capital + ends with sentence-final punct
  - playfulness -- turn contains an emoji **or** an ASCII emoticon
  - slang       -- turn contains a closed-list casual marker
  - question    -- turn ends with ``?``

**This block asks "than usual", not "is he".** That is the whole design,
and it is a repair: the original bucketed each axis against an absolute
threshold, which on a stable writer can only ever produce a constant.
Measured over 2018 real user turns it rendered on 99.7% of them, said
one of three things, and changed **four times in twelve weeks** -- 98.6%
of turns got the identical sentence "How Jacob writes lately: chatty,
formal." Three of the five axes had never emitted a label in the corpus
and could not: emoji peaked at 0.000 (he writes ``:D``, not U+1F604),
slang peaked at 0.009 against a 0.15 bar because it was measured per
*word*, and the question rate topped out at 0.333 against a 0.40 bar.
The two live axes were pinned -- window-mean formality never once fell
below its own threshold. See ``docs/personality-backlog/health.md`` H21.

So each axis is now scored as a **deviation from his own baseline**: the
window mean is compared against an exponentially-weighted long-run mean
and variance, and a label is emitted only when the two disagree by
``style_signal_sensitivity`` standard errors. Three consequences worth
keeping in mind if you touch this:

- **It self-calibrates.** There is one sensitivity knob instead of five
  hand-tuned per-axis bars, and it means the same thing for a terse
  user and a verbose one. A bar tuned to this corpus would have been
  wrong for anybody else's.
- **Silence is the default and it is informative.** A writer who is
  writing normally produces no block at all, which is what the original
  docstring claimed and never delivered.
- **The yardstick is the standard error, not the standard deviation.**
  The tested quantity is a mean of ``window`` samples. Comparing it
  against the per-turn spread is why the first cut of this fired on
  0.0% of turns -- on a binary axis the per-turn spread is ~0.5 and no
  window mean is three of those from anything.

The analyzer is constructed in :class:`SessionController` start-up
(when ``agent.style_signal_enabled``), warmed lazily on first call
from past user messages, fed by the post-turn mixin, persisted via a
tiny ``user_style_signal`` SQLite table, and surfaced as the
``style_signal`` inner-life provider on the prompt assembler.
"""
from __future__ import annotations

import collections
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from app.core.infra import timephrase


log = logging.getLogger("app.style_signal")


# Module-level defaults so tests can construct without a settings stub.
_DEFAULT_WINDOW = 30
_DEFAULT_WARMUP_MIN = 8

#: How many standard errors the recent window must sit from the baseline
#: before an axis says anything. Chosen by replaying the full 2018-turn
#: corpus: 3.0 speaks on ~28% of turns with 15 distinct label sets and
#: changes what it says 98 times, against the old code's 99.7% / 3 / 4.
#: Overlapping windows make consecutive turns heavily autocorrelated, so
#: the effective sample size is below ``window`` and this reads stricter
#: than a textbook 3-sigma would suggest -- do not lower it on the
#: theory that 3.0 is conservative.
_DEFAULT_SENSITIVITY = 3.0

#: Baseline decay. ``1/300`` puts the half-life around 208 turns, which
#: on this corpus (~25 user turns/day) makes "usual" mean roughly the
#: last week and a half -- long enough that a single evening cannot
#: become the norm, short enough that a genuine change in how he writes
#: eventually does.
_DEFAULT_BASELINE_ALPHA = 1.0 / 300.0

#: Turns of baseline required before any axis may speak. Below this the
#: variance estimate is too rough to call anything unusual.
_DEFAULT_BASELINE_MIN_TURNS = 60

#: Floor under the baseline standard deviation. Without it an axis that
#: has been perfectly constant divides by ~0 and reports the first
#: deviation as infinitely surprising.
_DEFAULT_SD_FLOOR = 0.05

#: The line is a register nudge, not a report. Two labels is the most
#: that reads as a sentence; the corpus produces up to five at once.
_DEFAULT_MAX_LABELS = 2

#: Bumped whenever the persisted blob's meaning changes. A blob from an
#: older build holds per-word densities under keys this build reads as
#: incidence rates, which would poison the baseline with values that
#: cannot recur -- so a mismatch is discarded and the analyzer re-warms
#: from chat history instead.
_STATE_VERSION = 2

#: Axis order is fixed so the rendered line reads naturally.
_AXES: tuple[str, ...] = (
    "terseness", "punctuation", "playfulness", "slang", "question",
)

#: ``(above-baseline, below-baseline)`` phrasing per axis. Both
#: directions are real information -- him going quiet is as much a
#: register change as him going playful.
_AXIS_LABELS: dict[str, tuple[str, str]] = {
    "terseness": ("terser than usual", "more long-form than usual"),
    "punctuation": (
        "more buttoned-up than usual", "looser punctuation than usual",
    ),
    "playfulness": (
        "more playful markers than usual", "drier than usual",
    ),
    "slang": ("more casual than usual", "more measured than usual"),
    "question": (
        "asking back more than usual", "asking back less than usual",
    ),
}


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
# count their components separately) but the axis is per-turn
# incidence, so a miscount inside one turn changes nothing.
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


# ASCII emoticons. Without these the playfulness axis was blind: zero of
# 2018 user turns contained a Unicode emoji and 47.8% contained an
# emoticon, so the axis measured nothing and could not have measured
# anything. The eye character is restricted to ``: ; =`` and must not be
# preceded by an alphanumeric, which keeps "12:30", "C:\\src", "http://"
# and "note: (later)" out; ``x``/``X`` eyes are excluded entirely
# because "(x)" is ordinary prose and "xD" is covered explicitly.
_EMOTICON_RE = re.compile(
    r"(?<![A-Za-z0-9])[:;=]-?[)(DPp3Oo/\\|*](?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:xD|XD|uwu|UwU|owo|OwO)(?![A-Za-z0-9])"
    r"|\^_\^|>_<|o_O|O_o|T_T|;_;|<3"
)


# Sentence-final punctuation, allowing a trailing closing quote /
# paren / bracket so "Hello.\"" still counts as ending in `.`.
_SENT_END_RE = re.compile(r"[.!?][\"'»”\)\]]?\s*$")
_WORD_TOKEN_RE = re.compile(r"[a-z']+")


@dataclass(slots=True, frozen=True)
class _TurnFeatures:
    """One recorded user turn's normalised features (each in [0,1]).

    ``playfulness``, ``slang`` and ``question`` are per-turn *incidence*
    (0.0 or 1.0), so their window mean is "share of recent turns that
    had one". The two that used to be per-word densities were unusable
    that way -- a 15%-of-all-words slang bar is not reachable by prose.
    """

    terseness: float
    punctuation: float
    playfulness: float
    slang: float
    question: float
    word_count: int

    def get(self, axis: str) -> float:
        return float(getattr(self, axis))


@dataclass(slots=True)
class _AxisBaseline:
    """Exponentially-weighted mean and variance for one axis.

    O(1) in both time and stored bytes, which is what lets the baseline
    be persisted on every turn alongside the window without turning the
    per-turn UPSERT into a kilobyte of JSON.
    """

    mean: float = 0.0
    var: float = 0.0

    def update(self, value: float, alpha: float, count: int) -> None:
        """Fold in one observation. ``count`` is 1-based.

        The effective rate is ``max(alpha, 1/count)``, which makes this
        an exact running mean until ``count`` reaches ``1/alpha`` and an
        EWMA after. Without it a baseline starting at 0.0 converges far
        too slowly to be usable: at ``alpha = 1/300`` it reaches only
        49% of the true mean after 200 turns, so every axis would read
        "higher than usual" for the first several months -- exactly the
        always-on constant this rewrite exists to remove.
        """
        rate = max(alpha, 1.0 / max(1, count))
        delta = value - self.mean
        self.mean += rate * delta
        # West's incremental EW variance: the (1 - rate) factor keeps
        # this an estimate of the *weighted* spread rather than letting
        # it grow without bound.
        self.var = (1.0 - rate) * (self.var + rate * delta * delta)

    def stdev(self, floor: float) -> float:
        return max(math.sqrt(max(0.0, self.var)), floor)


@dataclass(slots=True, frozen=True)
class StyleSignal:
    """Recent window means plus how far each sits from his baseline.

    ``deviations`` is the signed distance in baseline standard errors,
    per axis, and is what :meth:`labels` buckets. It is empty while the
    baseline is still warming, which is the honest way to say "no idea
    yet" -- the means are still populated so the MCP debug view and the
    L23 communication-style digest can show them.
    """

    terseness: float
    punctuation: float
    playfulness: float
    slang: float
    question: float
    window_size: int
    baseline_turns: int = 0
    deviations: dict[str, float] = field(default_factory=dict)

    def labels(
        self,
        *,
        sensitivity: float = _DEFAULT_SENSITIVITY,
        max_labels: int = _DEFAULT_MAX_LABELS,
    ) -> list[str]:
        """Name the axes that have moved, strongest first.

        Returns ``[]`` when he is writing the way he normally writes,
        which is the common case and the point: an unremarkable turn
        should cost nothing and say nothing.
        """
        scored: list[tuple[float, str]] = []
        for axis in _AXES:
            z = float(self.deviations.get(axis, 0.0))
            if z >= sensitivity:
                scored.append((abs(z), _AXIS_LABELS[axis][0]))
            elif z <= -sensitivity:
                scored.append((abs(z), _AXIS_LABELS[axis][1]))
        if not scored:
            return []
        # Strongest deviation first, then axis order for a stable tie
        # break so the same state always renders the same string.
        order = {label: i for i, label in enumerate(
            lbl for axis in _AXES for lbl in _AXIS_LABELS[axis]
        )}
        scored.sort(key=lambda pair: (-pair[0], order[pair[1]]))
        return [label for _z, label in scored[:max(1, int(max_labels))]]


class StyleSignalAnalyzer:
    """Track how Jacob writes lately against how he usually writes.

    Owns a small ring of per-turn features plus five O(1) baselines (no
    vectors, no LLM). Per-turn cost is a few regex scans, a deque
    append and five float updates; ``current_signal`` is a one-pass mean
    over the window. Not thread-safe; the post-turn pipeline calls
    :meth:`record_user_turn` on the turn thread and the assembler calls
    :meth:`current_signal` on that same thread.
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
        self._baselines: dict[str, _AxisBaseline] = {
            axis: _AxisBaseline() for axis in _AXES
        }
        self._baseline_turns = 0
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
        self._fold_into_baseline(features)

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
            "style-signal: warmed from history; window=%d baseline=%d",
            len(self._window),
            self._baseline_turns,
        )

    def current_signal(self) -> StyleSignal | None:
        """Return the snapshot, or ``None`` while the window is warming."""
        warmup = max(
            2,
            int(
                self._setting("style_signal_warmup_min", _DEFAULT_WARMUP_MIN)
            ),
        )
        if len(self._window) < warmup:
            return None
        n = float(len(self._window))
        means = {
            axis: sum(f.get(axis) for f in self._window) / n
            for axis in _AXES
        }
        return StyleSignal(
            window_size=int(n),
            baseline_turns=int(self._baseline_turns),
            deviations=self._deviations(means, n),
            **means,
        )

    def labels_for_signal(self, signal: StyleSignal) -> list[str]:
        """Apply the configured sensitivity to a signal."""
        return signal.labels(
            sensitivity=float(
                self._setting(
                    "style_signal_sensitivity", _DEFAULT_SENSITIVITY,
                )
            ),
            max_labels=_DEFAULT_MAX_LABELS,
        )

    # ── persistence (JSON round-trip) ─────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "warmed": bool(self._warmed),
            "baseline_turns": int(self._baseline_turns),
            "baselines": {
                axis: {
                    "mean": float(b.mean),
                    "var": float(b.var),
                }
                for axis, b in self._baselines.items()
            },
            "window": [
                {
                    "terseness": float(f.terseness),
                    "punctuation": float(f.punctuation),
                    "playfulness": float(f.playfulness),
                    "slang": float(f.slang),
                    "question": float(f.question),
                    "word_count": int(f.word_count),
                }
                for f in self._window
            ],
        }

    def from_dict(self, raw: dict[str, Any] | None) -> None:
        """Restore state from a persisted dict (best-effort).

        A blob from a build with different axis semantics is dropped
        whole rather than partially read: leaving ``_warmed`` false
        makes the post-turn path re-warm from chat history, which is a
        single scan and produces a baseline that means what this build
        thinks it means.
        """
        if not isinstance(raw, dict):
            return
        if int(raw.get("version") or 0) != _STATE_VERSION:
            log.info(
                "style-signal: discarding state v%s (want v%d); will re-warm",
                raw.get("version"),
                _STATE_VERSION,
            )
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
                        punctuation=float(row.get("punctuation", 0.0)),
                        playfulness=float(row.get("playfulness", 0.0)),
                        slang=float(row.get("slang", 0.0)),
                        question=float(row.get("question", 0.0)),
                        word_count=int(row.get("word_count", 0)),
                    )
                )
            except Exception:
                # Skip malformed rows but keep what we can.
                continue
        stored = raw.get("baselines")
        if isinstance(stored, dict):
            for axis in _AXES:
                entry = stored.get(axis)
                if not isinstance(entry, dict):
                    continue
                try:
                    self._baselines[axis] = _AxisBaseline(
                        mean=float(entry.get("mean", 0.0)),
                        var=float(entry.get("var", 0.0)),
                    )
                except Exception:
                    continue
        try:
            self._baseline_turns = max(0, int(raw.get("baseline_turns") or 0))
        except Exception:
            self._baseline_turns = 0
        self._warmed = bool(raw.get("warmed", False))

    # ── introspection ────────────────────────────────────────────────

    def window_size(self) -> int:
        return len(self._window)

    def baseline_turns(self) -> int:
        return int(self._baseline_turns)

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

    def _fold_into_baseline(self, features: _TurnFeatures) -> None:
        alpha = float(
            self._setting(
                "style_signal_baseline_alpha", _DEFAULT_BASELINE_ALPHA,
            )
        )
        alpha = min(1.0, max(1e-6, alpha))
        self._baseline_turns += 1
        for axis in _AXES:
            self._baselines[axis].update(
                features.get(axis), alpha, self._baseline_turns,
            )

    def _deviations(
        self, means: dict[str, float], window_n: float,
    ) -> dict[str, float]:
        """Signed distance from baseline, in standard errors.

        The tested quantity is a mean of ``window_n`` samples, so the
        yardstick is the standard error ``sd / sqrt(n)`` and not the
        per-turn ``sd``. Using the latter is why the first cut of this
        never fired: on a binary axis the per-turn spread is ~0.5, and
        no window mean is three of those from anything.
        """
        if self._baseline_turns < _DEFAULT_BASELINE_MIN_TURNS:
            return {}
        out: dict[str, float] = {}
        for axis in _AXES:
            base = self._baselines[axis]
            se = base.stdev(_DEFAULT_SD_FLOOR) / math.sqrt(max(1.0, window_n))
            if se <= 0.0:
                continue
            out[axis] = (means[axis] - base.mean) / se
        return out

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

    # Punctuation: starts with capital + ends with sentence-final
    # punctuation. Half-credit for each. Named for what it measures --
    # it was called "formality" and reported as register, which had
    # Aiko told that "Aww :3 gladly. I am sitting next to you." was
    # formal writing on every turn for three months.
    starts_capital = False
    if words:
        first_char = words[0][0] if words[0] else ""
        starts_capital = bool(first_char) and first_char.isalpha() and first_char.isupper()
    ends_sentence = bool(_SENT_END_RE.search(cleaned))
    punctuation = 0.0
    if starts_capital:
        punctuation += 0.5
    if ends_sentence:
        punctuation += 0.5

    # Playfulness / slang / question: per-turn incidence, not density.
    playfulness = 1.0 if (
        _EMOJI_RE.search(cleaned) or _EMOTICON_RE.search(cleaned)
    ) else 0.0

    word_tokens = _WORD_TOKEN_RE.findall(cleaned.lower())
    slang = 1.0 if any(tok in _SLANG_MARKERS for tok in word_tokens) else 0.0

    # Tolerate a trailing closing quote / paren after the '?'.
    tail = cleaned.rstrip().rstrip(")\"'»”] ")
    question = 1.0 if tail.endswith("?") else 0.0

    return _TurnFeatures(
        terseness=terseness,
        punctuation=punctuation,
        playfulness=playfulness,
        slang=slang,
        question=question,
        word_count=word_count,
    )


def render_inner_life_block(
    signal: StyleSignal | None,
    labels: list[str] | None = None,
    *,
    user_display_name: str = "Jacob",
) -> str:
    """Render the one-line directive for the prompt.

    Returns ``""`` when ``signal`` is ``None`` (window still warming) or
    when ``labels`` is empty -- he is writing the way he usually writes,
    and there is nothing to say about that.
    """
    if signal is None:
        return ""
    if not labels:
        return ""
    name = (user_display_name or "").strip() or "Jacob"
    return f"How {name} is writing today: " + ", ".join(labels) + "."


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
                    timephrase.utcnow().isoformat(),
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
