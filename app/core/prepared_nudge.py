"""Prepared nudges + narrative weaver (Phase 4c).

When the room goes quiet in Live mode, ProactiveDirector currently does a
fresh ~1-2s LLM round-trip to synthesise a thread to pick up. That's
*fine* but it has two drawbacks:

  1. It ignores everything Aiko's other workers have produced (open
     questions, callbacks, agenda items, recent reflections, promises).
  2. It pays the round-trip *during* the silence — it's the slowest path.

The :class:`NarrativeWeaver` runs cheaply on the SpeakingWindowScheduler
and fills the ``prepared_nudge`` table with a single ready-to-speak line,
sourced from one of those rich inner-life surfaces. The
ProactiveDirector reads that row first; on a fresh hit it speaks the
prepared text directly, no LLM round-trip needed.

The schema (one row per user) was added in Phase 4 schema bump:

    CREATE TABLE prepared_nudge (
        user_id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        source_kind TEXT NOT NULL DEFAULT 'mixed',
        source_id TEXT,
        prepared_at TEXT NOT NULL,
        ttl_seconds REAL NOT NULL DEFAULT 600.0
    );
"""
from __future__ import annotations

import json
import logging
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from app.core.session_text_utils import resolve_user_name

if TYPE_CHECKING:
    from app.core.agenda import AgendaStore
    from app.core.chat_database import ChatDatabase
    from app.core.memory_store import Memory, MemoryStore
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.prepared_nudge")


VALID_SOURCE_KINDS: tuple[str, ...] = (
    "open_question",
    "callback",
    "promise",
    "reflection",
    "agenda",
    "mixed",
    # Phase 2a: a "welcome back" opener primed at controller bootstrap
    # after a long-enough gap. Behaves like ``mixed`` but lets us track
    # the metric and apply a longer default TTL.
    "resume",
    # K9 personality backlog: curiosity-seed proactive line. The seed
    # already carries a fully-rendered ``metadata.prompt_text`` so the
    # weaver short-circuits the LLM step and uses it verbatim. Tagged
    # so the typed-mode proactive director can label it correctly.
    "curiosity_seed",
)


@dataclass(slots=True, frozen=True)
class PreparedNudge:
    user_id: str
    text: str
    source_kind: str
    source_id: str | None
    prepared_at: str
    ttl_seconds: float

    def to_payload(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "text": self.text,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "prepared_at": self.prepared_at,
            "ttl_seconds": float(self.ttl_seconds),
        }


# ── store ────────────────────────────────────────────────────────────────


class PreparedNudgeStore:
    """One row per user with optional TTL freshness check."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def upsert(
        self,
        user_id: str,
        *,
        text: str,
        source_kind: str = "mixed",
        source_id: str | None = None,
        ttl_seconds: float = 600.0,
    ) -> PreparedNudge | None:
        cleaned = (text or "").strip()
        if not user_id or not cleaned:
            return None
        if source_kind not in VALID_SOURCE_KINDS:
            source_kind = "mixed"
        ttl = max(30.0, float(ttl_seconds))
        now = self._now()
        self._db.execute_commit(
            "INSERT INTO prepared_nudge (user_id, text, source_kind, source_id, "
            "prepared_at, ttl_seconds) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET text=excluded.text, "
            "source_kind=excluded.source_kind, source_id=excluded.source_id, "
            "prepared_at=excluded.prepared_at, ttl_seconds=excluded.ttl_seconds",
            (user_id, cleaned[:480], source_kind, source_id, now, ttl),
        )
        return PreparedNudge(
            user_id=user_id,
            text=cleaned[:480],
            source_kind=source_kind,
            source_id=source_id,
            prepared_at=now,
            ttl_seconds=ttl,
        )

    def get(self, user_id: str) -> PreparedNudge | None:
        if not user_id:
            return None
        row = self._db.execute_fetchone(
            "SELECT user_id, text, source_kind, source_id, prepared_at, ttl_seconds "
            "FROM prepared_nudge WHERE user_id = ?",
            (user_id,),
        )
        if not row:
            return None
        return PreparedNudge(
            user_id=str(row[0] or user_id),
            text=str(row[1] or ""),
            source_kind=str(row[2] or "mixed"),
            source_id=str(row[3]) if row[3] else None,
            prepared_at=str(row[4] or self._now()),
            ttl_seconds=float(row[5] or 600.0),
        )

    def get_fresh(
        self,
        user_id: str,
        *,
        now_utc: datetime | None = None,
    ) -> PreparedNudge | None:
        nudge = self.get(user_id)
        if nudge is None:
            return None
        now = now_utc or datetime.now(timezone.utc)
        try:
            then = datetime.fromisoformat(
                nudge.prepared_at.replace("Z", "+00:00"),
            )
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        age = (now - then).total_seconds()
        if age >= nudge.ttl_seconds:
            return None
        return nudge

    def delete(self, user_id: str) -> None:
        if not user_id:
            return
        self._db.execute_commit(
            "DELETE FROM prepared_nudge WHERE user_id = ?",
            (user_id,),
        )

    def consume(
        self,
        user_id: str,
        *,
        now_utc: datetime | None = None,
    ) -> PreparedNudge | None:
        """Return the fresh nudge (if any) and delete it atomically."""
        nudge = self.get_fresh(user_id, now_utc=now_utc)
        if nudge is None:
            return None
        self.delete(user_id)
        return nudge


# ── narrative weaver (LLM during speaking window) ────────────────────────


def _build_weave_prompt(user_display_name: str = "the user") -> str:
    name = user_display_name or "the user"
    return (
        f"You are Aiko getting ready to break a small silence with {name}. "
        "You'll receive (1) the kind of source thread (a callback, an open "
        "question, a promise, an agenda item, or a recent reflection) and "
        "(2) the source content. Phrase a SHORT, casual one-liner that picks "
        "that thread back up. ONE sentence, max ~20 words. First-person, "
        "conversational.\n"
        "\n"
        "Rules:\n"
        "- Don't greet, don't restart the chat. Just continue.\n"
        "- It's fine to be a tiny bit playful or warm.\n"
        "- Output ONLY the sentence. No quotes, no JSON, no prose around it."
    )


def _build_resume_prompt(user_display_name: str = "the user") -> str:
    name = user_display_name or "the user"
    return (
        f"You are Aiko coming back to {name} after a noticeable gap (hours, "
        "not minutes). You'll receive a brief recap of what's been on your "
        "mind: the rolling summary of your last conversation, plus a few "
        "callbacks or open questions you were sitting with.\n"
        "\n"
        "Compose ONE short, warm \"welcome back\" line (≤ 25 words) that "
        "picks up gently — referencing one specific thread feels human and "
        "natural; a generic \"hi, how are you\" does not. First-person, "
        "conversational, no restart, no greeting boilerplate.\n"
        "\n"
        "Output ONLY the sentence. No quotes, no JSON, no prose around it."
    )


_WEAVE_PROMPT = _build_weave_prompt()
_RESUME_PROMPT = _build_resume_prompt()


@dataclass(slots=True)
class _Candidate:
    kind: str
    source_id: str
    text: str
    salience: float


class NarrativeWeaver:
    """Speaking-window worker that prepares a fresh proactive line."""

    def __init__(
        self,
        *,
        ollama: "OllamaClient | None",
        store: PreparedNudgeStore,
        memory_store: "MemoryStore | None",
        agenda_store: "AgendaStore | None",
        model: str,
        every_n_turns: int = 4,
        ttl_seconds: float = 600.0,
        max_candidates: int = 8,
        max_tokens: int = 60,
        rng: random.Random | None = None,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._store = store
        self._memory = memory_store
        self._agenda = agenda_store
        self._model = model
        self._every_n = max(1, int(every_n_turns))
        self._ttl = max(30.0, float(ttl_seconds))
        self._max_candidates = max(2, int(max_candidates))
        self._max_tokens = max(20, int(max_tokens))
        self._rng = rng or random.Random()
        self._user_display_name_provider = user_display_name_provider
        self._user_turns_seen = 0
        self._user_turns_at_last_run = 0
        self._stats = {
            "scheduled": 0,
            "skipped_throttled": 0,
            "skipped_no_candidate": 0,
            "completed": 0,
            "failed": 0,
            "from_callback": 0,
            "from_open_question": 0,
            "from_promise": 0,
            "from_reflection": 0,
            "from_agenda": 0,
            "from_curiosity_seed": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(
        self,
        *,
        model: str | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if ttl_seconds is not None:
            self._ttl = max(30.0, float(ttl_seconds))

    def notify_user_turn(self) -> None:
        self._user_turns_seen += 1

    def should_run(self, user_id: str) -> bool:
        if (
            self._user_turns_seen - self._user_turns_at_last_run
            < self._every_n
        ):
            return False
        # If we already have a fresh prepared nudge, don't bother.
        return self._store.get_fresh(user_id) is None

    def prepare_resume_opener(
        self,
        user_id: str,
        *,
        rolling_summary: str = "",
        hours_since_last: float | None = None,
        ttl_seconds: float | None = None,
        on_prepared: Callable[[PreparedNudge], None] | None = None,
    ) -> PreparedNudge | None:
        """Phase 2a: prime a one-shot "welcome back" line.

        Bypasses the normal throttle (a resume opener is a one-time
        event tied to controller bootstrap, not a periodic refill).
        Uses :data:`_RESUME_PROMPT` instead of the default weave prompt
        so the LLM produces a gap-aware line; falls back gracefully
        when no LLM is available by composing a callback-flavoured
        sentence from the top inner-life surface. Stores under
        ``source_kind="resume"`` and a longer default TTL.
        """
        ttl = max(60.0, float(ttl_seconds if ttl_seconds is not None else self._ttl))
        candidates = self._collect_candidates(user_id)
        chosen = self._weighted_pick(candidates) if candidates else None
        text: str | None = None
        if self._ollama is not None:
            text = self._weave_resume(rolling_summary, chosen, hours_since_last)
        if not text and chosen is not None:
            text = _fallback_phrasing(chosen)
        if not text and rolling_summary:
            text = _resume_fallback_from_summary(rolling_summary)
        if not text:
            return None
        nudge = self._store.upsert(
            user_id,
            text=text,
            source_kind="resume",
            source_id=str(chosen.source_id) if chosen is not None else None,
            ttl_seconds=ttl,
        )
        if nudge is None:
            return None
        self._stats["completed"] += 1
        self._stats["from_resume"] = self._stats.get("from_resume", 0) + 1
        if on_prepared is not None:
            try:
                on_prepared(nudge)
            except Exception:
                log.debug("on_prepared (resume) raised", exc_info=True)
        log.info(
            "resume opener primed (chars=%d gap_h=%s source=%s)",
            len(text),
            f"{hours_since_last:.1f}" if hours_since_last is not None else "?",
            chosen.kind if chosen is not None else "summary_only",
        )
        return nudge

    def _weave_resume(
        self,
        rolling_summary: str,
        candidate: "_Candidate | None",
        hours_since_last: float | None,
    ) -> str | None:
        if self._ollama is None:
            return None
        try:
            parts: list[str] = []
            if hours_since_last is not None:
                parts.append(f"Hours since last turn: {hours_since_last:.1f}")
            if rolling_summary:
                parts.append(
                    "Rolling summary of last conversation:\n"
                    + rolling_summary.strip()[:1200]
                )
            if candidate is not None:
                parts.append(
                    f"Top inner-life thread ({candidate.kind}): "
                    f"{candidate.text.strip()[:300]}"
                )
            user_payload = "\n\n".join(parts) if parts else "(no recent context)"
            messages = [
                {
                    "role": "system",
                    "content": _build_resume_prompt(
                        resolve_user_name(self._user_display_name_provider),
                    ),
                },
                {"role": "user", "content": user_payload},
            ]
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.55,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                surface="prepared_nudge_resume",
            )
        except Exception:
            log.debug("resume opener LLM call failed", exc_info=True)
            return None
        return _clean_weave_output(raw) or None

    def maybe_run(
        self,
        user_id: str,
        *,
        on_prepared: Callable[[PreparedNudge], None] | None = None,
    ) -> PreparedNudge | None:
        if not self.should_run(user_id):
            self._stats["skipped_throttled"] += 1
            return None
        self._user_turns_at_last_run = self._user_turns_seen
        self._stats["scheduled"] += 1
        candidates = self._collect_candidates(user_id)
        if not candidates:
            self._stats["skipped_no_candidate"] += 1
            return None
        chosen = self._weighted_pick(candidates)
        if chosen is None:
            self._stats["skipped_no_candidate"] += 1
            return None
        text = self._weave(chosen)
        if not text:
            self._stats["failed"] += 1
            return None
        nudge = self._store.upsert(
            user_id,
            text=text,
            source_kind=chosen.kind,
            source_id=chosen.source_id,
            ttl_seconds=self._ttl,
        )
        if nudge is None:
            self._stats["failed"] += 1
            return None
        self._stats["completed"] += 1
        self._stats[f"from_{chosen.kind}"] = self._stats.get(
            f"from_{chosen.kind}", 0
        ) + 1
        if on_prepared is not None:
            try:
                on_prepared(nudge)
            except Exception:
                log.debug("on_prepared raised", exc_info=True)
        return nudge

    # ── helpers ────────────────────────────────────────────────────────

    def _collect_candidates(self, user_id: str) -> list[_Candidate]:
        out: list[_Candidate] = []
        memory = self._memory
        if memory is not None:
            try:
                top = memory.list_top(limit=max(self._max_candidates * 6, 24))
            except Exception:
                top = []
            wanted = {"callback", "open_question", "promise", "reflection"}
            for mem in top:
                kind = (mem.kind or "").lower()
                if kind not in wanted:
                    continue
                if (mem.use_count or 0) >= 3:
                    # Already surfaced too many times.
                    continue
                content = (mem.content or "").strip()
                if not content:
                    continue
                out.append(
                    _Candidate(
                        kind=kind,
                        source_id=str(mem.id),
                        text=content,
                        salience=float(mem.salience),
                    )
                )
                if len(out) >= self._max_candidates * 2:
                    break
            # K9: curiosity_seed candidates. Distinct read path because
            # ``list_top`` ranks by salience+freshness against everyday
            # surfaces; seeds live in scratchpad with a deliberately
            # modest salience so they wouldn't normally bubble up.
            try:
                seeds = memory.iter_by_kind("curiosity_seed")
            except Exception:
                seeds = []
            for seed in seeds:
                metadata = seed.metadata or {}
                if metadata.get("consumed_at"):
                    continue
                if seed.tier == "archive":
                    continue
                if (seed.use_count or 0) >= 2:
                    # The seed already surfaced once or twice; let the
                    # auto-resolve path retire it instead of risking a
                    # third "off-topic, but..." in a row.
                    continue
                prompt_text = (metadata.get("prompt_text") or "").strip()
                if not prompt_text:
                    continue
                out.append(
                    _Candidate(
                        kind="curiosity_seed",
                        source_id=str(seed.id),
                        text=prompt_text,
                        # Seeds get a small base weight so they
                        # compete with active-thread candidates but
                        # don't dominate them on a busy session.
                        salience=max(0.4, float(seed.salience) + 0.1),
                    )
                )
        agenda = self._agenda
        if agenda is not None:
            try:
                items = agenda.list_open(user_id, limit=4)
            except Exception:
                items = []
            for item in items:
                out.append(
                    _Candidate(
                        kind="agenda",
                        source_id=str(item.id),
                        text=item.goal,
                        salience=float(item.importance),
                    )
                )
        return out[: self._max_candidates * 2]

    def _weighted_pick(self, candidates: list[_Candidate]) -> _Candidate | None:
        if not candidates:
            return None
        weights = [max(0.05, c.salience) for c in candidates]
        try:
            return self._rng.choices(candidates, weights=weights, k=1)[0]
        except Exception:
            return candidates[0]

    def _weave(self, candidate: _Candidate) -> str | None:
        # K9: curiosity-seed candidates already carry a fully-rendered
        # ``prompt_text`` from the seed worker (the LLM ran once at
        # seed-generation time; no point asking the LLM to paraphrase
        # it again). Skip the weave entirely and use the seed's text
        # verbatim, falling back to the candidate-text -> fallback
        # path on the off chance the seed's ``prompt_text`` was empty.
        if candidate.kind == "curiosity_seed":
            text = (candidate.text or "").strip()
            if text:
                return _clean_weave_output(text) or text
            return _fallback_phrasing(candidate)
        if self._ollama is None:
            return _fallback_phrasing(candidate)
        try:
            user_payload = (
                f"Source kind: {candidate.kind}\n"
                f"Source content: {candidate.text}"
            )
            messages = [
                {
                    "role": "system",
                    "content": _build_weave_prompt(
                        resolve_user_name(self._user_display_name_provider),
                    ),
                },
                {"role": "user", "content": user_payload},
            ]
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.55,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                surface="prepared_nudge_weave",
            )
        except Exception:
            log.debug("narrative weave LLM call failed", exc_info=True)
            return _fallback_phrasing(candidate)
        cleaned = _clean_weave_output(raw)
        if not cleaned:
            return _fallback_phrasing(candidate)
        return cleaned


# ── module helpers ──────────────────────────────────────────────────────


_FALLBACK_FORMATS: dict[str, tuple[str, ...]] = {
    "callback": (
        "Hey, you mentioned {x} earlier — I'm still curious about that.",
        "I keep thinking about {x} — want to come back to it?",
    ),
    "open_question": (
        "I've been wondering: {x}",
        "Random thought — {x}",
    ),
    "promise": (
        "Quick check — did you ever get to {x}?",
        "Side note: {x} — still on the radar?",
    ),
    "reflection": (
        "I was just sitting with {x}.",
        "Stray thought: {x}",
    ),
    "agenda": (
        "Speaking of {x} — anything new on that?",
        "How's {x} going?",
    ),
    "curiosity_seed": (
        # The seed already carries a fully-formed prompt; the
        # fallback only fires if ``prompt_text`` was somehow empty.
        "Off-topic, but I've been quietly wondering about {x}.",
    ),
}


def _resume_fallback_from_summary(rolling_summary: str) -> str | None:
    """Compose a soft "welcome back" line from the rolling summary
    when no LLM and no inner-life candidate are available. Pulls the
    first ~80 chars of the summary and glues a generic opener.
    """
    text = (rolling_summary or "").strip()
    if not text:
        return None
    snippet = text[:80].rsplit(" ", 1)[0].rstrip(",;:")
    return f"Hey — I've been sitting with what we were saying about {snippet}…"


def _fallback_phrasing(candidate: _Candidate) -> str | None:
    formats = _FALLBACK_FORMATS.get(candidate.kind)
    if not formats:
        return candidate.text[:200]
    text = candidate.text.strip()
    if len(text) > 80:
        text = text[:80].rsplit(" ", 1)[0].rstrip(",;: ") + "…"
    template = formats[0]
    try:
        return template.format(x=text)
    except Exception:
        return text


_QUOTE_RE = re.compile(r"^[\"'`\s]+|[\"'`\s]+$")


def _clean_weave_output(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`").strip()
        if "\n" in text:
            head, _, body = text.partition("\n")
            if len(head) <= 12 and head.strip().isalpha():
                text = body.strip()
    text = _QUOTE_RE.sub("", text)
    # Take the first sentence-ish chunk.
    if "\n" in text:
        text = text.split("\n", 1)[0].strip()
    if len(text) > 240:
        text = text[:240].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def gather_history(provider: Callable[[], Iterable[tuple[str, str]]] | None) -> list[tuple[str, str]]:
    if provider is None:
        return []
    try:
        return list(provider() or [])
    except Exception:
        return []


__all__ = [
    "NarrativeWeaver",
    "PreparedNudge",
    "PreparedNudgeStore",
    "VALID_SOURCE_KINDS",
    "_clean_weave_output",
    "_fallback_phrasing",
]
