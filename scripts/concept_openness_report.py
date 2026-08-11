#!/usr/bin/env python3
"""Read-only openness diagnostic for the L28 worker-diet / brain-line work.

L28 gave the pinned core lane an **openness reserve** and the per-turn flex
lane a **generative floor**, so a turn can never arrive carrying only what
Aiko must respect. Both mechanisms are *selection* code, and selection can
only reach what the store holds. This script answers the question the
per-turn telemetry cannot answer offline: **is there anything generative to
reach for, and does the selection actually reach it?**

Five sections:

- **Role mix** -- how much of the live graph is ``anchor`` / ``guide`` /
  ``generative``, and how much of the generative side can actually render
  in the static T3 block (a ``tension`` cannot -- it speaks only through
  its cooldowned T6 cue).
- **Pinned lane** -- the *real* ``ConceptView.core_lane`` run against the
  live rows with the live settings, so the reserve's fill, its draw order
  and its reachable kinds are observed rather than reasoned about.
- **Worker diets** -- every registered diet's real ``for_consumer``
  selection: what lands, what it costs against its budget, and which
  declared kinds contribute nothing because the store has none of them.
- **Generative intake** -- for each generative kind, the distance between
  its intake gate and the live value that has to clear it. This is the
  section that says whether thin supply is a cold start or a bar nobody
  can reach.
- **Prompt load** -- how many concept assertions one turn carries across
  the T0 profile block and the two T3 lanes.

Nothing here writes. The database is opened read-only via a URI, so it is
safe to run while Aiko is up, and the concept rows are loaded into a
throwaway in-memory mirror rather than a real ``ConceptStore``.

    python scripts/concept_openness_report.py
    python scripts/concept_openness_report.py --json

``--db`` points at an alternate database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"

from app.core.concepts.concept_diets import (  # noqa: E402
    CONCEPT_DIETS,
    DietTuning,
    resolve_budget,
)
from app.core.concepts.concept_kinds import (  # noqa: E402
    CONCEPT_KINDS,
    ROLES,
    ROLE_GENERATIVE,
    core_lane_kinds,
    kinds_by_role,
)
from app.core.concepts.concept_store import Concept  # noqa: E402
from app.core.concepts.concept_surfacing import (  # noqa: E402
    engagement_baseline,
)
from app.core.concepts.concept_view import ConceptView  # noqa: E402
from app.core.infra.settings import load_settings  # noqa: E402
from app.llm.token_utils import estimate_tokens  # noqa: E402

#: Longest label fragment printed in a sample row.
_LABEL_WIDTH = 58


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _role_of(kind: str) -> str:
    spec = CONCEPT_KINDS.get(str(kind))
    return str(getattr(spec, "role", "")) if spec is not None else ""


def _renders_in_static_block(kind: str) -> bool:
    """Whether a concept of this kind can appear in the T3 block at all.

    Read off the kind registry rather than restated here, so this report
    cannot drift from the renderer's own carve-out.
    """
    spec = CONCEPT_KINDS.get(str(kind))
    return bool(getattr(spec, "static_render", True)) if spec else True


class _RowStore:
    """The three read methods :class:`ConceptView` needs, over live rows.

    A real :class:`~app.core.concepts.concept_store.ConceptStore` wants a
    writable :class:`ChatDatabase` (it migrates on construction), which a
    read-only diagnostic must not have. The lanes this script measures
    (``core_lane`` / ``for_consumer``) only ever call ``list_by``, ``get``
    and ``cluster_evidence_for``, so mirroring those three over the loaded
    rows exercises the *real* selection code against the *real* graph.
    """

    def __init__(self, concepts: list[Concept]) -> None:
        self._by_id = {int(c.concept_id): c for c in concepts}

    def get(self, concept_id: int) -> Concept | None:
        return self._by_id.get(int(concept_id))

    def list_by(
        self,
        *,
        status: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
    ) -> list[Concept]:
        return [
            c
            for c in self._by_id.values()
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
        ]

    def cluster_evidence_for(self, concept_ids) -> dict[int, set[int]]:
        # Affect-lifted importance needs a live topic graph, which does not
        # exist offline; the diet path degrades to the bare kind prior.
        return {}


def _load_concepts(conn: sqlite3.Connection) -> list[Concept]:
    out: list[Concept] = []
    for r in conn.execute(
        "SELECT id, label, kind, subject, status, confidence, plasticity, "
        "       evidence_count, distinct_source_count, created_at, "
        "       promoted_at, last_reinforced_at, last_lifecycle_at "
        "FROM concepts"
    ):
        out.append(
            Concept(
                label=str(r["label"] or ""),
                kind=str(r["kind"] or ""),
                subject=str(r["subject"] or "user"),
                status=str(r["status"] or ""),
                confidence=float(r["confidence"] or 0.0),
                plasticity=float(r["plasticity"] or 0.5),
                evidence_count=int(r["evidence_count"] or 0),
                distinct_source_count=int(r["distinct_source_count"] or 0),
                created_at=str(r["created_at"] or ""),
                promoted_at=r["promoted_at"],
                last_reinforced_at=r["last_reinforced_at"],
                last_lifecycle_at=r["last_lifecycle_at"],
                concept_id=int(r["id"]),
            )
        )
    return out


def _describe(concept: Concept) -> dict[str, Any]:
    return {
        "id": int(concept.concept_id),
        "kind": concept.kind,
        "subject": concept.subject,
        "role": _role_of(concept.kind),
        "confidence": round(float(concept.confidence), 3),
        "label": " ".join(str(concept.label or "").split())[:_LABEL_WIDTH],
    }


def _role_counts(concepts: list[Concept]) -> dict[str, int]:
    counts = Counter(_role_of(c.kind) for c in concepts)
    return {role: int(counts.get(role, 0)) for role in ROLES}


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# ── section 1: role mix ───────────────────────────────────────────────


def _roles_section(concepts: list[Concept]) -> dict[str, Any]:
    actives = [c for c in concepts if c.status == "active"]
    mix = _role_counts(actives)
    total = sum(mix.values())
    generative = [c for c in actives if _role_of(c.kind) == ROLE_GENERATIVE]
    renderable = [c for c in generative if _renders_in_static_block(c.kind)]

    per_kind = []
    for name, spec in sorted(CONCEPT_KINDS.items()):
        rows = [c for c in concepts if c.kind == name]
        active = [c for c in rows if c.status == "active"]
        per_kind.append({
            "kind": name,
            "role": spec.role,
            "static_render": bool(getattr(spec, "static_render", True)),
            "core_always_on": bool(spec.core_always_on),
            "rows": len(rows),
            "active": len(active),
            "mean_confidence": (
                round(sum(c.confidence for c in active) / len(active), 3)
                if active else 0.0
            ),
        })

    return {
        "active_total": total,
        "by_role": mix,
        # The share of the graph that can only ever hold her where she is.
        "constraint_ratio": round(
            (mix["anchor"] + mix["guide"]) / total, 3
        ) if total else 0.0,
        "generative_active": len(generative),
        "generative_renderable": len(renderable),
        "generative_by_kind": dict(
            Counter(c.kind for c in generative).most_common()
        ),
        "by_subject": {
            subject: _role_counts(
                [c for c in actives if c.subject == subject]
            )
            for subject in sorted({c.subject for c in actives})
        },
        "per_kind": per_kind,
    }


# ── section 2: the pinned lane ────────────────────────────────────────


def _core_lane_section(
    view: ConceptView, *, ms: Any,
) -> dict[str, Any]:
    core_cap = max(0, int(getattr(ms, "context_budget_core_cap", 2)))
    core_min = float(getattr(ms, "context_budget_core_min_confidence", 0.75))
    slots_setting = max(
        0, int(getattr(ms, "concept_core_openness_slots", 2))
    )
    openness_min = float(
        getattr(ms, "concept_core_openness_min_confidence", 0.5)
    )
    # Mirrors ``build_relevant_context``: the reserve is sized against the
    # real cap, never the habituation over-fetch.
    slots = min(slots_setting, core_cap // 2)

    picks = view.core_lane(
        limit=core_cap,
        default_min_confidence=core_min,
        openness_slots=slots,
        openness_min_confidence=openness_min,
    )
    reserve = view._openness_picks(  # noqa: SLF001 -- diagnostic
        slots=slots, min_confidence=openness_min,
    )
    reserve_ids = {int(c.concept_id) for c in reserve}

    # Which generative kinds the reserve can draw from at all, so a kind
    # absent from the pin can be read as "outranked today" rather than as
    # "structurally excluded" -- the two need very different fixes.
    already = {k.name for k in core_lane_kinds()}
    candidates: dict[str, list[Concept]] = {}
    cue_only: dict[str, int] = {}
    for kind in kinds_by_role(ROLE_GENERATIVE):
        if kind.name in already:
            continue
        rows = view.core(kind=kind.name, min_confidence=openness_min)
        if not rows:
            continue
        if not _renders_in_static_block(kind.name):
            cue_only[kind.name] = len(rows)
            continue
        candidates[kind.name] = rows

    return {
        "core_cap": core_cap,
        "core_min_confidence": core_min,
        "openness_slots_setting": slots_setting,
        "openness_slots_effective": slots,
        "openness_min_confidence": openness_min,
        "picked": len(picks),
        "reserve_filled": len(reserve),
        "reserve_kinds": sorted({c.kind for c in reserve}),
        "reserve_renderable": all(
            _renders_in_static_block(c.kind) for c in reserve
        ),
        "eligible_generative_kinds": {
            name: len(rows) for name, rows in sorted(candidates.items())
        },
        "cue_only_generative_kinds": dict(sorted(cue_only.items())),
        "unreachable_generative_kinds": sorted(
            name for name in candidates
            if name not in {c.kind for c in reserve}
        ),
        "role_mix": _role_counts(picks),
        "picks": [
            {**_describe(c), "reserve": int(c.concept_id) in reserve_ids}
            for c in picks
        ],
    }


# ── section 3: worker diets ───────────────────────────────────────────


def _diets_section(view: ConceptView, *, tuning: DietTuning) -> dict[str, Any]:
    rows = []
    for name, diet in sorted(CONCEPT_DIETS.items()):
        picked = view.for_consumer(name)
        budget = resolve_budget(diet, tuning)
        spent = sum(estimate_tokens(c.label or "") + 6 for c in picked)
        present = {c.kind for c in picked}
        rows.append({
            "consumer": name,
            "declared_kinds": list(diet.kinds),
            "subject": diet.subject,
            "min_confidence": diet.min_confidence,
            "budget_tokens": budget,
            "spent_tokens": spent,
            "picked": len(picked),
            "role_mix": _role_counts(picked),
            # A declared kind the store cannot fill is the diet quietly
            # becoming narrower than it reads.
            "empty_kinds": [k for k in diet.kinds if k not in present],
            "sample": [_describe(c) for c in picked[:6]],
        })
    return {
        "context_window": tuning.context_window,
        "token_fraction": tuning.token_fraction,
        "max_tokens": tuning.max_tokens,
        "min_tokens": tuning.min_tokens,
        "diets": rows,
    }


# ── section 4: generative intake gates ────────────────────────────────


_ENGAGED_LABEL = "engaged"

_CLUSTER_AFFINITY_SQL = """
SELECT cluster_id, COUNT(*) AS surfaced,
       SUM(settled_flag) AS settled, SUM(engaged_flag) AS engaged
FROM (
  SELECT item_id AS cluster_id,
         CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END AS settled_flag,
         CASE WHEN engagement_label = ? THEN 1 ELSE 0 END AS engaged_flag
  FROM surfacing_outcomes
  WHERE item_kind = 'cluster' AND item_id > 0
  UNION ALL
  SELECT a.cluster_id,
         CASE WHEN so.settled_at IS NOT NULL THEN 1 ELSE 0 END,
         CASE WHEN so.engagement_label = ? THEN 1 ELSE 0 END
  FROM surfacing_outcomes so
  JOIN memory_topic_assignments a ON a.memory_id = so.item_id
  WHERE so.item_kind = 'memory' AND so.item_id > 0
)
GROUP BY cluster_id
HAVING settled >= ?
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _intake_section(
    conn: sqlite3.Connection, *, ms: Any, concepts: list[Concept],
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    # taste (K81): a cluster's engaged *rate* must clear the affinity bar,
    # which is relative to her pooled rate over the same snapshot.
    floor = float(getattr(ms, "taste_min_affinity", 0.15))
    multiple = max(
        1.0, float(getattr(ms, "taste_affinity_baseline_multiple", 1.4))
    )
    min_settled = max(1, int(getattr(ms, "taste_min_settled", 4)))
    rates: list[tuple[int, int, int, float]] = []
    if _table_exists(conn, "surfacing_outcomes"):
        for r in conn.execute(
            _CLUSTER_AFFINITY_SQL,
            (_ENGAGED_LABEL, _ENGAGED_LABEL, min_settled),
        ):
            settled = int(r["settled"] or 0)
            engaged = int(r["engaged"] or 0)
            rates.append((
                int(r["cluster_id"]), settled, engaged,
                round(engaged / settled, 4) if settled else 0.0,
            ))
    rates.sort(key=lambda t: -t[3])
    values = [t[3] for t in rates]
    baseline = engagement_baseline({
        cid: SimpleNamespace(settled=settled, engaged=engaged)
        for cid, settled, engaged, _rate in rates
    }) if rates else 0.0
    bar = min(1.0, max(floor, baseline * multiple))
    out["taste"] = {
        "absolute_floor": floor,
        "baseline_multiple": multiple,
        "baseline": round(baseline, 4),
        "effective_bar": round(bar, 4),
        "min_settled": min_settled,
        "clusters_warmed": len(rates),
        "clusters_over_bar": sum(1 for v in values if v >= bar),
        "best_rate": values[0] if values else 0.0,
        "median_rate": values[len(values) // 2] if values else 0.0,
        "mean_rate": (
            round(sum(values) / len(values), 4) if values else 0.0
        ),
        "top": [
            {
                "cluster_id": cid, "settled": settled,
                "engaged": engaged, "rate": rate,
            }
            for cid, settled, engaged, rate in rates[:8]
        ],
    }

    # pursuit (K85c): the pass is a no-op below a note floor. The count
    # alone cannot tell a cold start from a stalled writer, so the rate
    # rides along -- that distinction is the whole diagnosis.
    notes = 0
    first = last = None
    if _table_exists(conn, "memories"):
        row = conn.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM memories "
            "WHERE kind = 'pursuit_note'"
        ).fetchone()
        if row:
            notes = int(row[0] or 0)
            first, last = row[1], row[2]
    span_days = 0.0
    if first and last:
        start, end = _parse(str(first)), _parse(str(last))
        if start and end:
            span_days = max(
                (end - start).total_seconds() / 86400.0, 0.0
            )
    floor = max(1, int(getattr(ms, "pursuit_min_notes", 6)))
    per_day = round(notes / span_days, 2) if span_days >= 0.5 else None
    out["pursuit"] = {
        "min_notes": floor,
        "pursuit_note_memories": notes,
        "first_note": str(first or "")[:16],
        "last_note": str(last or "")[:16],
        "notes_per_day": per_day,
        "days_to_floor": (
            round(max(0, floor - notes) / per_day, 1)
            if per_day else None
        ),
    }

    # conduct (L42): a weekly pass, so absence is normal for a few days
    # and a five-week gap is not.
    last_run = None
    if _table_exists(conn, "kv"):
        row = conn.execute(
            "SELECT value FROM kv WHERE key LIKE '%conduct%'"
        ).fetchone()
        last_run = str(row[0]) if row else None
    out["conduct"] = {
        "cadence_seconds": int(
            getattr(ms, "conduct_cadence_seconds", 604800)
        ),
        "last_run_kv": last_run,
    }

    # Per-kind supply, including the rows that can never promote.
    supply = []
    for kind in kinds_by_role(ROLE_GENERATIVE):
        rows = [c for c in concepts if c.kind == kind.name]
        if not rows:
            supply.append({"kind": kind.name, "rows": 0})
            continue
        # Sourceless candidates are not automatically a defect: the K85
        # pursuit seeds are filed that way on purpose, so they have to earn
        # the same gate on the same lived notes a grown row needs.
        ungrounded = [
            c for c in rows
            if c.status == "candidate" and c.distinct_source_count <= 0
        ]
        supply.append({
            "kind": kind.name,
            "rows": len(rows),
            "active": sum(1 for c in rows if c.status == "active"),
            "candidate": sum(1 for c in rows if c.status == "candidate"),
            "ungrounded_candidates": len(ungrounded),
            "first_created": min(str(c.created_at or "") for c in rows)[:10],
            "last_created": max(str(c.created_at or "") for c in rows)[:10],
        })
    out["supply"] = supply
    return out


# ── section 5: prompt load ────────────────────────────────────────────


def _prompt_load_section(
    view: ConceptView, *, ms: Any, core: dict[str, Any],
) -> dict[str, Any]:
    profile_cap = max(0, int(getattr(ms, "profile_concept_max_lines", 10)))
    profile_bar = float(getattr(ms, "profile_concept_min_confidence", 0.5))
    flex_cap = max(0, int(getattr(ms, "context_budget_concept_cap", 15)))

    # The T0 block's own eligibility: subject=user identity + value.
    eligible = [
        c
        for kind in ("identity", "value")
        for c in view.core(
            subject="user", kind=kind, min_confidence=profile_bar,
        )
    ]
    profile_rows = sorted(
        eligible, key=lambda c: -float(c.confidence)
    )[:profile_cap]
    profile_tokens = sum(
        estimate_tokens(c.label or "") + 6 for c in profile_rows
    )
    core_tokens = sum(
        estimate_tokens(p["label"]) + 16 for p in core["picks"]
    )
    return {
        "profile_cap": profile_cap,
        "profile_min_confidence": profile_bar,
        "profile_eligible": len(eligible),
        "profile_rendered": len(profile_rows),
        "profile_tokens": profile_tokens,
        "core_rendered": core["picked"],
        "core_tokens": core_tokens,
        "flex_cap": flex_cap,
        "assertions_per_turn": len(profile_rows) + core["picked"] + flex_cap,
    }


# ── assembly ──────────────────────────────────────────────────────────


def collect(
    conn: sqlite3.Connection, *, now: datetime, settings: Any,
) -> dict[str, Any]:
    ms = settings.memory
    concepts = _load_concepts(conn)
    store = _RowStore(concepts)

    route = settings.llm.routes.get("worker_default")
    window = int(getattr(route, "context_window", 0) or 0)
    tuning = DietTuning(
        context_window=window,
        token_fraction=float(
            getattr(ms, "concept_diet_token_fraction", 0.06)
        ),
        max_tokens=int(getattr(ms, "concept_diet_max_tokens", 600)),
        min_tokens=int(getattr(ms, "concept_diet_min_tokens", 150)),
        importance_strength=(
            float(getattr(ms, "concept_importance_strength", 0.0) or 0.0)
            if bool(getattr(ms, "concept_importance_enabled", True))
            else 0.0
        ),
    )
    view = ConceptView(store, tuning=tuning)  # type: ignore[arg-type]

    core = _core_lane_section(view, ms=ms)
    return {
        "generated_at": now.isoformat(),
        "concepts": len(concepts),
        "roles": _roles_section(concepts),
        "core_lane": core,
        "diets": _diets_section(view, tuning=tuning),
        "intake": _intake_section(conn, ms=ms, concepts=concepts),
        "prompt_load": _prompt_load_section(view, ms=ms, core=core),
    }


def _render(data: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(f"Concept openness report  {data['generated_at']}")
    out.append("")

    roles = data["roles"]
    mix = roles["by_role"]
    out.append(
        f"{data['concepts']} concepts, {roles['active_total']} active: "
        f"{mix['anchor']} anchor, {mix['guide']} guide, "
        f"{mix['generative']} generative "
        f"({_pct(mix['generative'], roles['active_total'])}%). "
        f"constraint ratio {roles['constraint_ratio']}"
    )
    out.append(
        f"  generative that can render in T3: "
        f"{roles['generative_renderable']} of {roles['generative_active']} "
        f"-- {roles['generative_by_kind']}"
    )
    out.append("")
    out.append("Per kind (role / rows / active / mean confidence)")
    for row in roles["per_kind"]:
        flags = []
        if row["core_always_on"]:
            flags.append("core")
        if not row["static_render"]:
            flags.append("cue-only")
        out.append(
            f"  {row['kind']:<22} {row['role']:<11} "
            f"{row['rows']:>4} rows  {row['active']:>4} active  "
            f"conf {row['mean_confidence']:<6} {' '.join(flags)}"
        )

    core = data["core_lane"]
    out.append("")
    out.append(
        f"Pinned core lane (cap {core['core_cap']}, bar "
        f"{core['core_min_confidence']}) -- openness reserve "
        f"{core['openness_slots_effective']} slot(s) "
        f"(setting {core['openness_slots_setting']}, bar "
        f"{core['openness_min_confidence']})"
    )
    out.append(
        f"  {core['picked']} pinned, role mix {core['role_mix']}; "
        f"reserve filled {core['reserve_filled']}/"
        f"{core['openness_slots_effective']} with {core['reserve_kinds']}"
    )
    if not core["reserve_renderable"]:
        out.append(
            "  WARNING: a reserve pick is a cue-only kind -- it will be "
            "dropped by the renderer and the slot is wasted"
        )
    out.append(
        f"  eligible generative kinds "
        f"{core['eligible_generative_kinds']}; outranked today "
        f"{core['unreachable_generative_kinds']}; cue-only (never eligible) "
        f"{core['cue_only_generative_kinds']}"
    )
    for pick in core["picks"]:
        mark = "*" if pick["reserve"] else " "
        out.append(
            f"  {mark} #{pick['id']:<5} {pick['kind']:<20} "
            f"{pick['subject']:<12} {pick['role']:<11} "
            f"conf {pick['confidence']:<6} {pick['label']}"
        )

    diets = data["diets"]
    out.append("")
    out.append(
        f"Worker diets (worker window {diets['context_window']}, "
        f"{diets['token_fraction']} x window capped at "
        f"{diets['max_tokens']}, floor {diets['min_tokens']})"
    )
    for row in diets["diets"]:
        out.append(
            f"  {row['consumer']:<24} {row['picked']:>3} concepts  "
            f"{row['spent_tokens']:>4}/{row['budget_tokens']} tok  "
            f"roles {row['role_mix']}"
        )
        if row["empty_kinds"]:
            out.append(
                f"      declared but empty: {row['empty_kinds']}"
            )

    intake = data["intake"]
    out.append("")
    out.append("Generative intake gates")
    taste = intake["taste"]
    out.append(
        f"  taste     bar {taste['effective_bar']} = max(floor "
        f"{taste['absolute_floor']}, baseline {taste['baseline']} x "
        f"{taste['baseline_multiple']}); {taste['clusters_warmed']} clusters "
        f"warmed, {taste['clusters_over_bar']} over the bar "
        f"(best {taste['best_rate']}, median {taste['median_rate']})"
    )
    if taste["clusters_warmed"] and not taste["clusters_over_bar"]:
        out.append(
            "      UNREACHABLE: no cluster can clear the bar, so the pass "
            "mints nothing however long it runs"
        )
    pursuit = intake["pursuit"]
    line = (
        f"  pursuit   needs {pursuit['min_notes']} pursuit_note memories; "
        f"has {pursuit['pursuit_note_memories']}"
    )
    if pursuit["first_note"]:
        line += f" since {pursuit['first_note']}"
    if pursuit["notes_per_day"] is not None:
        line += f" ({pursuit['notes_per_day']}/day"
        if pursuit["days_to_floor"] is not None:
            line += f", floor in ~{pursuit['days_to_floor']}d"
        line += ")"
    elif pursuit["pursuit_note_memories"]:
        line += " (under a day of history -- cold start, not stalled)"
    out.append(line)
    conduct = intake["conduct"]
    out.append(
        f"  conduct   cadence {conduct['cadence_seconds']}s; "
        f"last-run key {conduct['last_run_kv'] or 'absent'}"
    )
    out.append("")
    out.append("Generative supply")
    for row in intake["supply"]:
        if not row.get("rows"):
            out.append(f"  {row['kind']:<12} no rows")
            continue
        note = (
            f"  ({row['ungrounded_candidates']} candidates with no evidence "
            f"yet -- authored seeds, or waiting on their first source)"
            if row["ungrounded_candidates"] else ""
        )
        out.append(
            f"  {row['kind']:<12} {row['rows']:>4} rows  "
            f"{row['active']:>3} active  {row['candidate']:>3} candidate  "
            f"{row['first_created']} -> {row['last_created']}{note}"
        )

    load = data["prompt_load"]
    out.append("")
    out.append("Concept assertions per turn")
    out.append(
        f"  T0 profile block  {load['profile_rendered']}/"
        f"{load['profile_cap']} lines (~{load['profile_tokens']} tok) "
        f"from {load['profile_eligible']} eligible"
    )
    out.append(
        f"  T3 pinned core    {load['core_rendered']} lines "
        f"(~{load['core_tokens']} tok)"
    )
    out.append(f"  T3 flex cap       {load['flex_cap']} lines")
    out.append(
        f"  worst case        {load['assertions_per_turn']} concept "
        f"assertions in one prompt"
    )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--json", action="store_true", help="emit raw JSON instead of a table"
    )
    args = parser.parse_args()

    settings = load_settings()
    conn = _connect(args.db)
    try:
        data = collect(
            conn, now=datetime.now(timezone.utc), settings=settings,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
