"""Read completed choices from idle planner traces."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

from app.core.world.idle_life_context import family
from app.core.world.pursuit_activities import ACTIVITY_BY_KEY


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


def activity_choice_receipt(chain):
    """Snapshot the original choice while its trace still exists, including for delayed notes."""
    if len(chain) != 1 or chain[0].key not in ACTIVITY_BY_KEY:
        return None
    from app.core.world.idle_life_trace import current_run

    run = current_run()
    if run is None or run.worker != "away_activity" or run.actor != "autonomous":
        return None
    decision = run.store.decision(run.run_id)
    events = decision["events"]
    if decision["truncated"] or any(row["kind"] == "planning_fallback" for row in events):
        return None
    found = list(_completed_choices(events, None, Counter()))
    if len(found) == 1:
        key, options, choice, step = found[0]
        alternatives = sorted(option for option in options if family(option) != family(key))
        if key == chain[0].key and alternatives:
            return {**_evidence(choice, step), "key": key, "alternatives": alternatives}
    return None


def build_recent_choice_history(decisions, *, now, retention_days, truncated=False):
    """Compare independent choices where the same two activities were available."""
    start = (now - timedelta(days=retention_days)).isoformat()
    split = (now - timedelta(days=retention_days / 2)).isoformat()
    end = now.isoformat()
    coverage = Counter()
    studies = {"earlier": [], "later": []}
    completed, returns = defaultdict(list), defaultdict(list)
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
        seen = set()
        for ev in events:
            activity = ACTIVITY_BY_KEY.get(ev["payload"].get("key"))
            if (ev["kind"] == "step_completed" and ev["actor"] == "autonomous"
                    and activity and activity not in seen):
                completed[activity].append(_evidence(None, ev))
                seen.add(activity)
        if any(ev["kind"] == "planning_fallback" for ev in events):
            continue
        found = list(_completed_choices(events, None, coverage))
        if len(found) != 1:
            continue
        key, options, choice, step = found[0]
        source = _evidence(choice, step)
        if not any(family(option) != family(key) for option in options):
            coverage["without_alternative"] += 1
            continue
        activity = ACTIVITY_BY_KEY.get(key)
        if activity:
            returns[activity].append(source)
        offered = {ACTIVITY_BY_KEY.get(option) for option in options}
        studies["earlier" if last < split else "later"].append((activity, offered, source))

    findings = {}
    for activity, occasions in completed.items():
        choices = returns[activity]
        finding = {"kind": "completed_activity", "count": len(occasions), "later": occasions[-3:],
                   "statement": f"I've spent some time {activity}."}
        if len(choices) >= 2:
            finding = {"kind": "repeated_choice", "count": len(choices), "later": choices[-3:],
                       "statement": f"I've chosen {activity} more than once."}
        for earlier in sorted(returns) if len(choices) >= 2 and not truncated else ():
            if activity == earlier:
                continue
            pair = {earlier, activity}
            before, after = [
                [(picked, src) for picked, offered, src in rows if pair <= offered]
                for rows in studies.values()
            ]
            if (len(before) >= 2 and len(after) >= 2 and all(k == earlier for k, _ in before)
                    and all(k == activity for k, _ in after)):
                finding = {"kind": "changed_choice_pattern", "previous_activity": earlier,
                           "earlier_count": len(before), "later_count": len(after),
                           "earlier": [src for _, src in before[-3:]],
                           "later": [src for _, src in after[-3:]],
                           "statement": f"I was choosing {earlier} before. "
                                        f"Lately I've been choosing {activity}."}
                break
        findings[activity] = {**finding, "activity": activity}

    def relevance(activity):
        item = findings[activity]
        return (completed[activity][-1]["recorded_at"] >= split,
                item["kind"] == "changed_choice_pattern", item["kind"] == "repeated_choice",
                sum(src["recorded_at"] >= split for src in returns[activity]),
                completed[activity][-1]["completion_event_id"])

    primary = max(findings, key=relevance, default=None)
    return {
        "from": start, "to": end, "split": split, "truncated": truncated,
        "coverage": {key: coverage[key] for key in (
            "reviewed_runs", "missing_cue_receipts", "prompted_choices", "without_alternative",
            "unverified_choices", "incomplete_runs", "boundary_runs",
        )},
        "findings": findings, "finding": findings.get(primary),
    }
