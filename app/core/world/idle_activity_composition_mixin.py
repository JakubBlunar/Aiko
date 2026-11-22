"""Grounded model composition for away-life activities."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core.infra import timephrase
from app.core.world.idle_life_trace import emit
from app.core.world.idle_life_context import IdleLifeContext
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

    @staticmethod
    def _reject_plan(reason: str) -> None:
        # Keep private model output out of retained diagnostics.
        emit("plan_rejected", {"reason": reason})
        log.info("away activity plan rejected: reason=%s", reason)
        return None

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
        context: IdleLifeContext | None = None,
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
        context = context or IdleLifeContext()
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
        available = getattr(self._world_store, "available_home_actions", None)
        home = available(now=_utcnow()) if callable(available) else {"actions": []}
        families: dict[str, list[dict[str, Any]]] = {}
        for action in home["actions"]:
            families.setdefault(action["action"], []).append(action)
        choices = [self._rng.choice(rows) for rows in families.values()]
        choices.extend(action for action in home["actions"]
                       if action["action"] == "prepare_drink" and action not in choices)
        home_actions = {action["token"]: action for action in choices[:24]}
        # Short selectors belong to this one prompt. Only its original state-bound
        # token travels with the plan to the executor.
        menu = {}
        for number, row in enumerate(home_actions.values(), 1):
            label = row["action"]
            if row.get("recipe"):
                label += "_" + row["recipe"]
            menu[f"home_{label}_{number}"] = row
        model_menu = [
            {"choice": choice, **{k: v for k, v in row.items() if k != "token"}}
            for choice, row in menu.items()
        ]
        home_context = (
            "\nSupported household capabilities: " + home.get("capabilities", "")
            + "\nAvailable household actions (data): "
            + json.dumps(model_menu, ensure_ascii=True)
            + '\nFor a household activity set "activity" to the exact choice from this menu. '
            "The choice already specifies the item, location "
            "and effect. Leave changed_item empty and creation null. "
            "For a drawing action, include drawing={title,strokes,note}. The title is "
            "2-80 characters and the optional note at most 240. Supply 1-24 strokes, each "
            "with points=[[x,y],...] (2-32 points), width=0.5-8, shade=0-3, filled=true "
            "or false. Coordinates stay within x=0-512 and y=0-320, at most 256 points "
            "total. Supply the actual strokes, not a description or markup. "
            "Other actions use drawing=null. Fictional "
            "references are authored material, not an actual visual observation. "
        ) if home_actions else ""
        intent_line = ""
        if intention:
            intent_line = (
                "Today you meant to " + intention + " — lean that way if it "
                "fits the hour. "
            )
        hobby_line = ""
        hobby_provider = getattr(self, "_hobby_provider", None)
        if hobby_provider is not None:
            try:
                hobby = str(hobby_provider() or "").strip()[:500]
                if hobby:
                    hobby_line = (
                        "An optional ongoing interest (not proof of completed practice): "
                        + hobby + ". Let it inform a choice when it fits; other subjects are fine. "
                    )
            except Exception:
                log.debug("away activity hobby context unavailable", exc_info=True)
        inspiration = context.render()
        emit("model_options", {
            "offered": [
                {"choice": choice, **{k: row[k] for k in ("token", "action", "item_id",
                                                          "location_id") if k in row}}
                for choice, row in menu.items()
            ],
            "household_actions_total": len(home["actions"]),
            "household_actions_omitted": max(0, len(home["actions"]) - len(home_actions)),
            "writing_available": can_write,
            "unavailable_hint": home.get("unavailable_hint", ""),
            "choice_inputs": {
                "version": 1,
                "optional_inspiration": bool(inspiration),
                "daily_intention": bool(intent_line),
                "ongoing_interest": bool(hobby_line),
            },
        })
        prompt = (
            timephrase.today_anchor() + "\n" + timephrase.STORED_TEXT_TIME_RULE + "\n"
            f"You are Aiko, alone in your cozy room while {user_name} is away. "
            f"It's currently {period}. Your room's spots: {loc_lines}. "
            f"Things around: {item_names}. {home_context}"
            f"{intent_line}{hobby_line}{inspiration}"
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
            "what's actually in the room. Return a JSON object. "
            + "Choose exactly one activity from the listed values. A household choice already "
            "specifies its target. Use changed_item only for a non-household item action.\n"
            '{"activity": "<' + "|".join([*menu, "thinking", "idle", "stretching", "reading",
                                         "making_tea", "watering_plants", "writing"]) + '>", '
            '"location_slug": "<one of the slugs above>", '
            '"posture": "<sitting|lying|standing|curled_up|leaning>", '
            '"changed_item": "<exact supported item name; empty string for none>", '
            '"creation": null, '
            '"choice_reason": "<own_interest|room_need|continue_work|rest|context|other>", '
            '"inspired_by": ["<optional offered source ref; empty if none>"]}. '
            "Source refs are optional attribution, not a request for private reasoning. "
            'For writing, replace creation=null with '
            '{"title": "<2-100 chars>", '
            '"form": "<poem|short_scene|character_sketch|wordplay>", '
            '"text": "<actual original text, 40-1400 chars>", '
            '"item_id": null, "base_revision": 0}. '
            "For a revision, use the existing work's item_id and latest revision as "
            "base_revision; you may retitle or reshape your work. For a new work use null and 0. "
            "Keep creation null for every activity except writing. For a quiet rest choose idle, "
            "thinking or stretching with changed_item empty. The sleep system owns sleep."
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
                options={"temperature": 0.9, "num_predict": (
                    1200 if any(row.get("parameters", {}).get("drawing_required")
                                for row in home_actions.values()) else 600 if can_write else 160
                )},
                format_json=True,
                surface="away_activity_plan",
            )
        except Exception:
            log.debug("away_activity plan compose failed", exc_info=True)
            return self._reject_plan("model_call_failed")
        try:
            blob = json.loads(content or "{}")
        except Exception:
            return self._reject_plan("invalid_json")
        if not isinstance(blob, dict):
            return self._reject_plan("invalid_object")
        context.attribution(blob)

        activity_choice = blob.get("activity")
        choice = (activity_choice if isinstance(activity_choice, str)
                  and activity_choice.startswith("home_") else None)
        token = blob.get("home_action_token")  # Older recorded model responses.
        if choice or token not in (None, ""):
            if choice and token not in (None, ""):
                return self._reject_plan("conflicting_household_selectors")
            selector = choice or token
            offered = menu if choice else home_actions
            action = offered.get(selector) if isinstance(selector, str) else None
            if action is None:
                return self._reject_plan("household_choice_not_offered")
            if blob.get("creation") is not None:
                return self._reject_plan("household_choice_with_creation")
            # Some models repeat the selected vessel's name in the generic
            # field. It names the same target and must never add a second effect.
            changed_item = blob.get("changed_item")
            if changed_item is not None and (
                not isinstance(changed_item, str)
                or changed_item.strip().casefold() not in ("", action["item"].casefold())
            ):
                return self._reject_plan("household_choice_with_other_item")
            drawing = blob.get("drawing")
            if action.get("parameters", {}).get("drawing_required"):
                from app.core.world.home_sketches import validate_drawing

                try:
                    drawing = validate_drawing(drawing)
                except ValueError:
                    return self._reject_plan("invalid_drawing")
            elif drawing is not None:
                return self._reject_plan("unexpected_drawing")
            return ActivityPlan(
                key="home_" + action["action"], posture="standing", activity=action["action"],
                summary=action["summary"], aiko_location_id=action["location_id"],
                home_action_token=action["token"], precomposed=True,
                home_action_drawing=drawing,
            )

        activity = normalize_activity(blob.get("activity"))
        if not activity:
            return self._reject_plan("unknown_activity")
        posture = str(blob.get("posture") or "").strip().lower()
        if posture not in VALID_POSTURES:
            posture = "sitting"
        slug = str(blob.get("location_slug") or "").strip().lower()
        loc = loc_by_slug.get(slug)
        if blob.get("creation") is not None:
            if not can_write:
                return self._reject_plan("writing_unavailable")
            draft = idle_creations.parse_draft(blob["creation"], items)
            if (draft is None or loc is None or activity != "writing"
                    or str(blob.get("changed_item") or "").strip()):
                return self._reject_plan("invalid_writing")
            return ActivityPlan(
                key="writing", posture="sitting", activity="writing",
                summary=f'saved revision {draft.base_revision + 1} of my '
                        f'{draft.form.replace("_", " ")} "{draft.title}"',
                aiko_location_id=loc.id, precomposed=True, creation=draft,
            )
        effect = self._resolve_named_effect(blob.get("changed_item"), items)
        if (effect is not None and effect.action == EFFECT_POUR_TEA
                and any("home_drink_station" in row.state for row in items)):
            return self._reject_plan("household_drink_requires_token")
        if str(blob.get("changed_item") or "").strip() and effect is None:
            return self._reject_plan("unsupported_item_action")
        if loc is None:
            return self._reject_plan("unknown_location")
        # Free-text plans can otherwise invent arbitrary unsupported experiences.
        if activity not in {"thinking", "idle", "napping", "stretching"} and effect is None:
            return self._reject_plan("missing_supported_action")
        if effect is not None:
            item = next(row for row in items if row.id == effect.item_id)
            if effect.action == EFFECT_ADVANCE_BOOK:
                from app.core.world.room_evolution import available_book_step

                if available_book_step(item.state) is None:
                    return self._reject_plan("book_step_unavailable")
                activity, summary = "reading", beat_detail.read_book_summary(item)
            elif effect.action == EFFECT_POUR_TEA:
                tea_line = beat_detail.tea_summary(item)
                if not tea_line:
                    return self._reject_plan("tea_unavailable")
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

