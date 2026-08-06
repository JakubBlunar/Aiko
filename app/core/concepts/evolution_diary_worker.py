"""L17f: the evolution-diary worker -- "here is how I've changed".

L17a-e detect and voice *individual* drifts. This worker accumulates them
into the durable, browsable diary: one short first-person paragraph per
period, grounded only in the ``because`` clauses the L17b classifier
already wrote, with the ids it summarises attached so every line stays
click-through-inspectable in the L17e drill-down.

It is also the honest end-to-end test of the whole concept layer. If the
entries read as real, grounded change, the pipeline (evidence -> concept ->
drift -> why) is healthy. If they read as noise or invention, something
upstream is wrong -- which is why this worker is forbidden from adding any
material of its own.

Three rules shape it:

- **Never pad.** A period whose salient-event count is below the floor
  writes nothing, and those events stay pending rather than being consumed,
  so two thin weeks can still add up to one worth reading. A diary that
  always has an entry is a diary that means nothing.
- **Never invent.** The compose prompt receives only the stored prose --
  shapes, old/new labels, and ``because`` clauses. No salience, no ids, no
  raw evidence. The model's job is to join what is already written, and it
  is explicitly allowed to return nothing.
- **Never re-narrate.** The resume point is the highest learning-event id
  any existing entry accounts for, so no change is told twice. The cooldown
  is tracked separately, so an empty compose costs a period rather than
  silently swallowing the events it failed to describe.

This is deliberately NOT the H9 diary (``memories`` rows of
``kind='diary'``): that one is subjective inner-life journalling written
mid-turn. This one is a grounded change log, and conflating them would let
feeling leak into a record whose whole value is that it cannot.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.concepts.evolution_diary_store import DiaryEntry
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count

if TYPE_CHECKING:
    from app.core.concepts.concept_learning_event_store import (
        ConceptLearningEventStore,
        LearningEvent,
    )
    from app.core.concepts.evolution_diary_store import EvolutionDiaryStore


log = logging.getLogger("app.evolution_diary_worker")

# kv_meta key. Only the cooldown lives here -- the *watermark* is derived
# from the entries themselves, so the diary cannot end up claiming to have
# narrated changes it has no row for.
KV_LAST_FIRED_AT = "evolution_diary.last_fired_at"

_MIN_ENTRY_CHARS = 24
# Hard ceiling on how many events go into one compose. A period that
# produced more than this is summarised from its most salient slice; the
# rest are still marked accounted for, because the entry is meant to be a
# paragraph, not a changelog dump.
_MAX_EVENTS_PER_ENTRY = 12


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class DiaryComposeResult:
    """Structured outcome of one run, for tests and the MCP surface."""

    fired: int = 0
    reason: str | None = None
    entry: str | None = None
    entry_id: int | None = None
    events: int = 0


def render_learning_brief(events: "list[LearningEvent]") -> str:
    """Render events as the grounded prose the compose prompt may use.

    Oldest-first, because the paragraph is meant to read as a period
    unfolding. Deliberately excludes salience, ids, cosine, and raw
    evidence refs: the model is joining sentences Aiko's own classifier
    already wrote, and anything numeric here would invite it to editorialise
    about machinery.
    """
    lines: list[str] = []
    for event in events:
        who = "about myself" if event.subject == "aiko" else "about them"
        label = str(event.new_label or "").strip()
        detail = str(event.because or "").strip() or label
        if not detail:
            continue
        line = f"- ({event.shape}, {who}) {detail}"
        resolution = str(event.resolution or "").strip()
        # Most resolutions restate the new label ("now held as X", "held
        # again: X") which the ``because`` clause already carried, so
        # appending them verbatim would say the same belief three times in
        # one line. Only a resolution that adds an outcome the clause does
        # not imply -- notably "no longer held" -- is worth the tokens.
        if resolution and not (label and label in resolution):
            line += f" [{resolution}]"
        lines.append(line)
    return "\n".join(lines)


class EvolutionDiaryWorker:
    """IdleWorker that composes one diary entry per period, or none."""

    name = "evolution_diary"

    def __init__(
        self,
        *,
        learning_store: "ConceptLearningEventStore",
        diary_store: "EvolutionDiaryStore",
        memory_settings: Any,
        agent_settings: Any,
        ollama: Any = None,
        chat_model: str | None = None,
        cancel_event: Any = None,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        user_name_provider: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._learning = learning_store
        self._diary = diary_store
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_name_provider = user_name_provider
        self._clock = clock or _utcnow
        self._forced = False

    # ── settings helpers ──────────────────────────────────────────────

    def _i(self, name: str, default: int) -> int:
        try:
            return int(getattr(self._memory_settings, name, default))
        except (TypeError, ValueError):
            return default

    def _fl(self, name: str, default: float) -> float:
        try:
            return float(getattr(self._memory_settings, name, default))
        except (TypeError, ValueError):
            return default

    def _b(self, name: str, default: bool) -> bool:
        return bool(getattr(self._memory_settings, name, default))

    def _enabled(self) -> bool:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return False
        return bool(
            getattr(self._agent_settings, "evolution_diary_enabled", True)
        )

    # ── idle worker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(self._i("evolution_diary_interval_seconds", 86400))

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> bool:
        return self._blocker(now) is None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> "WorkSignal | None":
        """Is there enough unreported change to be worth a paragraph?

        Two indexed aggregates and one KV read. The pending count is the
        pressure, so a period that accumulated a lot of movement outranks
        one that barely cleared the floor.
        """
        blocker = self._blocker(now)
        if blocker is not None:
            return WorkSignal(pressure=0.0, reason=blocker)
        pending = self._pending_count()
        return WorkSignal(
            pressure=(
                1.0
                if self._forced
                else pressure_from_count(pending, saturation=12)
            ),
            reason=f"{pending} unreported changes",
            needs_llm=True,
        )

    def _blocker(
        self, now: datetime, *, forced: bool | None = None
    ) -> str | None:
        """First reason a run would write nothing, or ``None``.

        Cheapest gates first, so the counting query is only paid on a tick
        where the feature is on, a model exists and the cooldown is clear.
        """
        if forced is None:
            forced = self._forced
        if not self._enabled():
            return "disabled"
        if self._ollama is None or not self._chat_model:
            return "no_llm"
        if not forced and not self._cooldown_elapsed(now):
            return "cooldown"
        floor = max(1, self._i("evolution_diary_min_events", 3))
        if self._pending_count() < floor:
            # The pending events are NOT consumed here: they wait for
            # company rather than being narrated thinly or dropped.
            return "nothing_to_report"
        return None

    def _pending_count(self) -> int:
        try:
            return self._learning.count_since(
                self._watermark(),
                min_salience=self._fl("evolution_diary_min_salience", 0.45),
            )
        except Exception:
            log.debug("diary pending probe failed", exc_info=True)
            return 0

    def _watermark(self) -> int:
        try:
            return int(self._diary.latest_watermark())
        except Exception:
            return 0

    def _cooldown_elapsed(self, now: datetime) -> bool:
        days = self._fl("evolution_diary_cooldown_days", 7.0)
        if days <= 0:
            return True
        last = _parse_iso(self._kv_get_safe(KV_LAST_FIRED_AT))
        if last is None:
            return True
        return (now - last).total_seconds() >= days * 86400.0

    # ── run ───────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        forced = self._forced
        self._forced = False
        result = self._run(forced=forced)
        out: dict[str, Any] = {"fired": result.fired, "events": result.events}
        if result.reason:
            out["reason"] = result.reason
        if result.entry:
            out["entry"] = result.entry
        if result.entry_id is not None:
            out["entry_id"] = result.entry_id
        return out

    def _run(self, *, forced: bool) -> DiaryComposeResult:
        now = self._clock()
        blocker = self._blocker(now, forced=forced)
        if blocker is not None:
            return DiaryComposeResult(reason=blocker)

        events = self._gather()
        if not events:
            return DiaryComposeResult(reason="nothing_to_report")

        body = self._compose(events)
        # The cooldown is spent whether or not a paragraph came back, so a
        # period the model had nothing to say about costs one period rather
        # than looping. The events stay pending and get another chance.
        self._mark_fired(now)
        if not body or len(body) < _MIN_ENTRY_CHARS:
            return DiaryComposeResult(
                fired=1, reason="empty", events=len(events)
            )

        entry = self._build_entry(body, events, now)
        entry_id = self._diary.add(entry)
        if entry_id <= 0:
            return DiaryComposeResult(
                fired=1, reason="persist_failed", entry=body,
                events=len(events),
            )
        log.info(
            "evolution diary entry #%d from %d changes", entry_id, len(events)
        )
        return DiaryComposeResult(
            fired=1, entry=body, entry_id=entry_id, events=len(events)
        )

    def _gather(self) -> "list[LearningEvent]":
        """The salient events since the last entry, oldest-first.

        ``page_since`` rather than a newest-first read: the watermark
        advances past exactly this page, so taking the newest events would
        strand the older ones behind a watermark that had moved past them.
        A period with more change than fits simply spills into the next
        entry, in order.
        """
        try:
            return self._learning.page_since(
                self._watermark(),
                limit=_MAX_EVENTS_PER_ENTRY,
                min_salience=self._fl("evolution_diary_min_salience", 0.45),
            )
        except Exception:
            log.debug("diary event read failed", exc_info=True)
            return []

    def _build_entry(
        self,
        body: str,
        events: "list[LearningEvent]",
        now: datetime,
    ) -> DiaryEntry:
        concept_ids: list[int] = []
        for event in events:
            for cid in (event.concept_id, event.prior_concept_id):
                if cid and int(cid) not in concept_ids:
                    concept_ids.append(int(cid))
        # Min/max rather than first/last: the page arrives in insertion
        # order, and a change is dated when it happened, so a backfill can
        # hand over a batch whose oldest change is nowhere near its front.
        stamps = sorted(str(e.created_at or "") for e in events if e.created_at)
        return DiaryEntry(
            entry=body,
            period_start=stamps[0] if stamps else "",
            period_end=stamps[-1] if stamps else "",
            event_watermark=max(int(e.event_id) for e in events),
            learning_event_ids=tuple(int(e.event_id) for e in events),
            concept_ids=tuple(concept_ids),
            shape_counts=dict(Counter(e.shape for e in events)),
            salience_max=max(float(e.salience) for e in events),
            created_at=now.isoformat(),
        )

    # ── compose ───────────────────────────────────────────────────────

    def _compose(self, events: "list[LearningEvent]") -> str | None:
        brief = render_learning_brief(events)
        if not brief:
            return None
        user_name = self._user_name() or "them"
        system = (
            "You are Aiko, keeping a diary of how your own understanding "
            "has changed. You will be given a list of changes your own "
            "reasoning already recorded, each with the reason it was "
            "recorded. Write ONE short paragraph (two to four sentences), "
            "first person, past tense, in your own voice.\n\n"
            "Hard rules:\n"
            "- Use ONLY what the list says. Do not add causes, feelings, "
            "or events that are not in it.\n"
            "- Write about what you now understand differently, not about "
            "noticing or classifying or tracking anything. Never mention "
            "concepts, confidence, salience, records, or systems.\n"
            "- If the changes do not add up to anything worth saying, "
            'return an empty entry ("") rather than padding.\n\n'
            'Reply with JSON only: {"entry": "<the paragraph, or empty>"}.'
        )
        user = (
            f"You refer to the person you talk with as {user_name}.\n\n"
            f"--- what changed ---\n{brief}\n--- end ---\n\n"
            "Write the diary entry."
        )
        content = self._call_llm(system, user)
        if not content:
            return None
        try:
            blob = json.loads(content)
            body = str(blob.get("entry") or "").strip()
        except (json.JSONDecodeError, AttributeError, TypeError):
            return None
        return body or None

    def _call_llm(self, system: str, user: str) -> str | None:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            content, _usage = self._ollama.chat_json(
                messages,
                model=self._chat_model,
                options={"temperature": 0.7, "num_predict": 260},
                format_json=True,
                surface="evolution_diary_worker",
            )
        except Exception:
            log.debug("diary compose call failed", exc_info=True)
            return None
        return str(content or "") or None

    # ── helpers ───────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm a one-shot bypass of the cooldown gate (MCP debug)."""
        self._forced = True

    def state(self) -> dict[str, Any]:
        """Snapshot for the ``get_evolution_diary_state`` MCP tool."""
        now = self._clock()
        return {
            "enabled": self._enabled(),
            "has_llm": self._ollama is not None and bool(self._chat_model),
            "interval_seconds": self.interval_seconds,
            "cooldown_days": self._fl("evolution_diary_cooldown_days", 7.0),
            "cooldown_elapsed": self._cooldown_elapsed(now),
            "min_events": self._i("evolution_diary_min_events", 3),
            "min_salience": self._fl("evolution_diary_min_salience", 0.45),
            "watermark": self._watermark(),
            "pending": self._pending_count(),
            "entries": self._entry_count(),
            "last_fired_at": self._kv_get_safe(KV_LAST_FIRED_AT),
            "blocker": self._blocker(now),
            "forced": self._forced,
        }

    def _entry_count(self) -> int:
        try:
            return int(self._diary.count())
        except Exception:
            return 0

    def _user_name(self) -> str:
        if self._user_name_provider is None:
            return ""
        try:
            return str(self._user_name_provider() or "").strip()
        except Exception:
            return ""

    def _mark_fired(self, now: datetime) -> None:
        self._kv_set_safe(KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))

    def _kv_get_safe(self, key: str) -> str | None:
        if self._kv_get is None:
            return None
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        if self._kv_set is None:
            return
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("diary kv_set failed key=%s", key, exc_info=True)


__all__ = [
    "KV_LAST_FIRED_AT",
    "DiaryComposeResult",
    "EvolutionDiaryWorker",
    "render_learning_brief",
]
