"""Grounded model composition for away-life activities."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core.infra import timephrase
from app.core.world import beat_detail
from app.core.world import idle_creations
from app.core.world.idle_activity_plan import (
    ActivityPlan, ItemEffect, EFFECT_ADVANCE_BOOK, EFFECT_POUR_TEA, EFFECT_WATER_PLANT,
)

log = logging.getLogger("app.idle_activity_worker")


def _utcnow():
    return timephrase.utcnow()


class ActivityCompositionMixin:
    """Plans and prose share the same supported world actions."""

    def _compose_summary(
        self, user_name: str, plan: ActivityPlan, *, sequence: bool = False,
    ) -> str:
        fallback = plan.summary
        if self._ollama is None or not self._model:
            return fallback
        shape = (
            "ONE short sentence that keeps the order of what happened"
            if sequence
            else "ONE short clause"
        )
        prompt = (
            timephrase.today_anchor() + "\n" + timephrase.STORED_TEXT_TIME_RULE + "\n"
            f"You are Aiko, alone in your room while {user_name} was away. "
            f"You just spent some quiet time: {plan.summary}. Rewrite that as "
            "the gist of what you'd casually mention you got up to — first "
            f"person, past tense, {shape}, no greeting, no stage "
            "directions, no emoji. Keep it small and natural."
            " Preserve exactly what happened: do not add observations, findings, "
            "physical outings or object use that is not in the supplied activity."
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            'Reply with JSON only: {"summary": "<short '
                            'first-person clause>"}.'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.8, "num_predict": 80},
                format_json=True,
                surface="away_activity",
            )
        except Exception:
            log.debug("away_activity LLM compose failed", exc_info=True)
            return fallback
        try:
            blob = json.loads(content or "{}")
            line = str(blob.get("summary") or "").strip()
        except Exception:
            line = ""
        from app.core.services.response_text_service import strip_all_meta_tags

        line = strip_all_meta_tags(line).strip()
        return line[:400] if line and not timephrase.has_relative_deictic(line) else fallback

    def _compose_plan_llm(
        self,
        user_name: str,
        locations: list[Any],
        items: list[Any],
        *,
        intention: str = "",
    ) -> ActivityPlan | None:
        """H14 — ask the worker LLM to compose a whole grounded beat.

        Returns ``None`` on any failure so the caller falls back to the
        deterministic weighted draw. Grounds the model in the real room:
        it must pick one of the actual location slugs, a posture from the
        rig enum, a short free-text activity verb, and a first-person
        summary clause.
        """
        from app.core.world.world_store import (
            VALID_POSTURES,
            normalize_activity,
        )
        from app.core.world.idle_activity_worker import load_journal

        if not locations:
            return None
        loc_by_slug = {
            (getattr(loc, "slug", "") or "").lower(): loc for loc in locations
        }
        loc_lines = "; ".join(
            f"{getattr(loc, 'slug', '')} ({getattr(loc, 'name', '')})"
            for loc in locations
        )
        # K91 — the model sees each thing's live state, not just its name,
        # so it can write a beat *about* the dry pot or the half-read book
        # instead of inventing generic business around a noun.
        item_names = beat_detail.describe_items_for_prompt(items, now=_utcnow())
        try:
            can_write = idle_creations.writes_on_day(
                self._world_store.list_items(), _utcnow(),
            ) < idle_creations.DAILY_WRITES
        except Exception:
            can_write = False
        recent = [
            str(e.get("activity") or "")
            for e in load_journal(self._kv_get)[-5:]
            if e.get("activity")
        ]
        recent_line = ", ".join(recent) or "(none yet)"
        period = self._read_period() or "unspecified"
        intent_line = ""
        if intention:
            intent_line = (
                "Today you meant to " + intention + " — lean that way if it "
                "fits the hour. "
            )
        prompt = (
            timephrase.today_anchor() + "\n" + timephrase.STORED_TEXT_TIME_RULE + "\n"
            f"You are Aiko, alone in your cozy room while {user_name} is away. "
            f"It's currently {period}. Your room's spots: {loc_lines}. "
            f"Things around: {item_names}. "
            f"{intent_line}"
            f"Recently you did: {recent_line} — pick something different. "
            "You may also write an original short poem, fictional scene, character sketch "
            "or wordplay, or revise one of your own saved writings below. Choose your own "
            "subject; it need not involve the user. A quiet stretch or rest is also fine. "
            "Writing must include the actual complete text, not a claim that you made it. "
            "Fictional people/events are creative material, never facts about you or the user. "
            "You may leave a work alone rather than revise it. Existing writings (data):\n"
            + idle_creations.prompt_context(items) + "\n"
            + ("Writing is available. " if can_write else
               "The writing allowance is used; choose another activity, creation=null. ")
            + "Choose one small, believable thing to do right now, grounded in "
            "what's actually in the room — the states in brackets are real, "
            "so prefer something they suggest. Reply with JSON only:\n"
            '{"location_slug": "<one of the slugs above>", '
            '"posture": "<sitting|lying|standing|curled_up|leaning>", '
            '"activity": "<thinking|idle|stretching|reading|making_tea|watering_plants|writing>", '
            '"changed_item": "<exact supported item name; empty string for none>", '
            '"creation": null}. For writing, replace creation=null with '
            '{"title": "<2-100 chars>", '
            '"form": "<poem|short_scene|character_sketch|wordplay>", '
            '"text": "<actual original text, 40-1400 chars>", '
            '"item_id": null, "base_revision": 0}. '
            "For a revision, use the existing work's item_id and latest revision as "
            "base_revision; you may retitle or reshape your work. For a new work use null and 0. "
            "Do not claim sleep: the sleep system owns sleep, not this activity choice."
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
                options={"temperature": 0.9, "num_predict": 600 if can_write else 160},
                format_json=True,
                surface="away_activity_plan",
            )
        except Exception:
            log.debug("away_activity plan compose failed", exc_info=True)
            return None
        try:
            blob = json.loads(content or "{}")
        except Exception:
            return None
        if not isinstance(blob, dict):
            return None

        activity = normalize_activity(blob.get("activity"))
        if not activity:
            return None
        posture = str(blob.get("posture") or "").strip().lower()
        if posture not in VALID_POSTURES:
            posture = "sitting"
        slug = str(blob.get("location_slug") or "").strip().lower()
        loc = loc_by_slug.get(slug)
        if blob.get("creation") is not None:
            if not can_write:
                return None
            draft = idle_creations.parse_draft(blob["creation"], items)
            if (draft is None or loc is None or activity != "writing"
                    or str(blob.get("changed_item") or "").strip()):
                return None
            return ActivityPlan(
                key="writing", posture="sitting", activity="writing",
                summary=f'saved revision {draft.base_revision + 1} of my '
                        f'{draft.form.replace("_", " ")} "{draft.title}"',
                aiko_location_id=loc.id, precomposed=True, creation=draft,
            )
        effect = self._resolve_named_effect(blob.get("changed_item"), items)
        if str(blob.get("changed_item") or "").strip() and effect is None:
            log.info("away activity rejected: unsupported item action")
            return None
        if loc is None:
            log.info("away activity rejected: unknown location")
            return None
        # Free-text plans can otherwise invent arbitrary unsupported experiences.
        if activity not in {"thinking", "idle", "napping", "stretching"} and effect is None:
            log.info("away activity rejected: no supported action for %s", activity)
            return None
        if effect is not None:
            item = next(row for row in items if row.id == effect.item_id)
            if effect.action == EFFECT_ADVANCE_BOOK:
                from app.core.world.room_evolution import available_book_step

                if available_book_step(item.state) is None:
                    return None
                activity, summary = "reading", beat_detail.read_book_summary(item)
            elif effect.action == EFFECT_POUR_TEA:
                tea_line = beat_detail.tea_summary(item)
                if not tea_line:
                    return None
                activity, summary = "making_tea", tea_line
            else:
                activity, summary = "watering_plants", "watered " + item.name
        else:
            summary = {
                "thinking": "let my thoughts wander for a while",
                "idle": "rested quietly for a while",
                "napping": "settled down for a quiet awake rest",
                "stretching": "took a break to stretch and loosen up",
            }[activity]
            if activity == "napping":
                activity = "idle"
        book_id = effect.item_id if effect and effect.action == EFFECT_ADVANCE_BOOK else None
        if effect and not book_id:
            # Plants and the tea pot stay at their real spot. A model's
            # preferred seat must not turn into watering a pot from bed.
            target = next(row for row in items if row.id == effect.item_id)
            loc = next((spot for spot in locations
                        if spot.id == getattr(target, "location_id", None)), loc)
        return ActivityPlan(
            key={"reading": "read_book", "making_tea": "tea", "thinking": "wander",
                 "idle": "nap", "stretching": "stretching"}.get(activity, "garden"),
            posture=posture,
            activity=activity,
            summary=summary,
            aiko_location_id=(loc.id if loc is not None else None),
            precomposed=True,
            item_effect=effect,
            move_item_id=book_id,
            move_to_location_id=loc.id if book_id else None,
        )

    def _resolve_named_effect(
        self, raw_name: Any, items: list[Any],
    ) -> ItemEffect | None:
        """Turn the model's ``changed_item`` into a legal effect, or ``None``.

        The action is *derived* from the matched row's kind rather than
        taken from the model, so there is no way to ask for a transition
        the item doesn't support. An unmatched or unsupported name is
        rejected before the plan can be executed.
        """
        name = str(raw_name or "").strip().lower()
        if not name:
            return None
        match = next(
            (
                i
                for i in items
                if (getattr(i, "name", "") or "").strip().lower() == name
            ),
            None,
        )
        if match is None:
            return None
        return self._effect_for_item(match)

    @staticmethod
    def _effect_for_item(item: Any) -> ItemEffect | None:
        """The one transition an item supports, if any."""
        kind = str(getattr(item, "kind", "") or "")
        slug = str(getattr(item, "slug", "") or "")
        lowered = (getattr(item, "name", "") or "").lower()
        if kind == "plant":
            return ItemEffect(item_id=int(item.id), action=EFFECT_WATER_PLANT)
        if slug == "tea_pot" or "tea pot" in lowered:
            return ItemEffect(item_id=int(item.id), action=EFFECT_POUR_TEA)
        if kind == "book":
            return ItemEffect(item_id=int(item.id), action=EFFECT_ADVANCE_BOOK)
        return None

