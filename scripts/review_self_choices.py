"""Read-only L57 pilot over retained idle-life decision receipts."""

from dataclasses import dataclass

from app.core.world.idle_life_context import family


_FREEFORM_CHOICES = {"read_book", "tea", "wander", "nap", "stretching", "garden"}
_CUE_FIELDS = ("optional_inspiration", "daily_intention", "ongoing_interest")


@dataclass(frozen=True)
class EpisodeReview:
    run_id: str
    episode_id: str
    # None uses recorded planner inputs. A reviewer may confirm legacy data
    # or veto an otherwise eligible choice, but known cues always take precedence.
    unprompted: bool | None = None


def _eligible_model_choices(options, candidates, attribution, reviewed):
    if reviewed is False:
        return set()
    inputs = options.get("choice_inputs")
    if isinstance(inputs, dict) and inputs.get("version") == 1:
        if any(inputs.get(field) is not False for field in _CUE_FIELDS):
            return set()
    elif reviewed is not True:
        return set()
    if attribution is None and reviewed is not True:
        return set()
    if attribution is not None and (
        attribution.get("model_selected_refs") or attribution.get("invalid_reference_count")
        or attribution.get("choice_reason") in {"context", "room_need", "continue_work"}
    ):
        return set()
    if not options:
        return candidates  # A reviewer confirmed the selection from legacy evidence.
    choices = candidates & _FREEFORM_CHOICES
    offered = options.get("offered")
    choices.update(
        f"home_{row['action']}" for row in (offered if isinstance(offered, list) else ())
        if isinstance(row, dict) and isinstance(row.get("action"), str)
    )
    if options.get("writing_available") is True:
        choices.add("writing")
    return choices


def _window(store, target, reviews):
    completed, returns, alternatives = set(), set(), set()
    endpoints = []
    for review in reviews:
        decision = store.decision(review.run_id)
        events = decision["events"]
        if events:
            endpoints.extend((events[0]["id"], events[-1]["id"]))
        if any(
            row["kind"] == "step_completed" and row["payload"].get("key") == target
            for row in events
        ):
            completed.add(review.episode_id)
        if decision.get("truncated") or any(row["kind"] == "planning_fallback" for row in events):
            continue
        candidates = set()
        options, attribution = {}, None
        selected = None
        for row in events:
            if row["actor"] != "autonomous":
                continue
            payload = row["payload"]
            if row["kind"] == "candidates":
                candidates = {item["key"] for item in payload.get("eligible", ())}
                options, attribution = {}, None
                selected = None
            elif row["kind"] == "model_options":
                options = payload
                attribution = None
            elif row["kind"] == "context_attribution":
                attribution = payload
            elif row["kind"] == "choice":
                key = payload.get("key")
                eligibility = _eligible_model_choices(
                    options, candidates, attribution, review.unprompted,
                )
                if (
                    payload.get("route") == "model" and key in eligibility
                    and target in eligibility
                    and any(family(option) != family(target) for option in eligibility)
                    and (key == target or family(key) != family(target))
                ):
                    selected = key
                else:
                    selected = None
            elif row["kind"] == "step_completed" and payload.get("key") == selected and selected:
                picked = returns if selected == target else alternatives
                picked.add(review.episode_id)
                selected = None
    # A resumed or multi-step episode is one occasion. Mixed choices within an
    # episode do not establish either side of the before/after comparison.
    span = (min(endpoints), max(endpoints)) if endpoints else None
    return {
        "completed": sorted(completed),
        "returns": sorted(returns - alternatives),
        "chose_alternative": sorted(alternatives - returns),
        "mixed_choices": sorted(returns & alternatives),
    }, span


def compare_choices(store, target, *, earlier, later):
    """Compare reviewed chronological windows and keep the source episode IDs."""
    earlier, later = tuple(earlier), tuple(later)
    episodes = {row.episode_id for row in earlier}
    if episodes.intersection(row.episode_id for row in later):
        raise ValueError("An episode can only belong to one comparison window")
    if len({row.run_id for row in (*earlier, *later)}) != len(earlier) + len(later):
        raise ValueError("A run can only be reviewed once")
    before, before_span = _window(store, target, earlier)
    after, after_span = _window(store, target, later)
    if before_span and after_span and before_span[1] >= after_span[0]:
        raise ValueError("The review windows must be chronological and disjoint")
    returns = len(after["returns"]) >= 2
    changed = (
        returns and len(before["chose_alternative"]) >= 2
        and not before["returns"] and not after["chose_alternative"]
        and not before["mixed_choices"] and not after["mixed_choices"]
    )
    return {
        "earlier": before,
        "later": after,
        "repeated_return": returns,
        "changed_choice_pattern": changed,
    }
