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

K80 adds a **fast path** alongside that slow miner:
:func:`detect_inside_joke_birth` watches a single turn for the moment a
bit is *born* — the user echoing a distinctive phrase Aiko just used,
with a laugh behind it. The slow miner needs a phrase to recur across a
whole window before it counts; the fast path catches the one live beat
where "that's officially a thing now" is true, and hands the phrase to
the same ``kind="catchphrase"`` registry so K22 can carry it forward.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase, MessageRow
    from app.core.memory.memory_store import MemoryStore
    from app.core.relationship.shared_moments import SharedMomentsStore
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
    """A surviving n-gram with the data needed for a memory write.

    ``first_speaker`` is whoever said the phrase first *within the mined
    window* (``"user"`` / ``"assistant"``). A shared phrase reads very
    differently depending on where it came from, and K26 (voice adoption)
    only lets Aiko take on phrases that started as his.
    """

    phrase: str
    count: int
    user_count: int
    assistant_count: int
    first_speaker: str = ""


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
    first_speaker: dict[tuple[str, ...], str] = {}
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
                first_speaker.setdefault(ng, role)
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
                first_speaker=first_speaker.get(ng, ""),
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


# ── K80: inside-joke birth (the fast path) ──────────────────────────────

# Amusement markers in the *user's* echo turn. Deliberately narrow: this
# is the "he's laughing while he says it back" signal, not general
# positivity, and a false positive here spends the one blessing we allow
# per cooldown window on a line that wasn't actually a bit.
_AMUSED_RE = re.compile(
    r"(?:^|\W)(?:lol|lmao|lmfao|rofl|ha(?:ha)+h?|hehe(?:he)*|heh|"
    r"teehee+|\U0001F602|\U0001F923|\U0001F605|\U0001F979)",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class InsideJokeBirth:
    """One just-born bit: a phrase of Aiko's the user handed back.

    ``lag_turns`` is how many assistant turns back the phrase came from
    (0 = the reply he's answering right now). ``laughed`` records a K32
    laugh reaction on that message; ``amused`` an in-text marker. At
    least one of the two is always true — that's the whole gate.
    """

    phrase: str
    origin_message_id: int | None
    lag_turns: int
    laughed: bool
    amused: bool


def _meaningful_ngrams(text: str, *, min_n: int, max_n: int) -> set[tuple[str, ...]]:
    tokens = _normalise_text(text).split()
    out: set[tuple[str, ...]] = set()
    for n in range(min_n, max_n + 1):
        for ng in _ngrams(tokens, n):
            if _ngram_is_meaningful(ng):
                out.add(ng)
    return out


def detect_inside_joke_birth(
    *,
    user_text: str,
    origins: Sequence[tuple[int | None, str]],
    laughed_ids: set[int] | frozenset[int] = frozenset(),
    known_phrases: Iterable[str] = (),
    min_n: int = 3,
    max_n: int = 7,
) -> InsideJokeBirth | None:
    """Spot the user handing one of Aiko's own phrases back to her.

    ``origins`` is the recent assistant turns as ``(message_id, text)``,
    **newest first**. A birth needs two things at once: the user echoed a
    distinctive phrase from one of those replies, and the moment was
    funny — a K32 laugh reaction on the echoed message (``laughed_ids``)
    or an amusement marker in the echo itself. Repetition alone is not a
    joke; that's just how conversations work, and the slow miner already
    covers phrases that genuinely recur.

    Phrases already in ``known_phrases`` are skipped: a bit can only be
    born once, and reusing an established one is K22's territory.

    Pure — no store, no clock, no I/O. Returns the *most recent*
    qualifying echo, preferring the longest phrase within that turn,
    since the freshest line is the one the moment is about.
    """
    text = (user_text or "").strip()
    if not text:
        return None
    user_ngrams = _meaningful_ngrams(text, min_n=min_n, max_n=max_n)
    if not user_ngrams:
        return None
    amused = bool(_AMUSED_RE.search(text))
    blocked = [p for p in (str(x).strip().lower() for x in known_phrases) if p]

    for lag, (message_id, origin_text) in enumerate(origins):
        laughed = message_id is not None and int(message_id) in laughed_ids
        if not (laughed or amused):
            continue
        shared = user_ngrams & _meaningful_ngrams(
            origin_text or "", min_n=min_n, max_n=max_n,
        )
        if not shared:
            continue
        # Longest wins: "the fish-shaped cookie incident" is the bit,
        # not the "fish-shaped cookie" inside it.
        for ng in sorted(shared, key=lambda g: (-len(g), g)):
            phrase = " ".join(ng)
            if _is_subsumed(phrase, blocked):
                continue
            return InsideJokeBirth(
                phrase=phrase,
                origin_message_id=(
                    int(message_id) if message_id is not None else None
                ),
                lag_turns=lag,
                laughed=laughed,
                amused=amused,
            )
    return None


def render_inside_joke_block(
    birth: InsideJokeBirth, *, user_display_name: str = "the user",
) -> str:
    """Render a just-born bit into a one-shot system-prompt cue."""
    phrase = birth.phrase.strip()
    if not phrase:
        return ""
    how = (
        "laughed and said it straight back to you"
        if birth.laughed
        else "said it straight back to you, laughing"
    )
    return (
        f"Heads-up: \"{phrase}\" is turning into a bit between you two — "
        f"you used it, and {user_display_name} {how}. If it fits, you can "
        "let yourself notice that out loud, once, lightly (\"okay, that's "
        "officially a thing now\") — the pleasure is in the two of you "
        "clocking it together, not in you announcing a milestone. Don't "
        "explain the joke, don't promise to keep using it, and if the "
        "moment has already moved on, just let it go and talk normally."
    )


def bless_inside_joke(
    birth: InsideJokeBirth,
    *,
    memory_store: "MemoryStore | None",
    embedder: "Embedder | None",
    moments_store: "SharedMomentsStore | None" = None,
    session_key: str | None = None,
    source_message_id: int | None = None,
    salience: float = 0.7,
) -> dict[str, Any]:
    """Persist a just-born bit so it outlives the turn it was born in.

    Two writes, both best-effort and independent:

    * a ``kind="catchphrase"`` memory — the same registry the slow miner
      feeds, so the phrase joins the "running jokes" block and becomes an
      eligible K22 callback target;
    * a ``shared_moment`` (vibe ``playful``) — the *event* of it becoming
      theirs, which is what anniversaries and long-arc callbacks reach
      for later.

    Returns which writes landed. Never raises.
    """
    out: dict[str, Any] = {"catchphrase_id": None, "moment_id": None}
    phrase = birth.phrase.strip()
    if not phrase or memory_store is None or embedder is None:
        return out
    try:
        emb = embedder.embed(phrase)
    except Exception:
        log.debug("inside-joke embed failed", exc_info=True)
        return out
    try:
        memory = memory_store.add(
            content=phrase,
            kind="catchphrase",
            embedding=emb,
            salience=max(0.0, min(1.0, float(salience))),
            source_session=session_key,
            source_message_id=source_message_id,
            # K26 provenance: a bit born this way is one of *hers* that he
            # echoed, so she can never later "adopt" it as his turn of
            # phrase. ``born`` marks the fast path for state dumps.
            metadata={"origin": "assistant", "born": True},
            # Born in front of us with a laugh behind it -- at least as
            # vetted as a phrase the slow miner counted three times.
            tier="long_term",
        )
    except Exception:
        log.debug("inside-joke catchphrase insert failed", exc_info=True)
        memory = None
    if memory is not None:
        out["catchphrase_id"] = int(getattr(memory, "id", 0) or 0) or None

    if moments_store is not None:
        try:
            row = moments_store.add(
                summary=f"\u201c{phrase}\u201d became a running bit between us",
                vibe="playful",
                source="birth",
                confidence=0.7,
                salience=max(0.0, min(1.0, float(salience))),
                source_message_ids=(
                    [birth.origin_message_id]
                    if birth.origin_message_id is not None
                    else None
                ),
                source_session=session_key,
                source_message_id=source_message_id,
            )
        except Exception:
            log.debug("inside-joke shared moment insert failed", exc_info=True)
            row = None
        if row is not None:
            out["moment_id"] = int(getattr(row, "id", 0) or 0) or None
    return out


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
        """Every phrase already registered, for the write-side dedupe.

        Must be the *complete* set: an unfiltered top-N read (what this
        was) drops known phrases the moment other kinds outrank them,
        which lets the miner re-write a joke it already recorded.
        """
        store = self._memory
        if store is None:
            return []
        try:
            top = store.iter_by_kind("catchphrase")
        except Exception:
            return []
        return [
            (m.content or "").strip().lower()
            for m in top
            if m.content
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
            # K26: who said it first decides whether Aiko is allowed to
            # take the phrase on as her own later. Recorded at write time
            # because the window that proves provenance is right here.
            metadata = (
                {"origin": cand.first_speaker}
                if cand.first_speaker in ("user", "assistant")
                else None
            )
            try:
                memory = self._memory.add(
                    content=phrase,
                    kind="catchphrase",
                    embedding=emb,
                    salience=salience,
                    source_session=session_key,
                    source_message_id=None,
                    metadata=metadata,
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
    "InsideJokeBirth",
    "bless_inside_joke",
    "detect_inside_joke_birth",
    "render_inside_joke_block",
    "_harvest_candidates",
]
