"""Read-only L57 pilot over retained idle-life decision receipts."""

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import argparse
import json
import sqlite3

from app.core.world.idle_choice_review import EpisodeReview, compare_choices
from app.core.world.idle_life_store import IdleLifeStore


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
