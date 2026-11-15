"""Explainable review candidates, not diagnoses or automatic behavior controls."""

from collections import Counter, defaultdict
from datetime import timedelta

from app.core.infra import timephrase

_MAINTENANCE = {
    "wash_vessel",
    "empty_vessel",
    "reheat_drink",
    "toggle_lid",
    "restock_drinks",
    "store_item",
    "retrieve_item",
    "discard_item",
    "restore_item",
    "unpack_order",
}


def review_events(events, *, cutoff=None, time_basis="recorded", window_end=None):
    reheats, lids, source_counts = defaultdict(list), defaultdict(list), Counter()
    steps, maintenance, model_attributions, context_offered = 0, 0, 0, 0
    resources, observed_resources = Counter(), Counter()
    flags, holds, latest_projects = [], {}, {}
    reheat_runs, lid_runs, hold_runs = [], [], []
    run_starts, run_ends = {}, set()
    mixes = Counter()
    scheduler_spans = []

    def resource_delta(payload, output):
        for change in payload.get("resource_changes", []):
            if not isinstance(change, dict):
                continue
            for field, values in change.get("fields", {}).items():
                if not (
                    field == "quantity" or ".supplies." in field or field.endswith(".amount_ml")
                ):
                    continue
                left, right = values.get("before"), values.get("after")
                if left is None:
                    left = 0
                if right is None:
                    right = 0
                if type(left) in (int, float) and type(right) in (int, float):
                    output[(str(change["item_id"]), field)] += right - left

    for event in events:
        p, kind = event["payload"], event["kind"]
        eid, actor = event["id"], event["actor"]
        if kind == "admission" and event["worker"] == "scheduler":
            start = timephrase.parse_iso(
                event["occurred_at"] if time_basis == "narrative" else event["recorded_at"]
            )
            end = timephrase.parse_iso(
                p.get("last_narrative_at", event["occurred_at"])
                if time_basis == "narrative"
                else event["last_seen_at"]
            )
            if start and end:
                scheduler_spans.append((start, end, p.get("boot_id", "unknown")))
        if kind == "run_started":
            run_starts[event["run_id"]] = eid
        elif kind in {"run_result", "run_failed", "run_finished"}:
            if kind == "run_finished":
                resource_delta(p, observed_resources)
            run_ends.add(event["run_id"])
        elif kind in {"project_state", "project_closed"}:
            latest_projects[p.get("project_id", "unknown")] = {"event_id": eid, **p}
        elif kind == "context":
            context_offered += bool(p.get("sources"))
            for ref in p.get("sources", []):
                source_counts[ref.get("ref", "unknown")] += 1
        elif kind == "context_attribution":
            model_attributions += bool(p.get("model_selected_refs"))
        elif kind == "admission" and event["worker"] == "away_activity":
            if p.get("reason") != "active_shared_conversation_situation":
                hold_runs.extend(holds.values())
                holds.clear()
                continue
            owner = p.get("hold_owner") or {}
            identity = (owner.get("situation_generation"), owner.get("situation_source_message_id"))
            start, end = (
                timephrase.parse_iso(
                    event["occurred_at"] if time_basis == "narrative" else event["recorded_at"]
                ),
                timephrase.parse_iso(
                    p.get("last_narrative_at", event["occurred_at"])
                    if time_basis == "narrative"
                    else event["last_seen_at"]
                ),
            )
            if not start or not end:
                continue
            start = max(start, cutoff) if cutoff else start
            previous = holds.get(identity)
            if previous and start - previous[1] <= timedelta(seconds=90):
                holds[identity] = (previous[0], end, previous[2], eid)
            else:
                if previous:
                    hold_runs.append(previous)
                holds[identity] = (start, end, eid, eid)
        elif kind == "action_effect":
            resource_delta(p, resources)
            if actor != "autonomous":
                continue
            action, item = p.get("action", "unknown"), str(p.get("item_id"))
            instance = str(p.get("drink_instance") or "unknown")
            steps += 1
            mixes[action] += 1
            maintenance += action in _MAINTENANCE
            if action in {"sip_drink", "empty_vessel", "prepare_drink"}:
                if reheats[instance]:
                    reheat_runs.append((instance, list(reheats[instance])))
                if lids[item]:
                    lid_runs.append((item, list(lids[item])))
                reheats[instance].clear()
                lids[item].clear()
            if action == "reheat_drink" and not instance.endswith("unknown"):
                reheats[instance].append(eid)
            if action == "toggle_lid":
                lids[item].append(eid)
        elif kind == "step_completed" and actor == "autonomous":
            receipt = p.get("receipt") or {}
            if receipt.get("mode") == "simulated_home_action":
                continue  # the canonical action_effect already counted it.
            steps += 1
            mixes[p.get("key", "unknown")] += 1

    for instance, ids in reheat_runs + list(reheats.items()):
        if len(ids) >= 3:
            flags.append(
                {
                    "kind": "reheating_without_sipping",
                    "event_ids": ids[-12:],
                    "detail": f"{len(ids)} reheats of drink {instance} "
                    "with no intervening recorded sip. "
                    "Later progress may exist; this can be intentional.",
                }
            )
    for item, ids in lid_runs + list(lids.items()):
        if len(ids) >= 6:
            flags.append(
                {
                    "kind": "lid_cycle",
                    "event_ids": ids[-12:],
                    "detail": f"{len(ids)} lid changes on item {item} "
                    "without recorded drink progress.",
                }
            )
    for start, end, first, last in hold_runs + list(holds.values()):
        if end - start >= timedelta(hours=6):
            flags.append(
                {
                    "kind": "long_shared_hold",
                    "event_ids": [first, last],
                    "detail": "A shared-conversation hold was observed for at least six hours. "
                    "Inspect its owner/source; duration alone does not prove it stale.",
                }
            )
    if steps >= 12 and maintenance / steps >= 0.7:
        flags.append(
            {
                "kind": "maintenance_share",
                "event_ids": [],
                "detail": f"{maintenance}/{steps} recorded autonomous steps were object upkeep. "
                "This is a review candidate, not a productivity target.",
            }
        )

    def delta_rows(counter):
        return [
            {"item_id": item, "field": field, "net_change": value}
            for (item, field), value in sorted(counter.items())
            if value
        ]

    coverage = coverage_summary(scheduler_spans, cutoff=cutoff, window_end=window_end)
    return {
        "coverage": coverage,
        "flags": flags,
        "autonomous_steps": steps,
        "maintenance_steps": maintenance,
        "activity_mix": dict(mixes),
        "context_offer_events": context_offered,
        "model_attribution_events": model_attributions,
        "most_offered_sources": [
            {"ref": ref, "offers": count} for ref, count in source_counts.most_common(12)
        ],
        "canonical_home_resource_deltas": delta_rows(resources),
        "observed_worker_resource_deltas": delta_rows(observed_resources),
        "resource_note": "Canonical and observed deltas overlap; never add them together.",
        "projects": sorted(latest_projects.values(), key=lambda row: row["event_id"],
                           reverse=True)[:40],
        "project_count": len(latest_projects),
        "projects_omitted": max(0, len(latest_projects) - 40),
        "incomplete_run_event_ids": [eid for rid, eid in run_starts.items() if rid not in run_ends][
            -40:
        ],
        "threshold_note": "Provisional review thresholds; calibrate using normal days. "
        "No automatic behavioral changes.",
    }


def coverage_summary(spans, *, cutoff=None, window_end=None):
    """Observed heartbeat continuity, never an inference that an offline app did nothing."""
    merged = []
    for start, end, boot in sorted(spans):
        start = max(start, cutoff) if cutoff else start
        end = min(end, window_end) if window_end else end
        if end < start:
            continue
        if merged and boot == merged[-1][2] and start - merged[-1][1] <= timedelta(seconds=90):
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]), boot)
        else:
            merged.append((start, end, boot))
    gaps = []
    cursor = cutoff or (merged[0][0] if merged else window_end)
    observed = 0.0
    for start, end, _ in merged:
        if cursor and start > cursor and (start - cursor).total_seconds() > 90:
            gaps.append(
                {
                    "from": cursor.isoformat(),
                    "to": start.isoformat(),
                    "seconds": (start - cursor).total_seconds(),
                    "status": "unknown; no continuous scheduler heartbeat",
                }
            )
        observed += max(0.0, (end - start).total_seconds())
        cursor = max(cursor, end) if cursor else end
    trailing = max(0.0, (window_end - cursor).total_seconds()) if window_end and cursor else 0.0
    if trailing > 90:
        gaps.append(
            {
                "from": cursor.isoformat(),
                "to": window_end.isoformat(),
                "seconds": trailing,
                "status": "unknown; no recent scheduler heartbeat",
            }
        )
    return {
        "observed_scheduler_seconds": observed,
        "unknown_spans": gaps[:50],
        "unknown_span_count": len(gaps),
        "omitted_spans": max(0, len(gaps) - 50),
        "trailing_unconfirmed_seconds": trailing,
        "note": "Heartbeats establish recorder coverage, not time spent performing activities.",
    }
