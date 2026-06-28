"""HobbyWorker — H19, Aiko's ongoing personal project across days.

An :class:`IdleWorker` that maintains a single *current hobby* (a multi-day
thread Aiko returns to in her idle time) and advances it slowly during quiet
windows. Unlike the one-off away-beats (H13/H14) the hobby has continuity:
its progress counter climbs across days, it occasionally yields a *takeaway*
(surfaced through the shared H17 idle-seed cue so Aiko phrases it herself),
and it rotates to a fresh hobby once it's run long enough.

State lives in one ``kv_meta`` JSON blob (``aiko.current_hobby``); the
deterministic catalogue + progress / milestone / rotation math lives in the
pure :mod:`app.core.world.hobby` module. The standing "what she's been up
to" line is rendered by ``_render_hobby_block`` in the inner-life mixin.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready
from app.core.world import hobby as hobby_mod
from app.core.world.idle_activity_worker import append_idle_seed

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.hobby_worker")


# Single ``kv_meta`` JSON blob, namespaced under ``aiko.*`` alongside the
# other idle-life state (day_color, vulnerability_budget, idle_seeds).
KV_CURRENT_HOBBY = "aiko.current_hobby"


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
    if not isinstance(blob, dict) or not blob.get("label"):
        return None
    return blob


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        rng: random.Random | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._agent = agent_settings
        self._mem = memory_settings
        self._user_display_name_provider = user_display_name_provider
        self._ollama = ollama
        self._model = model
        self._idle_seed_max_ring = max(1, int(idle_seed_max_ring))
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
        if not bool(getattr(self._agent, "hobby_worker_enabled", True)):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent, "hobby_worker_enabled", True)):
            return {"skipped": True, "reason": "disabled"}

        now = _utcnow()
        state = load_hobby(self._chat_db.kv_get)

        # No hobby yet → start one.
        if state is None:
            return self._start_hobby(now)

        # Rotate if it's run long enough (or forced).
        max_advances = int(
            getattr(self._mem, "hobby_max_advances", 12)
        )
        force_rotate = self._force_rotate
        self._force_rotate = False
        if force_rotate or hobby_mod.should_rotate(
            progress=int(state.get("progress", 0)),
            advances=int(state.get("advances", 0)),
            max_advances=max_advances,
        ):
            return self._rotate_hobby(now, state)

        # Pace progress with a wall-clock floor so it doesn't climb every
        # idle tick — a hobby that advances 24×/day reads as fake.
        force_advance = self._force_advance
        self._force_advance = False
        if not force_advance and not self._advance_due(now, state):
            return {"waiting": True, "label": state.get("label")}

        return self._advance_hobby(now, state)

    # ── transitions ──────────────────────────────────────────────────

    def _start_hobby(self, now: datetime) -> dict[str, Any]:
        tpl = hobby_mod.pick_hobby(self._rng)
        state = {
            "key": tpl.key,
            "label": tpl.label,
            "kind": tpl.kind,
            "unit": tpl.unit,
            "progress": 0,
            "advances": 0,
            "started_at": now.isoformat(timespec="seconds"),
            "last_advanced_at": None,
        }
        self._write(state)
        log.info("hobby started: key=%s label=%s", tpl.key, tpl.label)
        return {"started": True, "key": tpl.key, "label": tpl.label}

    def _rotate_hobby(
        self, now: datetime, state: dict[str, Any],
    ) -> dict[str, Any]:
        old_label = str(state.get("label") or "")
        old_key = str(state.get("key") or "")
        tpl = hobby_mod.pick_hobby(self._rng, exclude=(old_key,))
        new_state = {
            "key": tpl.key,
            "label": tpl.label,
            "kind": tpl.kind,
            "unit": tpl.unit,
            "progress": 0,
            "advances": 0,
            "started_at": now.isoformat(timespec="seconds"),
            "last_advanced_at": None,
        }
        self._write(new_state)
        # Wrapping up a thread is a great seed: "finally finished X, picked
        # up Y". Compose it (best-effort) and surface via the H17 cue.
        seed = self._compose_rotation_seed(old_label, tpl.label)
        if seed:
            self._emit_seed(now, old_label, seed)
        log.info(
            "hobby rotated: from=%s to=%s", old_key, tpl.key,
        )
        return {"rotated": True, "from": old_key, "to": tpl.key}

    def _advance_hobby(
        self, now: datetime, state: dict[str, Any],
    ) -> dict[str, Any]:
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

    def _compose_milestone_seed(self, state: dict[str, Any]) -> str | None:
        tpl = hobby_mod.template_for(str(state.get("key") or ""))
        hint = tpl.takeaway_hint if tpl else "what you've been working on"
        label = str(state.get("label") or "your project")
        progress = int(state.get("progress", 0))
        context = (
            f"You've been {label} for a while now ({progress} "
            f"{state.get('unit', 'step')}s in). The latest bit touched on: "
            f"{hint}."
        )
        return self._compose_seed_llm(context)

    def _compose_rotation_seed(
        self, old_label: str, new_label: str,
    ) -> str | None:
        context = (
            f"You just wrapped up {old_label} and you're starting something "
            f"new: {new_label}."
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
        if not content:
            return None
        try:
            blob = json.loads(content)
        except Exception:
            return None
        seed = ""
        if isinstance(blob, dict):
            seed = str(blob.get("seed") or "").strip()
        return seed[:240] or None

    def _write(self, state: dict[str, Any]) -> None:
        try:
            self._chat_db.kv_set(KV_CURRENT_HOBBY, json.dumps(state))
        except Exception:
            log.debug("hobby state write failed", exc_info=True)


__all__ = ["HobbyWorker", "load_hobby", "KV_CURRENT_HOBBY"]
