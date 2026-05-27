"""Recurring-phrase miner (Phase 2c — "Aiko human-like upgrades").

Walks the last ~50 messages and looks for short n-grams (3-7 words)
that recur ≥ N times across BOTH user and assistant turns. The reasoning:

  * Things both people say back to each other become inside jokes.
  * Short repeated phrases on only one side are usually filler ("you
    know", "right?") and shouldn't be promoted.
  * Long phrases that recur exactly tend to be quotes / songs / errors;
    we cap at 7 words to keep the registry on the "verbal handshake"
    end of the spectrum.

The miner is **offline** (runs on the SpeakingWindowScheduler at low
priority). It writes durable :class:`Memory` rows of ``kind="catchphrase"``
which the prompt assembler surfaces via the ``catchphrase`` provider as a
"Aiko's running jokes with Jacob:" block.

Throttling: at most one mining pass per ``min_seconds_between`` (default
600 s — frequent enough to catch a new joke landing, rare enough not to
hammer the embedder). A second guard ``min_new_user_turns`` skips the
pass when there's been less than N new user turns since the last run.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase, MessageRow
    from app.core.memory_store import MemoryStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.catchphrase_miner")


# Tokens we don't want to count as content. Includes ultra-common
# function words and the same filler-noise stoplist the plan calls for.
_STOPWORDS = frozenset(
    {
        "i", "you", "the", "a", "an", "and", "or", "but", "so", "to", "of",
        "in", "on", "at", "is", "are", "was", "were", "be", "been", "being",
        "it", "its", "this", "that", "these", "those", "they", "them",
        "we", "us", "our", "your", "my", "me", "him", "her", "his", "she",
        "he", "with", "for", "from", "by", "as", "if", "than", "then",
        "yes", "no", "ok", "okay", "yeah", "yep", "right", "well", "uh",
        "um", "hmm", "huh", "lol", "haha",
    }
)

# Leading /trailing punctuation we strip when slicing candidate ngrams
# back out of a normalised sentence.
_PUNCT_RE = re.compile(r"[^\w\s'-]+", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True, frozen=True)
class CatchphraseCandidate:
    """A surviving n-gram with the data needed for a memory write."""

    phrase: str
    count: int
    user_count: int
    assistant_count: int


def _normalise_text(text: str) -> str:
    """Lowercase, strip non-word chars, collapse whitespace.

    We keep apostrophes and hyphens because contractions ("you're",
    "what's") and compound words ("game-changer") often *are* the
    catchphrase. Numbers stay too — "level 27" can be an inside joke.
    """
    if not text:
        return ""
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _ngrams(tokens: list[str], n: int) -> Iterable[tuple[str, ...]]:
    if n <= 0 or len(tokens) < n:
        return
    for i in range(len(tokens) - n + 1):
        yield tuple(tokens[i : i + n])


def _ngram_is_meaningful(ngram: tuple[str, ...]) -> bool:
    """Reject n-grams that are just stoplist or single-character tokens."""
    if any(len(t) < 2 for t in ngram):
        return False
    non_stop = [t for t in ngram if t not in _STOPWORDS]
    # Require at least 2 content words AND at least 1/3 of the n-gram
    # being non-stop. Tunable; this filters "you know what" but keeps
    # "fish-shaped cookie" and "time to debug".
    if len(non_stop) < max(2, len(ngram) // 3):
        return False
    return True


def _harvest_candidates(
    messages: list["MessageRow"],
    *,
    min_n: int = 3,
    max_n: int = 7,
    min_total_count: int = 3,
    require_both_sides: bool = True,
) -> list[CatchphraseCandidate]:
    """Roll the n-gram counter over user + assistant turns and keep
    those that recur often enough on both sides."""
    user_counts: Counter[tuple[str, ...]] = Counter()
    assistant_counts: Counter[tuple[str, ...]] = Counter()
    for row in messages:
        role = (row.role or "").lower()
        if role not in ("user", "assistant"):
            continue
        norm = _normalise_text(row.content or "")
        if not norm:
            continue
        tokens = norm.split()
        seen_in_msg: set[tuple[str, ...]] = set()
        for n in range(min_n, max_n + 1):
            for ng in _ngrams(tokens, n):
                if ng in seen_in_msg:
                    continue
                if not _ngram_is_meaningful(ng):
                    continue
                seen_in_msg.add(ng)
                if role == "user":
                    user_counts[ng] += 1
                else:
                    assistant_counts[ng] += 1
    out: list[CatchphraseCandidate] = []
    seen_ngrams = set(user_counts) | set(assistant_counts)
    for ng in seen_ngrams:
        u = user_counts[ng]
        a = assistant_counts[ng]
        total = u + a
        if total < min_total_count:
            continue
        if require_both_sides and (u == 0 or a == 0):
            continue
        out.append(
            CatchphraseCandidate(
                phrase=" ".join(ng),
                count=int(total),
                user_count=int(u),
                assistant_count=int(a),
            )
        )
    # Prefer phrases that both sides use roughly equally. The score
    # below rewards high total count and balanced usage.
    out.sort(
        key=lambda c: (
            -c.count,
            -min(c.user_count, c.assistant_count),
            c.phrase,
        )
    )
    return out


def _is_subsumed(longer: str, existing: list[str]) -> bool:
    """If a shorter version of the candidate is already promoted,
    skip the longer one. This keeps the registry to the natural
    canonical form."""
    for already in existing:
        if longer == already:
            return True
        if already in longer:
            return True
    return False


class CatchphraseMiner:
    """Speaking-window job that mines and persists recurring phrases."""

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        memory_store: "MemoryStore | None",
        embedder: "Embedder | None",
        history_window: int = 50,
        min_n: int = 3,
        max_n: int = 7,
        min_total_count: int = 3,
        require_both_sides: bool = True,
        max_writes_per_run: int = 3,
        min_seconds_between: float = 600.0,
        min_new_user_turns: int = 6,
        salience: float = 0.55,
    ) -> None:
        self._db = chat_db
        self._memory = memory_store
        self._embedder = embedder
        self._history_window = max(8, int(history_window))
        self._min_n = max(2, int(min_n))
        self._max_n = max(self._min_n, int(max_n))
        self._min_total_count = max(2, int(min_total_count))
        self._require_both_sides = bool(require_both_sides)
        self._max_writes = max(1, int(max_writes_per_run))
        self._min_seconds_between = max(0.0, float(min_seconds_between))
        self._min_new_user_turns = max(1, int(min_new_user_turns))
        self._salience = max(0.0, min(1.0, float(salience)))
        self._last_run_at = 0.0
        self._last_run_user_count = 0
        self._stats = {
            "scheduled": 0,
            "skipped_throttled": 0,
            "skipped_disabled": 0,
            "skipped_no_candidates": 0,
            "completed": 0,
            "failed": 0,
            "candidates_seen": 0,
            "memories_written": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ── public ──────────────────────────────────────────────────────────

    def maybe_run(self, *, session_key: str) -> int:
        """Mine the recent history. Returns how many memories were
        written (0 when throttled, disabled, or no candidates).
        """
        if self._memory is None or self._embedder is None:
            self._stats["skipped_disabled"] += 1
            return 0
        now = time.monotonic()
        if now - self._last_run_at < self._min_seconds_between:
            self._stats["skipped_throttled"] += 1
            return 0
        try:
            messages = self._db.get_messages(session_key)
        except Exception:
            return 0
        if not messages:
            return 0
        # Tail of the history within our window.
        tail = messages[-self._history_window :]
        user_count = sum(
            1 for r in tail if (r.role or "").lower() == "user"
        )
        if user_count - self._last_run_user_count < self._min_new_user_turns:
            self._stats["skipped_throttled"] += 1
            return 0
        self._last_run_at = now
        self._last_run_user_count = user_count
        self._stats["scheduled"] += 1

        candidates = _harvest_candidates(
            tail,
            min_n=self._min_n,
            max_n=self._max_n,
            min_total_count=self._min_total_count,
            require_both_sides=self._require_both_sides,
        )
        self._stats["candidates_seen"] += len(candidates)
        if not candidates:
            self._stats["skipped_no_candidates"] += 1
            return 0
        existing_phrases = self._existing_catchphrase_phrases()
        return self._persist_top_candidates(
            candidates,
            existing_phrases=existing_phrases,
            session_key=session_key,
        )

    # ── internals ───────────────────────────────────────────────────────

    def _existing_catchphrase_phrases(self) -> list[str]:
        store = self._memory
        if store is None:
            return []
        try:
            top = store.list_top(limit=64)
        except Exception:
            return []
        return [
            (m.content or "").strip().lower()
            for m in top
            if (m.kind or "").lower() == "catchphrase" and m.content
        ]

    def _persist_top_candidates(
        self,
        candidates: list[CatchphraseCandidate],
        *,
        existing_phrases: list[str],
        session_key: str,
    ) -> int:
        written = 0
        for cand in candidates:
            if written >= self._max_writes:
                break
            phrase = cand.phrase.strip()
            if not phrase:
                continue
            phrase_lower = phrase.lower()
            if _is_subsumed(phrase_lower, existing_phrases):
                continue
            try:
                emb = self._embedder.embed(phrase)
            except Exception:
                log.debug("catchphrase embed failed", exc_info=True)
                self._stats["failed"] += 1
                continue
            # Salience scales with balanced usage: a phrase used 3:3
            # outranks one used 5:1 even if both have count 6.
            balance = min(cand.user_count, cand.assistant_count) / max(
                1, cand.count // 2
            )
            salience = max(0.3, min(0.9, self._salience + 0.1 * (balance - 1.0)))
            try:
                memory = self._memory.add(
                    content=phrase,
                    kind="catchphrase",
                    embedding=emb,
                    salience=salience,
                    source_session=session_key,
                    source_message_id=None,
                    # Schema v8: catchphrases are analytic outputs over
                    # an entire conversation window -- already vetted
                    # by recurrence, so they go straight to long_term.
                    tier="long_term",
                )
            except Exception:
                log.debug("catchphrase memory insert failed", exc_info=True)
                self._stats["failed"] += 1
                continue
            if memory is None:
                # Dedup hit — same phrase already exists.
                existing_phrases.append(phrase_lower)
                continue
            existing_phrases.append(phrase_lower)
            written += 1
            self._stats["memories_written"] += 1
            log.info(
                "catchphrase mined: %r (count=%d user=%d assistant=%d)",
                phrase, cand.count, cand.user_count, cand.assistant_count,
            )
        if written == 0 and candidates:
            self._stats["skipped_no_candidates"] += 1
        else:
            self._stats["completed"] += 1
        return written


__all__ = [
    "CatchphraseCandidate",
    "CatchphraseMiner",
    "_harvest_candidates",
]
