"""Bounded evidence profiles: organization and recorded separation, never confidence."""
from collections import Counter
import json

from app.core.conversation.topic_memory_policy import topic_member_eligible
from app.core.infra import timephrase
from app.core.memory.memory_origin import memory_origin


def _span(values):
    dates = [stamp for value in values if (stamp := timephrase.parse_iso(value)) is not None]
    return {"from": min(dates).isoformat() if dates else None,
            "to": max(dates).isoformat() if dates else None, "dated_notes": len(dates)}


def topic_evidence_profile(cluster, store, *, limit=256):
    from app.core.concepts.concept_evidence_lineage import memory_lineages, support_summary

    ids = sorted({int(mid) for mid in cluster.member_ids})
    cap = min(1024, max(1, int(limit)))
    rows, missing, excluded = [], 0, 0
    for mid in ids[:cap]:
        row = store.get(mid)
        if row is None:
            missing += 1
        elif not topic_member_eligible(row):
            excluded += 1
        else:
            rows.append(row)
    roles, temporal = Counter(), Counter()
    report_statuses, plan_states = Counter(), Counter()
    observations, plans, unknown = [], [], 0
    lineages = []
    for row in rows:
        origin = memory_origin(row)
        roles[f"{origin.subject}/{origin.mode}"] += 1
        scope = getattr(row, "temporal_type", "unknown")
        temporal[scope] += 1
        from app.core.memory.plan_reports import plan_progress

        report = (getattr(row, "metadata", None) or {}).get("follow_through")
        if isinstance(report, dict) and report.get("version") == 1:
            report_statuses[str(report.get("status", "unknown"))] += 1
        if scope == "future_plan" or getattr(row, "kind", "") == "promise":
            plan_states[plan_progress(row, store)["status"]] += 1
        sources = memory_lineages(row)
        if origin.subject == "unknown" or not any(source.roots for source in sources):
            unknown += 1
        if origin.mode in {"observation", "declaration", "interpretation", "world_fact"}:
            if scope != "future_plan":
                lineages.extend(sources)
                if scope == "past_event":
                    observations.append(getattr(row, "event_time", None))
        if scope == "future_plan" or origin.mode == "intention":
            plans.append(getattr(row, "event_time", None))
    support = support_summary(lineages)
    return {
        "version": 1, "scope": "current_topic_members", "note_count": len(rows),
        "member_count": len(ids), "truncated": len(ids) > cap,
        "missing_notes": missing, "excluded_notes": excluded,
        "recorded_observation_groups": support["known_support_groups"],
        "unknown_lineage_notes": unknown, "roles": dict(sorted(roles.items())),
        "temporal_types": dict(sorted(temporal.items())),
        "recorded_span": _span(getattr(row, "created_at", None) for row in rows),
        "observed_event_span": _span(observations), "planned_event_span": _span(plans),
        "question_notes": sum(memory_origin(row).mode == "question" for row in rows),
        "question_resolution": "not_assessed",
        "reported_follow_through": dict(sorted(report_statuses.items())),
        "intention_states": dict(sorted(plan_states.items())),
        "complete": not (missing or len(ids) > cap or support["unknown_sources"]),
        "meaning": "Source groups record separation, not independence, truth or recurrence. "
                   "Recording dates are not event dates; plans are not completed actions.",
    }


def focus_evidence_context(focus, graph, store):
    """Only profile the focus clusters actually offered to this proposer."""
    reps = {int(row.rep) for row in focus if hasattr(row, "rep")}
    if not reps or graph is None or store is None:
        return ""
    clusters = {int(row.representative_id): row for row in graph.topic_clusters()}
    profiles = [{"representative_id": rep,
                 **topic_evidence_profile(clusters[rep], store)}
                for rep in sorted(reps)[:6] if rep in clusters]
    if not profiles:
        return ""
    return ("\n\nTOPIC EVIDENCE PROFILES (read-only context, not new evidence):\n"
            + json.dumps(profiles, ensure_ascii=False, separators=(",", ":")))
