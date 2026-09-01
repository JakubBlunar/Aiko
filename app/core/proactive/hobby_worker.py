"""HobbyWorker — H19 / H28, Aiko's ongoing personal project across days.

An :class:`IdleWorker` that maintains a single *current hobby* (a multi-day
thread Aiko returns to in her idle time) and advances it slowly during quiet
windows. Unlike the one-off away-beats (H13/H14) the hobby has continuity:
its progress counter climbs across days, it occasionally yields a *takeaway*
(surfaced through the shared H17 idle-seed cue so Aiko phrases it herself),
and it rotates to a fresh hobby once it's run long enough.

H28: the standing thread is bound to a named **artifact** (a title, a
sketch subject, a recipe). Rotation invents the next one on the worker
LLM with a kind-drift gate (wrapping a reading thread must not propose
another reading thread). A small history ring remembers what she already
started so she does not invent the same title twice; it is never a second
standing list. Reading hobbies bind to the room paperback.

State lives in ``kv_meta`` JSON blobs (``aiko.current_hobby`` and
``aiko.hobby_catalogue``); the catalogue, admission gate, and progress
math live in the pure :mod:`app.core.world.hobby` module. The standing
"what she's been up to" line is rendered by ``_render_hobby_block``.

K85b: the two moments this worker already stops to think about -- a
milestone and a wrap-up -- also leave a ``pursuit_note`` behind.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import WorkSignal
from app.core.world import hobby as hobby_mod
from app.core.world.idle_activity_worker import append_idle_seed
from app.core.world.room_evolution import (
    BOOK_SLUG,
    ensure_book_titled,
    is_generic_book_title,
    stamp_book_title,
)
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.pursuit_notes import PursuitNoteWriter
    from app.core.world.world_store import WorldStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.hobby_worker")


# Single ``kv_meta`` JSON blob, namespaced under ``aiko.*`` alongside the
# other idle-life state (day_color, vulnerability_budget, idle_seeds).
KV_CURRENT_HOBBY = "aiko.current_hobby"
# History / exclusion ring of hobbies she actually started. Cap lives in
# hobby.py; this is never auto-promoted as a second current hobby.
KV_HOBBY_CATALOGUE = "aiko.hobby_catalogue"


def load_hobby(kv_get: Callable[[str], str | None]) -> dict[str, Any] | None:
    """Return the current-hobby state blob (or ``None`` if unset/garbage)."""
    try:
        raw = kv_get(KV_CURRENT_HOBBY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except Exception:
        return None
    if not isinstance(blob, dict):
        return None
    if not blob.get("label") and not blob.get("artifact"):
        return None
    return blob


def load_hobby_history(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the started-hobby ring (empty on unset/garbage)."""
    try:
        raw = kv_get(KV_HOBBY_CATALOGUE)
    except Exception:
        return []
    if not raw:
        return []
    try:
        blob = json.loads(raw)
    except Exception:
        return []
    if not isinstance(blob, list):
        return []
    return [row for row in blob if isinstance(row, dict)]


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _parse_json_object(content: Any) -> dict[str, Any] | None:
    if isinstance(content, dict):
        return content
    if not content:
        return None
    try:
        blob = json.loads(content)
    except Exception:
        return None
    return blob if isinstance(blob, dict) else None


class HobbyWorker:
    """IdleWorker that advances + rotates Aiko's current hobby."""

    name = "hobby"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        user_display_name_provider: Callable[[], str],
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        idle_seed_max_ring: int = 6,
        pursuit_notes: "PursuitNoteWriter | None" = None,
        world_store: "WorldStore | None" = None,
        rng: random.Random | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._agent = agent_settings
        self._mem = memory_settings
        self._user_display_name_provider = user_display_name_provider
        self._ollama = ollama
        self._model = model
        self._idle_seed_max_ring = max(1, int(idle_seed_max_ring))
        self._pursuit_notes = pursuit_notes
        self._world = world_store
        self._rng = rng or random.Random()
        # MCP debug one-shots.
        self._force_advance = False
        self._force_rotate = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(self._mem, "hobby_worker_interval_seconds", 3600)
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        """Enabled, and a move actually available.

        The wall-clock advance floor (``hobby_advance_min_hours``, 6h by
        default) is a hard veto rather than pressure: between advances
        there is nothing for a run to do but re-read one kv key, and
        the heartbeat would otherwise wake it six times per floor.
        """
        if not bool(getattr(self._agent, "hobby_worker_enabled", True)):
            return False
        return self._next_move(now)[0] is not None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Which transition is due, and whether it will compose a seed.

        ``needs_llm`` is genuinely per-run here: a rotation always
        composes a wrap-up seed *and* invents the next hobby, a
        milestone advance composes a seed every ``hobby_milestone_every``
        advances, and an ordinary advance is a kv write. Cold start is
        a seed-catalogue pick — no worker LLM required.
        """
        if not bool(getattr(self._agent, "hobby_worker_enabled", True)):
            return WorkSignal(pressure=0.0, reason="disabled")
        move, composes = self._next_move(now)
        if move is None:
            return WorkSignal(pressure=0.0, reason="pacing")
        return WorkSignal(
            pressure=1.0 if move in ("start", "rotate") else 0.6,
            reason=move,
            needs_llm=bool(
                composes and self._ollama is not None and self._model
            ),
        )

    def _next_move(self, now: datetime) -> tuple[str | None, bool]:
        """``(move, composes_seed)`` for the transition a run would make.

        Read-only, and in particular it *peeks* at the two MCP one-shots
        instead of consuming them — spending a force flag on a probe
        would mean the run it was meant for never sees it.
        """
        try:
            state = load_hobby(self._chat_db.kv_get)
        except Exception:
            log.debug("hobby demand probe failed", exc_info=True)
            return None, False
        if state is None:
            return "start", False
        if self._force_rotate or hobby_mod.should_rotate(
            progress=int(state.get("progress", 0)),
            advances=int(state.get("advances", 0)),
            max_advances=int(getattr(self._mem, "hobby_max_advances", 12)),
        ):
            return "rotate", True
        if not self._force_advance and not self._advance_due(now, state):
            return None, False
        milestone = hobby_mod.is_milestone(
            advances=int(state.get("advances", 0)) + 1,
            every=int(getattr(self._mem, "hobby_milestone_every", 3)),
        )
        return "advance", milestone

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent, "hobby_worker_enabled", True)):
            return {"skipped": True, "reason": "disabled"}

        now = _utcnow()
        move, _composes = self._next_move(now)
        # Consume the one-shots only now that the decision is made; the
        # probe above deliberately left them armed.
        self._force_rotate = False
        self._force_advance = False

        state = load_hobby(self._chat_db.kv_get)
        if move == "start" or state is None:
            return self._start_hobby(now)
        if move == "rotate":
            return self._rotate_hobby(now, state)
        if move is None:
            # Pace progress with a wall-clock floor so it doesn't climb
            # every idle tick — a hobby that advances 24x/day reads as
            # fake.
            return {"waiting": True, "label": state.get("label")}
        return self._advance_hobby(now, state)

    # ── transitions ──────────────────────────────────────────────────

    def _start_hobby(self, now: datetime) -> dict[str, Any]:
        proposal = self._seed_proposal(exclude_keys=(), exclude_kinds=())
        if proposal.kind == "reading":
            proposal = self._bind_start_to_room_book(proposal)
        state = hobby_mod.proposal_to_state(
            proposal, now_iso=now.isoformat(timespec="seconds"),
        )
        self._write(state)
        log.info(
            "hobby started: key=%s artifact=%s kind=%s",
            proposal.key, proposal.artifact, proposal.kind,
        )
        return {
            "started": True,
            "key": proposal.key,
            "label": proposal.label,
            "artifact": proposal.artifact,
        }

    def _rotate_hobby(
        self, now: datetime, state: dict[str, Any],
    ) -> dict[str, Any]:
        old_key = str(state.get("key") or "")
        old_kind = str(state.get("kind") or "")
        old_label = str(state.get("label") or "")
        history = load_hobby_history(self._chat_db.kv_get)
        history = hobby_mod.append_history(
            history, hobby_mod.history_entry(state),
        )
        self._write_history(history)

        recent = hobby_mod.recent_artifacts_of(history)
        current_artifact = str(state.get("artifact") or "")
        if current_artifact:
            recent = recent + (current_artifact,)

        proposal = self._compose_next_hobby(
            leaving=state, recent_artifacts=recent,
        )
        if proposal is None:
            proposal = self._seed_proposal(
                exclude_keys=(old_key,),
                exclude_kinds=(old_kind,) if old_kind else (),
                exclude_artifacts=recent,
            )

        if proposal.kind == "reading":
            self._stamp_reading_onto_book(proposal)
        # Drifting off reading parks the paperback; do not wipe it.

        new_state = hobby_mod.proposal_to_state(
            proposal, now_iso=now.isoformat(timespec="seconds"),
        )
        self._write(new_state)

        seed = self._compose_rotation_seed(state, proposal)
        if seed:
            self._emit_seed(now, old_label or proposal.label, seed)
        self._note_wrapup(now, state, old_label, seed)
        log.info(
            "hobby rotated: from=%s to=%s artifact=%s kind=%s",
            old_key, proposal.key, proposal.artifact, proposal.kind,
        )
        return {
            "rotated": True,
            "from": old_key,
            "to": proposal.key,
            "artifact": proposal.artifact,
        }

    def _advance_hobby(
        self, now: datetime, state: dict[str, Any],
    ) -> dict[str, Any]:
        if str(state.get("kind") or "") == "reading":
            self._heal_room_book(state)
        state["progress"] = int(state.get("progress", 0)) + 1
        state["advances"] = int(state.get("advances", 0)) + 1
        state["last_advanced_at"] = now.isoformat(timespec="seconds")
        self._write(state)

        every = int(getattr(self._mem, "hobby_milestone_every", 3))
        emitted_seed = None
        if hobby_mod.is_milestone(
            advances=int(state["advances"]), every=every,
        ):
            seed = self._compose_milestone_seed(state)
            if seed:
                self._emit_seed(now, str(state.get("label") or ""), seed)
                emitted_seed = seed
            self._note_milestone(now, state, seed)

        log.info(
            "hobby advanced: key=%s progress=%d advances=%d milestone=%s",
            state.get("key"),
            state["progress"],
            state["advances"],
            bool(emitted_seed),
        )
        return {
            "advanced": True,
            "key": state.get("key"),
            "progress": state["progress"],
            "seed": emitted_seed,
        }

    # ── next-hobby pick ───────────────────────────────────────────────

    def _seed_proposal(
        self,
        *,
        exclude_keys: tuple[str, ...],
        exclude_kinds: tuple[str, ...],
        exclude_artifacts: tuple[str, ...] = (),
    ) -> hobby_mod.HobbyProposal:
        tpl = hobby_mod.pick_hobby(
            self._rng, exclude=exclude_keys, exclude_kinds=exclude_kinds,
        )
        return hobby_mod.proposal_from_template(
            tpl,
            self._rng,
            exclude_artifacts=exclude_artifacts,
            plant_name=self._live_plant_name(),
        )

    def _compose_next_hobby(
        self,
        *,
        leaving: dict[str, Any],
        recent_artifacts: tuple[str, ...],
    ) -> hobby_mod.HobbyProposal | None:
        raw = self._compose_next_llm(leaving, recent_artifacts)
        leaving_kind = str(leaving.get("kind") or "")
        return hobby_mod.admit_proposal(
            raw,
            leaving_kind=leaving_kind,
            recent_artifacts=recent_artifacts,
        )

    def _compose_next_llm(
        self,
        leaving: dict[str, Any],
        recent_artifacts: tuple[str, ...],
    ) -> dict[str, Any] | None:
        if self._ollama is None or not self._model:
            return None
        leaving_kind = str(leaving.get("kind") or "reading")
        other_kinds = [k for k in hobby_mod.HOBBY_KINDS if k != leaving_kind]
        kinds_line = ", ".join(other_kinds) or ", ".join(hobby_mod.HOBBY_KINDS)
        wrapped = hobby_mod.artifact_phrase(leaving)
        recent_line = ", ".join(
            a for a in recent_artifacts if a
        ) or "(none yet)"
        items_line = self._room_items_line()
        prompt = (
            "You are inventing Aiko's next personal project — one named "
            "thing she will keep up in her own time for a few days. She "
            f"is wrapping {wrapped} (kind: {leaving_kind}). She is allowed "
            "to leave it unfinished; a paperback stays on the shelf with "
            "its chapter count, a sketch stays in the book. Tonight's "
            "standing project must drift to a *different kind*: one of "
            f"{kinds_line}. Do not propose another {leaving_kind} thread.\n"
            f"Things actually in her room: {items_line}. Ground the "
            "project in those, her own head, or the garden — no new "
            "furniture.\n"
            f"She has already started: {recent_line}. Do not repeat those "
            "artifacts. A rare return to an old one is allowed only if "
            "the kind still drifts.\n"
            "Reply with JSON only:\n"
            '{"key": "<short_slug>", "kind": "<one of the kinds above>", '
            '"unit": "<chapter|session|sketch|recipe|check|record|lesson>", '
            '"artifact": "<the specific named thing, a title or subject, '
            'never a genre like a sci-fi book or just sketching>", '
            '"artifact_detail": "<one-line blurb>", '
            '"takeaway_hint": "<what a later thought might riff on>"}'
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": "Reply with a single JSON object, nothing else.",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.9, "num_predict": 180},
                format_json=True,
                surface="hobby_next",
            )
        except Exception:
            log.debug("hobby next compose failed", exc_info=True)
            return None
        return _parse_json_object(content)

    # ── room book ────────────────────────────────────────────────────

    def _book_item(self) -> Any | None:
        if self._world is None:
            return None
        try:
            items = self._world.list_items()
        except Exception:
            log.debug("hobby list_items failed", exc_info=True)
            return None
        return next(
            (i for i in items if (getattr(i, "slug", "") or "") == BOOK_SLUG),
            None,
        )

    def _bind_start_to_room_book(
        self, proposal: hobby_mod.HobbyProposal,
    ) -> hobby_mod.HobbyProposal:
        """Cold-start reading uses the paperback already in the room."""
        item = self._book_item()
        if item is None:
            return proposal
        titled = ensure_book_titled(
            getattr(item, "state", None) or {}, self._rng,
        )
        self._write_book(item, titled)
        title = str(titled.get("title") or "").strip()
        if not title:
            return proposal
        return hobby_mod.HobbyProposal(
            key=proposal.key,
            label=hobby_mod.standing_label("reading", title, proposal.label),
            kind="reading",
            unit=proposal.unit,
            artifact=title,
            artifact_detail=str(titled.get("blurb") or proposal.artifact_detail),
            takeaway_hint=proposal.takeaway_hint,
        )

    def _stamp_reading_onto_book(
        self, proposal: hobby_mod.HobbyProposal,
    ) -> None:
        """Invented (or returned) reading writes the paperback."""
        item = self._book_item()
        if item is None:
            return
        old_raw = str((getattr(item, "state", None) or {}).get("title") or "").strip()
        old_title = "" if is_generic_book_title(old_raw) else old_raw
        same = (
            bool(old_title)
            and old_title.lower() == proposal.artifact.strip().lower()
        )
        new_state = stamp_book_title(
            getattr(item, "state", None) or {},
            proposal.artifact,
            proposal.artifact_detail,
            self._rng,
            reset_progress=not same,
        )
        self._write_book(item, new_state)

    def _heal_room_book(self, state: dict[str, Any]) -> None:
        """Give a live untitled paperback a real title; keep chapters."""
        item = self._book_item()
        if item is None:
            return
        titled = ensure_book_titled(
            getattr(item, "state", None) or {}, self._rng,
        )
        self._write_book(item, titled)
        title = str(titled.get("title") or "").strip()
        if title and (
            not str(state.get("artifact") or "").strip()
            or hobby_mod.is_genre_artifact(str(state.get("artifact") or ""))
        ):
            state["artifact"] = title
            state["artifact_detail"] = str(
                titled.get("blurb") or state.get("artifact_detail") or ""
            )
            state["label"] = hobby_mod.standing_label(
                "reading", title, str(state.get("label") or ""),
            )

    def _write_book(self, item: Any, new_state: dict[str, Any]) -> None:
        if self._world is None:
            return
        title = str(new_state.get("title") or "").strip()
        blurb = str(new_state.get("blurb") or "").strip()
        try:
            self._world.update_item(
                int(item.id),
                name=title or None,
                description=blurb or None,
                state=new_state,
            )
        except Exception:
            log.debug("hobby book stamp failed", exc_info=True)

    def _live_plant_name(self) -> str:
        if self._world is None:
            return ""
        try:
            plants = self._world.list_items(kind="plant")
        except TypeError:
            try:
                plants = [
                    i for i in self._world.list_items()
                    if (getattr(i, "kind", "") or "") == "plant"
                ]
            except Exception:
                return ""
        except Exception:
            return ""
        for plant in plants:
            name = str(getattr(plant, "name", "") or "").strip()
            if name:
                return name
        return ""

    def _room_items_line(self) -> str:
        if self._world is None:
            return "(nothing notable)"
        try:
            from app.core.world.beat_detail import describe_items_for_prompt
            items = list(self._world.list_items())
            return describe_items_for_prompt(items, now=_utcnow())
        except Exception:
            log.debug("hobby room describe failed", exc_info=True)
            return "(nothing notable)"

    # ── helpers ───────────────────────────────────────────────────────

    def _advance_due(self, now: datetime, state: dict[str, Any]) -> bool:
        min_hours = float(getattr(self._mem, "hobby_advance_min_hours", 6.0))
        if min_hours <= 0:
            return True
        last = state.get("last_advanced_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(str(last))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        return (now - last_dt).total_seconds() >= min_hours * 3600.0

    def _emit_seed(self, now: datetime, label: str, seed: str) -> None:
        append_idle_seed(
            self._chat_db.kv_get,
            self._chat_db.kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "activity": label,
                "key": "hobby",
                "seed": seed,
            },
            max_entries=self._idle_seed_max_ring,
        )

    # ── K85b: leave something behind ─────────────────────────────────

    def _note_milestone(
        self, now: datetime, state: dict[str, Any], seed: str | None,
    ) -> None:
        """Keep the milestone past the day its seed is spent."""
        label = str(state.get("label") or "").strip()
        if self._pursuit_notes is None or not label:
            return
        line = f"{self._progress_phrase(state)} into {label}."
        if seed:
            line = f"{line} {seed}"
        self._pursuit_notes.write(
            line,
            source="hobby_milestone",
            topic=str(state.get("key") or ""),
            at=now,
            extra={"advances": int(state.get("advances", 0))},
        )

    def _note_wrapup(
        self,
        now: datetime,
        state: dict[str, Any],
        old_label: str,
        seed: str | None,
    ) -> None:
        """Keep the finished thread, which rotation is about to delete."""
        label = (old_label or "").strip()
        if self._pursuit_notes is None or not label:
            return
        line = (
            f"Wrapped up {label} after {self._progress_phrase(state)}."
        )
        if seed:
            line = f"{line} {seed}"
        self._pursuit_notes.write(
            line,
            source="hobby_wrapup",
            topic=str(state.get("key") or ""),
            at=now,
            extra={"advances": int(state.get("advances", 0))},
        )

    @staticmethod
    def _progress_phrase(state: dict[str, Any]) -> str:
        """``"9 chapters"`` — the unit pluralised against the count."""
        progress = max(0, int(state.get("progress", 0) or 0))
        unit = str(state.get("unit") or "step").strip() or "step"
        plural = unit if progress == 1 else unit + "s"
        return f"{progress} {plural}"

    def _compose_milestone_seed(self, state: dict[str, Any]) -> str | None:
        named = hobby_mod.artifact_phrase(state)
        label = str(state.get("label") or "your project")
        progress = int(state.get("progress", 0))
        unit = str(state.get("unit") or "step")
        context = (
            f"You've been {label} for a while now ({progress} "
            f"{unit}s in). The latest bit touched on {named}."
        )
        return self._compose_seed_llm(context)

    def _compose_rotation_seed(
        self,
        old_state: dict[str, Any],
        new_proposal: hobby_mod.HobbyProposal,
    ) -> str | None:
        old_named = hobby_mod.artifact_phrase(old_state)
        context = (
            f"You just wrapped up {old_named} and you're starting something "
            f"new: {new_proposal.label} — specifically {new_proposal.artifact}."
        )
        return self._compose_seed_llm(context)

    def _compose_seed_llm(self, context: str) -> str | None:
        if self._ollama is None or not self._model:
            return None
        try:
            name = self._user_display_name_provider() or "you"
        except Exception:
            name = "you"
        system = (
            "You are Aiko's quiet inner voice, reflecting on a hobby you've "
            f"been keeping up in your own time. {context} In ONE short "
            "sentence (max ~20 words), write a single forward-looking "
            "thought, small question, or budding opinion this sparked that "
            f"you might bring up to {name} later. First person, casual, "
            "specific. No greeting, no quotes, no preamble. Return JSON "
            '{"seed": "<the thought>"}.'
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "Give me the thought."},
                ],
                model=self._model,
                options={"temperature": 0.9, "num_predict": 80},
                format_json=True,
                surface="hobby_seed",
            )
        except Exception:
            log.debug("hobby seed compose failed", exc_info=True)
            return None
        blob = _parse_json_object(content)
        if blob is None:
            return None
        seed = str(blob.get("seed") or "").strip()
        return seed[:240] or None

    def _write(self, state: dict[str, Any]) -> None:
        try:
            self._chat_db.kv_set(KV_CURRENT_HOBBY, json.dumps(state))
        except Exception:
            log.debug("hobby state write failed", exc_info=True)

    def _write_history(self, ring: list[dict[str, Any]]) -> None:
        try:
            self._chat_db.kv_set(KV_HOBBY_CATALOGUE, json.dumps(ring))
        except Exception:
            log.debug("hobby history write failed", exc_info=True)


__all__ = [
    "HobbyWorker",
    "load_hobby",
    "load_hobby_history",
    "KV_CURRENT_HOBBY",
    "KV_HOBBY_CATALOGUE",
]
