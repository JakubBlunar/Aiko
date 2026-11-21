"""Read-only L57 pilot over retained idle-life decision receipts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EpisodeReview:
    run_id: str
    episode_id: str
    unprompted: bool = False


def _window(store, target, reviews):
    completed, returns, alternatives = set(), set(), set()
    endpoints = []
    for review in reviews:
        events = store.decision(review.run_id)["events"]
        if events:
            endpoints.extend((events[0]["id"], events[-1]["id"]))
        if any(
            row["kind"] == "step_completed" and row["payload"].get("key") == target
            for row in events
        ):
            completed.add(review.episode_id)
        if not review.unprompted or any(row["kind"] == "planning_fallback" for row in events):
            continue
        eligibility = set()
        selected = None
        for row in events:
            if row["actor"] != "autonomous":
                continue
            payload = row["payload"]
            if row["kind"] == "candidates":
                eligibility = {item["key"] for item in payload.get("eligible", ())}
                selected = None
            elif row["kind"] == "choice":
                key = payload.get("key")
                if (
                    payload.get("route") == "model" and key in eligibility
                    and target in eligibility and len(eligibility) > 1
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
