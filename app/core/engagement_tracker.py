"""K14 -- Implicit engagement signals.

Reads two per-turn signals from Jacob's behaviour (reply latency +
message length) and routes them to two consumers depending on which
mode the turn ran in:

  - **voice mode**: both latency and length contribute to a small
    ``closeness_delta`` that's folded into
    :class:`RelationshipAxesUpdater.apply_turn`. Short snappy replies
    nudge closeness up; long silences + curt messages nudge it down.
    The per-turn cap stays well inside the existing ``_MAX_DELTA = 0.08``
    so the reaction / moment / milestone channels still dominate.
  - **typed mode**: latency is NOT consumed as engagement (per Jacob's
    feedback: typing latency is thinking time, not disengagement). Only
    length contributes to ``closeness_delta``. Long typed gaps go to a
    separate consumer instead -- ``absence_seconds`` is set when the
    gap lands in ``[absence_curiosity_min, resume_opener_min_hours)``
    so the next prompt can render a curiosity cue (Aiko notices Jacob
    was away).

The tracker is purely in-process. Latency window lives in an in-memory
``collections.deque`` (voice-only); length is read on demand from the
existing K13 :class:`StyleSignalAnalyzer` window via an injected
provider so we don't duplicate the rolling word-count buffer.

Per-turn cost: a few deque ops + the K13 word-count provider call. No
embedder, no LLM, no SQLite write of its own (the cumulative effect
persists via ``RelationshipAxesState`` already).

See [`docs/personality-backlog/patterns.md`](../../docs/personality-backlog/patterns.md)
"K14. Implicit engagement signals" for the original sketch, and the
project AGENTS.md / config-documentation rule for the agent-settings
contract.
"""
from __future__ import annotations

import collections
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Literal


log = logging.getLogger("app.engagement")


# Module-level defaults so unit tests can instantiate without an
# ``AgentSettings`` stub. Settings overrides take priority via the
# ``_setting`` lookup (mirrors :class:`StyleSignalAnalyzer`).
_DEFAULT_WINDOW = 12
_DEFAULT_WARMUP_MIN = 6
# z-scores at which a single signal is "strong enough" to drive the
# label / contribute the full per-turn cap. Higher = stricter.
_DEFAULT_LATENCY_Z_STRONG_DROP = 1.5
_DEFAULT_LENGTH_Z_STRONG_DROP = -1.0
# Per-turn closeness delta cap. Sits well below the existing
# ``RelationshipAxesUpdater._MAX_DELTA = 0.08`` so the reaction-tag /
# moment-vibe / milestone channels keep dominating; engagement is a
# colouring layer, not the main signal.
_DEFAULT_CLOSENESS_DELTA_MAX = 0.04
# Typed-mode absence-curiosity band. Lower bound is "long enough to
# read as a real absence, not a thoughtful pause"; upper bound is the
# default ``resume_opener_min_hours`` (4h) past which the existing
# resume-opener path takes over.
_DEFAULT_ABSENCE_CURIOSITY_MIN_SECONDS = 1800.0
_DEFAULT_RESUME_OPENER_MIN_HOURS = 4.0

# Floors on the rolling stdev so a tiny dispersion can't amplify
# small absolute deltas into huge z-scores. Tuned for the units they
# guard (seconds for latency, words for length).
_LATENCY_STDEV_FLOOR_S = 0.5
_LENGTH_STDEV_FLOOR_WORDS = 1.0


EngagementLabel = Literal["engaged", "neutral", "disengaged", "abandoned"]


@dataclass(slots=True, frozen=True)
class EngagementResult:
    """Outcome of a single :meth:`EngagementTracker.record_turn` call.

    ``closeness_delta`` lands in :class:`RelationshipAxesUpdater.apply_turn`
    via the new ``engagement_delta`` kwarg; ``label`` drives the
    typed-proactive abandoned-cycle gate; ``absence_seconds`` (Phase 2)
    drives the typed-mode absence-curiosity inner-life cue. The two
    debug passthroughs (``latency_seconds`` / ``length_z``) are for the
    MCP ``get_engagement_state`` tool and the per-turn ``engagement:``
    INFO log line; they have no behavioural side-effects.
    """

    closeness_delta: float
    label: EngagementLabel
    absence_seconds: float | None
    latency_seconds: float | None
    length_z: float | None
    latency_z: float | None
    mode: str
    warmed: bool


class EngagementTracker:
    """Per-turn engagement signal extractor.

    One instance per :class:`SessionController`. ``record_turn`` is
    called from the post-turn pipeline; ``last_result`` is exposed for
    the MCP debug tool. Not thread-safe -- post-turn runs on the turn
    thread, and the prompt assembler reads ``last_result`` on that
    same thread.
    """

    def __init__(
        self,
        *,
        agent_settings: Any | None = None,
        word_count_window_provider: Callable[[], list[int]] | None = None,
    ) -> None:
        """Construct a tracker.

        ``agent_settings`` is the :class:`AgentSettings` dataclass; the
        tracker uses ``getattr`` with module-level defaults so it stays
        testable without a stub. ``word_count_window_provider`` returns
        the rolling list of recent user-message word counts -- typically
        ``[f.word_count for f in style_signal_analyzer._window]`` so the
        tracker doesn't duplicate K13's rolling buffer. Passing ``None``
        disables the length signal; the tracker then runs latency-only
        (voice mode only) which is acceptable but weaker.
        """
        self._agent_settings = agent_settings
        self._word_count_window_provider = word_count_window_provider
        window = max(2, int(self._setting("engagement_window", _DEFAULT_WINDOW)))
        # Voice latency in seconds. Only appended when ``mode == "live"``
        # AND ``latency_seconds is not None``; typed latencies don't
        # participate in the engagement signal at all.
        self._latency_window: collections.deque[float] = collections.deque(
            maxlen=window,
        )
        self._last_result: EngagementResult | None = None

    # ── public API ────────────────────────────────────────────────────

    def record_turn(
        self,
        *,
        mode: str,
        latency_seconds: float | None,
        user_word_count: int,
    ) -> EngagementResult:
        """Score one turn and return the per-turn engagement result.

        ``mode`` is the turn's originating mode (``"live"`` or
        ``"typed"``); ``"live"`` includes latency in the engagement
        delta, ``"typed"`` does not (typed latency routes to
        ``absence_seconds`` instead). ``latency_seconds`` is the gap
        between Aiko's last reply and the current user message; ``None``
        when there's no prior assistant turn yet (cold-start). The
        result is also cached on ``self.last_result`` for MCP debug.
        """
        mode_norm = (mode or "typed").strip().lower() or "typed"

        # Voice-only latency window maintenance.
        latency_z: float | None = None
        if mode_norm == "live" and latency_seconds is not None and latency_seconds >= 0.0:
            # Compute z BEFORE appending so the current sample doesn't
            # bias its own baseline. Order matters here.
            latency_z = self._z_or_none(
                list(self._latency_window),
                value=float(latency_seconds),
                stdev_floor=_LATENCY_STDEV_FLOOR_S,
            )
            self._latency_window.append(float(latency_seconds))

        # Length z-score from the K13 word-count provider. Same "score
        # against the prior window" semantic as latency above.
        length_z: float | None = None
        if self._word_count_window_provider is not None:
            try:
                window = list(self._word_count_window_provider() or [])
            except Exception:
                log.debug("engagement: word_count provider raised", exc_info=True)
                window = []
            # The K13 window includes the *current* turn (post-turn
            # calls ``record_user_turn`` before us in the pipeline). To
            # score against the prior baseline we drop the last entry
            # when it matches the current word count.
            if window and window[-1] == int(user_word_count):
                baseline = window[:-1]
            else:
                baseline = window
            length_z = self._z_or_none(
                baseline,
                value=float(int(user_word_count)),
                stdev_floor=_LENGTH_STDEV_FLOOR_WORDS,
            )

        warmed = self._is_warmed(mode_norm, length_z=length_z, latency_z=latency_z)
        closeness_delta = 0.0
        label: EngagementLabel = "neutral"
        if warmed:
            closeness_delta, label = self._score(
                mode=mode_norm,
                latency_z=latency_z,
                length_z=length_z,
            )

        # Phase 2: typed-mode absence-curiosity band. Voice-mode never
        # populates this -- voice latencies route through latency_z
        # into the engagement delta.
        absence_seconds = self._absence_band(
            mode=mode_norm, latency_seconds=latency_seconds,
        )

        result = EngagementResult(
            closeness_delta=float(closeness_delta),
            label=label,
            absence_seconds=absence_seconds,
            latency_seconds=(
                float(latency_seconds) if latency_seconds is not None else None
            ),
            length_z=(float(length_z) if length_z is not None else None),
            latency_z=(float(latency_z) if latency_z is not None else None),
            mode=mode_norm,
            warmed=bool(warmed),
        )
        self._last_result = result
        return result

    @property
    def last_result(self) -> EngagementResult | None:
        return self._last_result

    def latency_window_snapshot(self) -> list[float]:
        """For MCP debug + tests. Returns a copy."""
        return list(self._latency_window)

    # ── scoring ───────────────────────────────────────────────────────

    def _score(
        self,
        *,
        mode: str,
        latency_z: float | None,
        length_z: float | None,
    ) -> tuple[float, EngagementLabel]:
        """Combine the two signal contributions into ``(delta, label)``.

        Each contribution is normalised against its "strong" threshold
        so ±1 means "the strong threshold was hit exactly." Voice mode
        averages the two contributions (when both are present); typed
        mode uses length only (single signal -- full weight).
        """
        latency_strong = max(
            0.1,
            float(self._setting(
                "engagement_latency_z_strong_drop",
                _DEFAULT_LATENCY_Z_STRONG_DROP,
            )),
        )
        length_strong = float(self._setting(
            "engagement_length_z_strong_drop", _DEFAULT_LENGTH_Z_STRONG_DROP,
        ))
        # Normalise so "negative engagement" comes out negative for both
        # signals. High latency_z = above-average wait = negative;
        # low length_z = below-average words = negative.
        latency_factor: float | None = None
        if latency_z is not None:
            latency_factor = -latency_z / latency_strong
        length_factor: float | None = None
        if length_z is not None:
            length_factor = length_z / max(0.1, abs(length_strong))

        # Voice mode: use whichever subset of signals are present.
        # ``mode == "typed"`` never has latency_z set (we never appended
        # to the window for typed turns) so the latency_factor stays
        # None there even if a stale value were passed.
        active: list[float] = []
        if mode == "live" and latency_factor is not None:
            active.append(latency_factor)
        if length_factor is not None:
            active.append(length_factor)
        if not active:
            return 0.0, "neutral"
        engagement = sum(active) / float(len(active))

        max_delta = max(
            0.0,
            float(self._setting(
                "engagement_closeness_delta_max",
                _DEFAULT_CLOSENESS_DELTA_MAX,
            )),
        )
        closeness_delta = max(-max_delta, min(max_delta, engagement * max_delta))

        label = self._label_for(engagement)
        return closeness_delta, label

    @staticmethod
    def _label_for(engagement: float) -> EngagementLabel:
        """Bucket a normalised engagement scalar into a label."""
        if engagement <= -1.5:
            return "abandoned"
        if engagement <= -0.7:
            return "disengaged"
        if engagement >= 0.7:
            return "engaged"
        return "neutral"

    # ── absence-curiosity (Phase 2) ───────────────────────────────────

    def _absence_band(
        self, *, mode: str, latency_seconds: float | None,
    ) -> float | None:
        """Return ``latency_seconds`` when it lands in the typed-mode
        absence-curiosity band, else ``None``.

        The band is bounded above by ``resume_opener_min_hours``
        (default 4h) so a long-enough gap routes through the existing
        resume-opener path instead of this cue -- no double-firing.
        Voice mode always returns ``None``: voice latency feeds the
        engagement delta directly.
        """
        if mode != "typed":
            return None
        if latency_seconds is None or latency_seconds <= 0.0:
            return None
        if not bool(
            self._setting(
                "engagement_absence_curiosity_enabled", True,
            )
        ):
            return None
        min_seconds = float(self._setting(
            "engagement_absence_curiosity_min_seconds",
            _DEFAULT_ABSENCE_CURIOSITY_MIN_SECONDS,
        ))
        max_seconds = float(self._setting(
            "resume_opener_min_hours", _DEFAULT_RESUME_OPENER_MIN_HOURS,
        )) * 3600.0
        if min_seconds <= 0.0 or max_seconds <= min_seconds:
            return None
        if min_seconds <= latency_seconds < max_seconds:
            return float(latency_seconds)
        return None

    # ── helpers ───────────────────────────────────────────────────────

    def _is_warmed(
        self,
        mode: str,
        *,
        length_z: float | None,
        latency_z: float | None,
    ) -> bool:
        """Whether the tracker has enough history to score this turn.

        Warmup is per-signal: typed-mode needs ``length_z`` to exist
        (the only signal available there); voice-mode needs *at least
        one* of ``length_z`` / ``latency_z`` to exist (so the tracker
        can start scoring even before the latency window has filled
        if the K13 length window is already warm).
        """
        warmup = max(
            2,
            int(self._setting("engagement_warmup_min", _DEFAULT_WARMUP_MIN)),
        )
        # The z helper returns ``None`` when the baseline is shorter
        # than ``warmup``, so a non-None z implies the baseline crossed
        # warmup. Either signal being warm is enough.
        if mode == "typed":
            return length_z is not None
        return length_z is not None or latency_z is not None

    @staticmethod
    def _z_or_none(
        baseline: list[float], *, value: float, stdev_floor: float,
    ) -> float | None:
        """Return ``(value - mean) / max(stdev, floor)`` or ``None`` when
        the baseline is too small to score reliably.

        ``baseline`` must NOT include ``value`` -- callers slice it off
        first when needed. Warmup floor is hard-coded to match
        :data:`_DEFAULT_WARMUP_MIN`; the per-tracker setting is read
        elsewhere.
        """
        if len(baseline) < _DEFAULT_WARMUP_MIN:
            return None
        n = float(len(baseline))
        mean = sum(baseline) / n
        # Population stdev (we're treating ``baseline`` as the whole
        # observed history, not a sample).
        var = sum((x - mean) ** 2 for x in baseline) / n
        stdev = math.sqrt(max(0.0, var))
        denom = max(float(stdev_floor), stdev)
        return (value - mean) / denom

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self._agent_settings, name, default)


__all__ = [
    "EngagementLabel",
    "EngagementResult",
    "EngagementTracker",
]
