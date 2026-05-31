"""Per-turn novelty detector (K6 personality backlog).

Compares the incoming user-turn embedding against a rolling centroid
of the last N user-message vectors and emits a banded inner-life
signal so Aiko can react with genuine surprise on out-of-baseline
turns instead of blank acceptance.

Design choices:

- **Cross-session per-user**. The ring buffer is warmed lazily from
  :class:`app.core.rag.rag_store.RagStore` on the first ``detect()`` call
  (filtered to ``role='user'`` rows whose ``session_id`` starts with
  the current user prefix) so a topic genuinely discussed yesterday
  won't re-fire ``strong_novelty`` today.
- **Banded output**, not a single threshold. ``mild_shift`` covers
  small topic pivots; ``strong_novelty`` is the "oh -- that's a new
  one" beat. Each gets distinct copy in the inner-life provider.
- **Cooldown between hits**. The signal fires at most every
  ``novelty_cooldown_turns + 1`` turns so Aiko doesn't pile "you keep
  saying surprising things" beats on top of each other.
- **Always-append**. Even when below threshold, we push the current
  vector into the ring so the centroid keeps moving with the
  conversation. A novel turn becomes part of the baseline going
  forward; we don't lock it out.

The detector is constructed on :class:`SessionController` start-up
(when ``agent.novelty_detection_enabled``) and registered as the
``novelty`` inner-life provider on the prompt assembler. It is
called per-turn from the assembler's ``assemble_with_budget``,
mirroring how F2's knowledge-gap provider receives the current
``user_text`` (unlike the post-turn-stash ``belief_gaps`` provider).
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np


log = logging.getLogger("app.novelty_detector")


# Module-level defaults so tests can instantiate without a settings
# stub. ``SessionController`` passes ``MemorySettings`` in production
# and the detector reads the configured values via ``getattr``.
_DEFAULT_WINDOW = 12
_DEFAULT_WARMUP_MIN = 3
_DEFAULT_MILD_THRESHOLD = 0.35
_DEFAULT_STRONG_THRESHOLD = 0.55
_DEFAULT_COOLDOWN_TURNS = 2
# Mirror ``MessageIndexer._MIN_INDEX_LENGTH`` so a one-word
# "ok"/"yep"/"sure" turn never trips a band -- those are reactions,
# not topic shifts, even when lexically distant from the centroid.
_MIN_TEXT_LENGTH = 8


BAND_MILD = "mild_shift"
BAND_STRONG = "strong_novelty"


@dataclass(slots=True, frozen=True)
class NoveltyResult:
    """One banded novelty signal the inner-life provider may render.

    ``distance`` is ``1.0 - cosine(vec, centroid)`` and lives in
    ``[0.0, 2.0]`` in theory (vectors are unit-norm, so practical
    values cluster in ``[0, 1.2]``). ``mean_similarity`` is the
    cosine itself, kept for log/debug readability. ``window_size``
    is the count of vectors used to compute the centroid for this
    classification (useful when validating warmup behaviour).
    """

    distance: float
    band: str
    window_size: int
    mean_similarity: float


def _normalize(vec: np.ndarray) -> np.ndarray:
    """Return a unit-norm copy of ``vec`` (or zero-vec when degenerate).

    :class:`Embedder` already returns unit-norm vectors, but the
    centroid (mean of unit vectors) has magnitude ``< 1`` in general,
    so we re-normalize it before the dot product.
    """
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return arr
    return arr / norm


class NoveltyDetector:
    """Compute a banded novelty signal for each incoming user turn.

    Owns an in-memory ring buffer of unit-norm vectors warmed from
    Lance once per session lifetime. Not thread-safe by itself; the
    caller (``PromptAssembler``) is expected to invoke ``detect`` on
    the turn thread.
    """

    def __init__(
        self,
        *,
        embedder: Any,
        rag_store: Any | None,
        user_id: str,
        memory_settings: Any | None = None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._embedder = embedder
        self._rag_store = rag_store
        self._user_id = (user_id or "").strip()
        self._memory_settings = memory_settings
        self._clock = clock  # unused today; kept for symmetry w/ other detectors
        window = max(2, int(self._setting("novelty_window", _DEFAULT_WINDOW)))
        self._ring: collections.deque[np.ndarray] = collections.deque(
            maxlen=window,
        )
        self._warmed = False
        self._cooldown_remaining = 0
        # K18 (topic stagnation) consumes these per-turn signals:
        # ``last_distance`` is the cosine distance the most recent
        # ``detect()`` call computed against the live centroid (None
        # when we couldn't measure -- short text, warmup, embed
        # failure). ``last_band`` is the banded outcome we returned
        # from that call (None when below the mild threshold or
        # suppressed). Both are reset at the top of every ``detect``
        # so a stale value never leaks across turns.
        self.last_distance: float | None = None
        self.last_band: str | None = None

    # ── public API ───────────────────────────────────────────────────

    def detect(self, user_text: str) -> NoveltyResult | None:
        """Score ``user_text`` against the rolling centroid.

        Returns a :class:`NoveltyResult` when the distance crosses one
        of the configured bands, ``None`` otherwise (silent turn,
        warmup, cooldown, or short input). Always appends the
        embedded vector to the ring on a non-silent call so the
        centroid evolves with the conversation.
        """
        # K18 hooks: clear last-turn signals up front so a caller
        # reading them after a warmup/short-text turn sees a clean
        # ``None`` rather than a stale value from earlier in the
        # session.
        self.last_distance = None
        self.last_band = None
        text = (user_text or "").strip()
        if len(text) < _MIN_TEXT_LENGTH:
            log.debug(
                "novelty-detector: skip (short text len=%d)",
                len(text),
            )
            return None

        self._warm_if_needed()

        warmup = max(2, int(self._setting("novelty_warmup_min", _DEFAULT_WARMUP_MIN)))
        if len(self._ring) < warmup:
            # Embed + remember so the ring fills, but don't emit a
            # signal until we have a real baseline to compare to.
            vec = self._embed(text)
            if vec is not None:
                self._ring.append(vec)
            log.debug(
                "novelty-detector: cold-start (ring=%d need=%d)",
                len(self._ring),
                warmup,
            )
            return None

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            # Still append so the baseline keeps moving even while we
            # suppress the signal. We *do* compute the distance here
            # so K18 can see how close the conversation is to the
            # centroid even on a suppressed novelty turn.
            vec = self._embed(text)
            if vec is not None:
                # Distance off the centroid the *prior* ring saw, so
                # we mirror the normal-path order: compute then
                # append.
                centroid_pre = _normalize(
                    np.mean(np.stack(list(self._ring)), axis=0)
                )
                similarity_pre = float(np.dot(vec, centroid_pre))
                self.last_distance = max(0.0, 1.0 - similarity_pre)
                self._ring.append(vec)
            log.debug(
                "novelty-detector: cooldown remaining=%d distance=%s",
                self._cooldown_remaining,
                f"{self.last_distance:.3f}"
                if self.last_distance is not None
                else "n/a",
            )
            return None

        vec = self._embed(text)
        if vec is None:
            return None

        centroid = _normalize(np.mean(np.stack(list(self._ring)), axis=0))
        similarity = float(np.dot(vec, centroid))
        # Cosine of unit vectors is in [-1, 1]; clamp distance to a
        # well-behaved positive band for downstream consumers.
        distance = max(0.0, 1.0 - similarity)
        window_size = len(self._ring)
        # Append after computing so the current turn doesn't bias its
        # own centroid -- the ring represents *prior* turns.
        self._ring.append(vec)
        # Surface the measurement to K18 even when we end up below
        # the mild band -- the stagnation detector needs every
        # measured distance to track "we've been close to centroid".
        self.last_distance = distance

        mild = float(self._setting("novelty_mild_threshold", _DEFAULT_MILD_THRESHOLD))
        strong = float(
            self._setting("novelty_strong_threshold", _DEFAULT_STRONG_THRESHOLD)
        )
        # Defensive ordering: if a misconfigured strong<=mild slipped
        # through, just bail to a single-threshold behaviour.
        if strong < mild:
            strong = mild

        band: str | None
        if distance >= strong:
            band = BAND_STRONG
        elif distance >= mild:
            band = BAND_MILD
        else:
            band = None

        log.info(
            "novelty-detector: distance=%.3f band=%s window=%d user=%s",
            distance,
            band or "silent",
            window_size,
            self._user_id or "(none)",
        )

        if band is None:
            return None

        cooldown = max(
            0, int(self._setting("novelty_cooldown_turns", _DEFAULT_COOLDOWN_TURNS))
        )
        self._cooldown_remaining = cooldown
        # Surface the band so K18's post-novelty suppression can fire
        # without having to re-derive it from the returned result.
        self.last_band = band
        return NoveltyResult(
            distance=distance,
            band=band,
            window_size=window_size,
            mean_similarity=similarity,
        )

    # ── internals ────────────────────────────────────────────────────

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self._memory_settings, name, default)

    def _embed(self, text: str) -> np.ndarray | None:
        if self._embedder is None:
            return None
        try:
            vec = self._embedder.embed(text)
        except Exception:
            log.debug("novelty-detector: embed failed", exc_info=True)
            return None
        if vec is None:
            return None
        return _normalize(vec)

    def _warm_if_needed(self) -> None:
        if self._warmed:
            return
        self._warmed = True  # set first so a failure doesn't re-try every turn
        store = self._rag_store
        if store is None:
            return
        limit = max(2, int(self._setting("novelty_window", _DEFAULT_WINDOW)))
        try:
            vectors = store.list_recent_user_vectors(
                user_id_prefix=self._user_id,
                limit=limit,
            )
        except Exception:
            log.debug(
                "novelty-detector: warm from rag_store failed",
                exc_info=True,
            )
            return
        if not vectors:
            log.debug(
                "novelty-detector: warm-up empty (user=%s)",
                self._user_id or "(none)",
            )
            return
        # ``list_recent_user_vectors`` returns most-recent first; the
        # ring's centroid math is commutative so order doesn't matter
        # for the score, but we push oldest-first to make eviction
        # behaviour intuitive in tests.
        for v in reversed(vectors):
            arr = np.asarray(v, dtype=np.float32)
            if arr.size == 0:
                continue
            self._ring.append(_normalize(arr))
        log.info(
            "novelty-detector: warmed ring=%d user=%s",
            len(self._ring),
            self._user_id or "(none)",
        )


def render_inner_life_block(result: NoveltyResult | None) -> str:
    """Render the one-line inner-life signal for the given band.

    Two bands, two copies. ``mild_shift`` nudges Aiko to acknowledge
    a small topic pivot; ``strong_novelty`` asks for real curiosity.
    Returns ``""`` when ``result`` is ``None`` so the assembler can
    drop the block entirely.
    """
    if result is None:
        return ""
    if result.band == BAND_STRONG:
        return (
            "Heads-up: Jacob just brought up something well outside the "
            "recent baseline -- react with real curiosity, not a flat "
            "acknowledgement."
        )
    if result.band == BAND_MILD:
        return (
            "Heads-up: Jacob just nudged the topic sideways from what "
            "you've been on -- small pivot, not a hard reset."
        )
    return ""
