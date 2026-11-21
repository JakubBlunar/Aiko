"""Read completed choices from idle planner traces."""

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from app.core.world.idle_life_context import family


_FREEFORM_CHOICES = {"read_book", "tea", "wander", "nap", "stretching", "garden"}
_CUE_FIELDS = ("optional_inspiration", "daily_intention", "ongoing_interest")
_SKETCH = "home_new_sketch"
_READ = "read_book"
_FINDING_TEXT = {
    "changed_choice_pattern": (
        "Among the comparable choices we recorded, I chose reading earlier and sketching "
        "later when both were available."
    ),
    "repeated_choice": "I chose to sketch more than once when other activities were available.",
    "completed_activity": "I made a sketch during this period.",
}


@dataclass(frozen=True)
class EpisodeReview:
    run_id: str
    episode_id: str
    # None uses recorded planner inputs. A reviewer may confirm legacy data
    # or veto an otherwise eligible choice, but known cues always take precedence.
    unprompted: bool | None = None


def _eligible_model_choices(options, candidates, attribution, reviewed):
    inputs = options.get("choice_inputs")
    if reviewed is False:
        return set(), "reviewer_veto"
    if not (isinstance(inputs, dict) and inputs.get("version") == 1):
        if reviewed is not True:
            return set(), "missing_cue_receipts"
    elif any(inputs.get(field) is True for field in _CUE_FIELDS):
        return set(), "prompted_choices"
    elif any(inputs.get(field) is not False for field in _CUE_FIELDS):
        return set(), "missing_cue_receipts"
    if attribution is None and reviewed is not True:
        return set(), "missing_cue_receipts"
    if attribution is not None and (
        attribution.get("model_selected_refs") or attribution.get("invalid_reference_count")
        or attribution.get("choice_reason") in {"context", "room_need", "continue_work"}
    ):
        return set(), "prompted_choices"
    if not options:
        return candidates, None  # A reviewer confirmed the selection from legacy evidence.
    choices = candidates & _FREEFORM_CHOICES
    offered = options.get("offered")
    choices.update(
        f"home_{row['action']}" for row in (offered if isinstance(offered, list) else ())
        if isinstance(row, dict) and isinstance(row.get("action"), str)
    )
    if options.get("writing_available") is True:
        choices.add("writing")
    return choices, None


def _completed_choices(events, reviewed, coverage):
    candidates = set()
    options, attribution = {}, None
    selection = None
    for row in events:
        if row["actor"] != "autonomous":
            continue
        kind, payload = row["kind"], row["payload"]
        if kind == "candidates":
            candidates = {item["key"] for item in payload.get("eligible", ())}
            options, attribution, selection = {}, None, None
        elif kind == "model_options":
            options, attribution = payload, None
        elif kind == "context_attribution":
            attribution = payload
        elif kind == "choice":
            selection = None
            if payload.get("route") != "model":
                continue
            eligibility, reason = _eligible_model_choices(
                options, candidates, attribution, reviewed,
            )
            if reason:
                coverage[reason] += 1
            elif payload.get("key") in eligibility:
                selection = (payload["key"], eligibility, row)
            else:
                coverage["unverified_choices"] += 1
        elif kind == "step_completed" and selection and payload.get("key") == selection[0]:
            yield (*selection, row)
            selection = None
    if selection is not None:
        coverage["unverified_choices"] += 1


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
        for selected, eligibility, _, _ in _completed_choices(events, review.unprompted, Counter()):
            if (target in eligibility and any(family(key) != family(target) for key in eligibility)
                    and (selected == target or family(selected) != family(target))):
                picked = returns if selected == target else alternatives
                picked.add(review.episode_id)
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


def _evidence(choice, step):
    return {
        "run_id": step["run_id"],
        "choice_event_id": choice["id"] if choice else None,
        "completion_event_id": step["id"],
        "recorded_at": step["recorded_at"],
    }


def build_recent_choice_history(decisions, *, now, retention_days, truncated=False):
    """Project one bounded drawing example from complete autonomous decisions."""
    start = (now - timedelta(days=retention_days)).isoformat()
    split = (now - timedelta(days=retention_days / 2)).isoformat()
    end = now.isoformat()
    coverage = Counter()
    studies = {"earlier": [], "later": []}
    sketches, returns = [], []
    for decision in decisions:
        events = decision["events"]
        if (decision["truncated"] or not events or events[0]["kind"] != "run_started"
                or events[-1]["kind"] != "run_finished"
                or any(ev["actor"] != "autonomous" for ev in (events[0], events[-1]))):
            coverage["incomplete_runs"] += 1
            continue
        if any(ev["kind"] == "choice" and ev["payload"].get("route") == "debug_forced"
               for ev in events):
            continue
        first, last = events[0]["recorded_at"], events[-1]["recorded_at"]
        if not start <= first <= last <= end or first < split <= last:
            coverage["boundary_runs"] += 1
            continue
        coverage["reviewed_runs"] += 1
        completed = next((ev for ev in events if ev["kind"] == "step_completed"
                          and ev["actor"] == "autonomous" and ev["payload"].get("key") == _SKETCH),
                         None)
        if completed:
            sketches.append(_evidence(None, completed))
        if any(ev["kind"] == "planning_fallback" for ev in events):
            continue
        found = list(_completed_choices(events, None, coverage))
        if len(found) != 1:
            continue
        key, options, choice, step = found[0]
        source = _evidence(choice, step)
        if key == _SKETCH:
            if not any(family(option) != family(_SKETCH) for option in options):
                coverage["without_alternative"] += 1
                continue
            returns.append(source)
        if {_SKETCH, _READ} <= options:
            studies["earlier" if last < split else "later"].append((key, source))

    before, after = studies["earlier"], studies["later"]
    finding = None
    if (not truncated and len(before) >= 2 and len(after) >= 2
            and all(key == _READ for key, _ in before)
            and all(key == _SKETCH for key, _ in after)):
        finding = {
            "kind": "changed_choice_pattern",
            "earlier_count": len(before), "later_count": len(after),
            "earlier": [source for _, source in before[-3:]],
            "later": [source for _, source in after[-3:]],
        }
    elif len(returns) >= 2:
        finding = {"kind": "repeated_choice", "count": len(returns), "later": returns[-3:]}
    elif sketches:
        finding = {"kind": "completed_activity", "count": len(sketches), "later": sketches[-3:]}
    if finding:
        finding["statement"] = _FINDING_TEXT[finding["kind"]]
        if finding["kind"] == "completed_activity" and len(sketches) > 1:
            finding["statement"] = f"I made sketches on {len(sketches)} recorded occasions."
    return {
        "from": start, "to": end, "split": split, "truncated": truncated,
        "coverage": {key: coverage[key] for key in (
            "reviewed_runs", "missing_cue_receipts", "prompted_choices", "without_alternative",
            "unverified_choices", "incomplete_runs", "boundary_runs",
        )},
        "finding": finding,
    }
