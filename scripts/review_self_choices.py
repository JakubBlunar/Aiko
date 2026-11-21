"""Read-only L57 pilot over retained idle-life decision receipts."""

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import argparse
import json
import sqlite3

from app.core.world.idle_life_context import family
from app.core.world.idle_life_store import IdleLifeStore


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


@dataclass(frozen=True)
class _SnapshotDecisions:
    conn: sqlite3.Connection

    def decision(self, run_id):
        return IdleLifeStore.read_decision(self.conn, run_id)

    def window(self, start, end):
        run_ids = self.conn.execute(
            "SELECT DISTINCT run_id FROM idle_life_events "
            "WHERE worker='away_activity' AND actor='autonomous' AND kind='run_started' "
            "AND run_id<>'' AND recorded_at>=? AND recorded_at<? ORDER BY run_id",
            (start, end),
        )
        reviews = []
        coverage = {"included_runs": 0, "incomplete_runs": 0, "boundary_runs": 0}
        for (run_id,) in run_ids:
            decision = self.decision(run_id)
            events = decision["events"]
            if decision["truncated"] or not events or events[-1]["kind"] != "run_finished":
                coverage["incomplete_runs"] += 1
            elif events[-1]["recorded_at"] >= end:
                coverage["boundary_runs"] += 1
            else:
                reviews.append(EpisodeReview(run_id, run_id))
        coverage["included_runs"] = len(reviews)
        return reviews, coverage


def _utc_bound(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Window bounds require a timezone, such as Z or +01:00")
    return stamp.astimezone(timezone.utc).isoformat()


def review_snapshot(path, target, *, earlier, later):
    """Compare complete autonomous runs in two half-open recording-time windows."""
    before_start, before_end = map(_utc_bound, earlier)
    after_start, after_end = map(_utc_bound, later)
    if not before_start < before_end <= after_start < after_end:
        raise ValueError("Use two nonempty chronological windows without overlap")
    uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")  # Both windows and every receipt share one SQLite snapshot.
        decisions = _SnapshotDecisions(conn)
        before, before_coverage = decisions.window(before_start, before_end)
        after, after_coverage = decisions.window(after_start, after_end)
        result = compare_choices(decisions, target, earlier=before, later=after)
    for name, coverage in (("earlier", before_coverage), ("later", after_coverage)):
        window = result[name]
        coverage["eligible_episodes"] = len(window["returns"]) + len(window["chose_alternative"])
        window["coverage"] = coverage
    if after_coverage["eligible_episodes"] < 2:
        result["repeated_return"] = None
    if min(before_coverage["eligible_episodes"], after_coverage["eligible_episodes"]) < 2:
        result["changed_choice_pattern"] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="A SQLite backup")
    parser.add_argument("--target", required=True, help="An activity key, e.g. home_new_sketch")
    parser.add_argument("--earlier", nargs=2, required=True, metavar=("START", "END"))
    parser.add_argument("--later", nargs=2, required=True, metavar=("START", "END"))
    args = parser.parse_args()
    try:
        report = review_snapshot(
            args.snapshot, args.target, earlier=args.earlier, later=args.later,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps({"target": args.target, **report}, indent=2))


if __name__ == "__main__":
    main()
