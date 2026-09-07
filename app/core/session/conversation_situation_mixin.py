"""Typed facade for present conversation/world situation awareness."""
from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Any

from app.core.conversation.conversation_situation import (
    ACTIVE,
    ConversationSituationSnapshot,
    ConversationSituationState,
    WorldSituation,
    state_age_seconds,
)
from app.core.infra import timephrase


_WORD_RE = re.compile(r"[a-z0-9]+")
_PLACE_NOISE = frozenset(
    {"a", "an", "at", "by", "in", "near", "on", "our", "the", "their", "your"}
)
_ACTIVITY_NOISE = frozenset(
    {"a", "an", "and", "are", "is", "the", "to", "together", "user", "with"}
)


def _tokens(value: str, noise: frozenset[str]) -> set[str]:
    return {token for token in _WORD_RE.findall((value or "").lower()) if token not in noise}


def _text_matches(reference: str, candidates: tuple[str, ...], *, activity: bool) -> bool:
    noise = _ACTIVITY_NOISE if activity else _PLACE_NOISE
    reference_tokens = _tokens(reference, noise)
    if not reference_tokens:
        return False
    candidate_tokens: set[str] = set()
    for candidate in candidates:
        candidate_tokens.update(_tokens(candidate, noise))
    return bool(reference_tokens & candidate_tokens)


class ConversationSituationMixin:
    """Assemble one authoritative snapshot for prompts, debug, and guards."""

    def conversation_situation_snapshot(
        self,
        *,
        user_text: str = "",
        input_mode: str | None = None,
        fresh: bool = False,
    ) -> ConversationSituationSnapshot:
        session_id = self.session_key
        mode_raw = str(input_mode or getattr(self, "_last_turn_mode", "typed") or "typed")
        normalized_mode = "typed" if mode_raw == "typed" else "voice"
        cache_key = (
            int(
                getattr(
                    getattr(self, "_prompt_assembler", None),
                    "_assembly_seq",
                    -1,
                )
            ),
            session_id,
            normalized_mode,
        )
        dialogue_act = ""
        if user_text:
            try:
                from app.core.conversation.dialogue_act_tagger import tag_regex

                dialogue_act = tag_regex(user_text).act
            except Exception:
                dialogue_act = ""
        cached = getattr(self, "_conversation_situation_snapshot_cache", None)
        if not fresh and cached is not None and cached[0] == cache_key:
            snapshot = cached[1]
            if dialogue_act and snapshot.dialogue_act != dialogue_act:
                snapshot = replace(snapshot, dialogue_act=dialogue_act)
                self._conversation_situation_snapshot_cache = (cache_key, snapshot)
            return snapshot

        state = self._read_conversation_situation(session_id)
        world = self._conversation_world_situation()
        world_compatible, conflict_reason = self._situation_world_compatibility(
            state, world
        )
        age = state_age_seconds(state)
        stale_seconds = max(
            300.0,
            float(
                getattr(
                    self._settings.agent,
                    "conversation_situation_stale_seconds",
                    21600.0,
                )
            ),
        )
        inferred_stale = age is None or age > stale_seconds
        shared_commitment_active = bool(
            state is not None
            and state.status == ACTIVE
            and state.shared
            and not inferred_stale
            and world_compatible
        )

        arc = ""
        arc_confidence = 0.0
        arc_store = getattr(self, "_arc_store", None)
        if arc_store is not None:
            try:
                arc_state = arc_store.get(self._user_id)
                if arc_state is not None:
                    arc = str(arc_state.arc)
                    arc_confidence = float(arc_state.confidence)
            except Exception:
                pass

        mood_label = ""
        try:
            affect = self._affect_store.get(self._user_id)
            mood_label = str(getattr(affect, "mood_label", "") or "")
        except Exception:
            pass
        vitality_band = ""
        try:
            vitality_band = str(self.vitality_snapshot().get("band") or "")
        except Exception:
            pass

        last_activity = float(getattr(self, "_last_user_activity_at", 0.0) or 0.0)
        elapsed_ms = (
            max(0, int((time.monotonic() - last_activity) * 1000.0))
            if last_activity > 0
            else 0
        )
        snapshot = ConversationSituationSnapshot(
            session_id=session_id,
            generation=int(state.generation if state is not None else 0),
            observed_at=timephrase.utcnow().isoformat(timespec="seconds"),
            input_mode=normalized_mode,
            floor_transition=(
                "user_to_aiko"
                if bool(getattr(self, "_turn_in_progress", False))
                else "neither"
            ),
            dialogue_act=dialogue_act,
            arc=arc,
            arc_confidence=arc_confidence,
            mood_label=mood_label,
            vitality_band=vitality_band,
            user_present=int(getattr(self, "_connected_clients", 0) or 0) > 0,
            user_active_app=(
                str(getattr(self, "_user_active_app", "") or "")
                if bool(
                    getattr(self._settings.agent, "activity_awareness_enabled", False)
                )
                else ""
            ),
            world=world,
            inferred=state,
            inferred_stale=inferred_stale,
            world_compatible=world_compatible,
            conflict_reason=conflict_reason,
            shared_commitment_active=shared_commitment_active,
            since_user_activity_ms=elapsed_ms,
        )
        self._conversation_situation_snapshot_cache = (cache_key, snapshot)
        return snapshot

    def _read_conversation_situation(
        self, session_id: str
    ) -> ConversationSituationState | None:
        store = getattr(self, "_conversation_situation_store", None)
        if store is None:
            return None
        try:
            return store.get(session_id)
        except Exception:
            return None

    def _conversation_world_situation(self) -> WorldSituation:
        store = getattr(self, "_world_store", None)
        if store is None:
            return WorldSituation()
        try:
            state = store.get_state()
            location = (
                store.get_location_by_id(state.location_id)
                if state.location_id is not None
                else None
            )
            scene = store.current_scene()
            return WorldSituation(
                scene_id=state.scene_id,
                scene_name=str(getattr(scene, "name", "") or ""),
                location_id=state.location_id,
                location_slug=str(getattr(location, "slug", "") or ""),
                location_name=str(getattr(location, "name", "") or ""),
                posture=str(state.posture or ""),
                activity=str(state.activity or ""),
                updated_at=str(state.updated_at or ""),
            )
        except Exception:
            return WorldSituation()

    @staticmethod
    def _situation_world_compatibility(
        state: ConversationSituationState | None,
        world: WorldSituation,
    ) -> tuple[bool, str]:
        if state is None or state.status != ACTIVE:
            return False, "no_active_situation"
        anchored = False
        if state.place_ref:
            if not _text_matches(
                state.place_ref,
                (world.location_slug, world.location_name, world.scene_name),
                activity=False,
            ):
                return False, "inferred_context_conflicts_with_world"
            anchored = True
        if state.aiko_activity and not _text_matches(
            state.aiko_activity,
            (world.activity, world.posture),
            activity=True,
        ):
            # ``idle`` / ``relaxing`` are world baselines, not proof that
            # Aiko is not also talking or watching something with the user.
            # A non-neutral activity is affirmative contradictory evidence.
            if (world.activity or "").strip().lower() not in {
                "",
                "idle",
                "relaxing",
            }:
                return False, "inferred_context_conflicts_with_world"
        elif state.aiko_activity:
            anchored = True
        if not anchored:
            return False, "no_world_anchor"
        return True, ""

    def _render_conversation_situation_block(self, user_text: str) -> str:
        snapshot = self.conversation_situation_snapshot(user_text=user_text)
        state = snapshot.inferred
        if state is None or snapshot.inferred_stale or state.status == "ended":
            return ""
        if not snapshot.world_compatible:
            return (
                "[Conversation situation]\n"
                f"A prior conversational reading was: {state.summary} "
                "It conflicts with the authoritative room state; do not present "
                "that reading as current."
            )
        shared = (
            f" Shared activity: {state.shared_activity}."
            if state.shared and state.shared_activity
            else ""
        )
        aiko_now = (
            f" In this context you are {state.aiko_activity}."
            if state.aiko_activity
            else ""
        )
        return (
            "[Conversation situation]\n"
            f"The conversation currently establishes: {state.summary}{shared}{aiko_now} "
            "Use this as present continuity without reciting it."
        )

    def _reconcile_conversation_situation_after_world_mutation(self) -> None:
        """Refresh a compatible lease or clear one a deliberate move ended."""
        store = getattr(self, "_conversation_situation_store", None)
        if store is None:
            return
        state = self._read_conversation_situation(self.session_key)
        if state is None or state.status != ACTIVE or not state.shared:
            return
        self._conversation_situation_snapshot_cache = None
        snapshot = self.conversation_situation_snapshot(fresh=True)
        if snapshot.world_compatible:
            store.upsert(
                replace(
                    state,
                    updated_at=timephrase.utcnow().isoformat(timespec="seconds"),
                    miss_count=0,
                )
            )
        else:
            store.clear(self.session_key)
        self._conversation_situation_snapshot_cache = None

    def _renew_conversation_situation_on_turn(
        self, user_message_id: int | None
    ) -> None:
        """Renew an active compatible lease without changing its evidence."""
        if user_message_id is None:
            return
        store = getattr(self, "_conversation_situation_store", None)
        state = self._read_conversation_situation(self.session_key)
        if store is None or state is None or state.status != ACTIVE:
            return
        snapshot = self.conversation_situation_snapshot(fresh=True)
        if not snapshot.world_compatible:
            return
        store.upsert(
            replace(
                state,
                source_message_id=max(state.source_message_id, int(user_message_id)),
                updated_at=timephrase.utcnow().isoformat(timespec="seconds"),
            )
        )
        self._conversation_situation_snapshot_cache = None

    def conversation_situation_diagnostics(self) -> dict[str, Any]:
        snapshot = self.conversation_situation_snapshot(fresh=True)
        worker = getattr(self, "_conversation_situation_worker", None)
        guard = getattr(self, "_world_mutation_guard", None)
        return {
            "snapshot": snapshot.to_payload(),
            "worker": (
                worker.stats(self.session_key) if worker is not None else {}
            ),
            "world_mutation_guard": (
                guard.last_decision() if guard is not None else {}
            ),
        }


__all__ = ["ConversationSituationMixin"]
