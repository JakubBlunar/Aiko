"""Source-cited reports after a chosen exchange, read in one archive snapshot."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from types import SimpleNamespace

from app.core.conversation.archive import OWNER_SQL, TEXT_CHARS
from app.core.infra import timephrase
from app.core.memory.memory_relations import relation_current
from app.core.memory.observation_receipts import receipt_sources_current
from app.core.memory.plan_reports import report_current


SCAN_LIMIT = 500
RECEIPT_LIMIT = 500
PREVIEW_CHARS = 1000
BASELINE_ROWS = 32
MAX_RESULTS = 8
_PREVIEW = "CASE WHEN length(content)<=? THEN content END"
_CORRECTION = re.compile(r"^(?:correction\s*[:,]|i (?:was wrong about|meant)\b)", re.I)
_UPDATE = re.compile(
    r"^(?:update\s*[:,]|(?:i|we) (?:started|finished|completed|tried|moved|changed|"
    r"switched|replaced|rescheduled|postponed|cancell?ed|fixed|submitted|decided)\b)", re.I,
)
_NONLITERAL = re.compile(r"\b(?:pretend|imagine|hypothetical|role.?play|fictional)\b", re.I)
_RECEIPTS = (
    ("follow_through", "status", report_current, {
        "reported_action", "reported_completed", "cancelled", "changed_plan", "unknown",
    }),
    ("contextual_relation", "relation", relation_current, {"correction", "changed_preference"}),
)


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def _matches(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {_normalize(text)} "


def _reference(message: dict) -> dict:
    return {"message_id": message["message_id"], "session_id": message["session_id"],
            "role": message["role"], "message_sent_at": message["created_at"]}


def _position(message: dict) -> tuple[str, int]:
    return message["created_at"], message["message_id"]


def _owned_source(archive, conn, mid):
    message = archive._message(conn, mid, chars=PREVIEW_CHARS)
    if message and not message["session_id"].endswith(":noremember"):
        return message
    return None


class _SnapshotMemories:
    """Read just the prior rows needed by the existing receipt validators."""

    def __init__(self, conn):
        self._conn = conn
        self._rows = {}

    def _get_conn(self):
        return self._conn

    def get(self, mid):
        if type(mid) is not int:
            return None
        if mid not in self._rows:
            row = self._conn.execute(
                "SELECT content, kind, created_at, event_time, temporal_type, source_message_id, "
                "source_session, CASE WHEN json_valid(metadata) THEN metadata END "
                "FROM memories WHERE id=?", (mid,),
            ).fetchone()
            memory = None
            if row:
                content, kind, created, event, temporal, source_id, session, saved = row
                metadata = json.loads(saved) if saved else {}
                if isinstance(metadata, dict):
                    memory = SimpleNamespace(
                        id=mid, content=content, kind=kind, created_at=created, event_time=event,
                        temporal_type=temporal, metadata=metadata, source_message_id=source_id,
                        source_session=session,
                    )
            self._rows[mid] = memory
        return self._rows[mid]


def _source_event(metadata, temporal, report, store):
    fidelity = metadata.get("claim_fidelity")
    if (temporal != "past_event" or not isinstance(fidelity, dict)
            or fidelity.get("basis") != "reported"
            or fidelity.get("message_id") != report["message_id"]
            or not isinstance(fidelity.get("quote"), str)
            or not fidelity["quote"] or fidelity["quote"] not in report["quote"]
            or not receipt_sources_current(fidelity, store)):
        return {}
    stamp = timephrase.parse_iso(fidelity.get("event_time"))
    precision = fidelity.get("precision")
    if stamp and precision == "day":
        return {"event_date": stamp.date().isoformat(), "event_time_precision": "day"}
    if stamp and precision in {"minute", "exact"}:
        return {"event_time": fidelity["event_time"], "event_time_precision": precision}
    return {}


def _linked_reports(archive, conn, messages, phrase):
    if not messages:
        return {}, False
    store = _SnapshotMemories(conn)
    reports, seen = defaultdict(list), set()
    incomplete = False
    placeholders = ",".join("?" for _ in messages)
    for field, discriminator, is_current, accepted in _RECEIPTS:
        key = f"json_extract(metadata, '$.{field}.report.message_id')"
        rows = conn.execute(
            "SELECT metadata, temporal_type, source_session FROM memories "
            f"WHERE json_valid(metadata) AND {key} IN ({placeholders}) ORDER BY {key}, id LIMIT ?",
            (*messages, RECEIPT_LIMIT + 1),
        ).fetchall()
        incomplete |= len(rows) > RECEIPT_LIMIT
        for saved, temporal, session in rows[:RECEIPT_LIMIT]:
            metadata = json.loads(saved)
            receipt = metadata.get(field)
            if not isinstance(receipt, dict):
                continue
            report, state = receipt.get("report"), receipt.get(discriminator)
            if (not isinstance(report, dict) or receipt.get("subject") != "user"
                    or receipt.get("sources") != [report]
                    or not isinstance(state, str) or state not in accepted
                    or type(report.get("message_id")) is not int or report.get("role") != "user"
                    or not isinstance(report.get("quote"), str)
                    or not 8 <= len(report["quote"]) <= 240):
                continue
            mid = report["message_id"]
            after = messages.get(mid)
            if (after is None or report.get("session_id") != after["session_id"]
                    or session not in {None, "", after["session_id"]}):
                continue
            prior = store.get(receipt.get("prior_memory_id"))
            before = _owned_source(archive, conn, prior.source_message_id) if (
                prior is not None and type(prior.source_message_id) is int
            ) else None
            if (before is None or before["role"] != "user" or _position(before) >= _position(after)
                    or prior.source_session not in {None, "", before["session_id"]}
                    or not (_matches(report["quote"], phrase) or _matches(before["text"], phrase))
                    or not is_current(receipt, store)):
                continue
            kind = "plan_report" if field == "follow_through" else state
            identity = (kind, state, before["message_id"], report["quote"])
            if identity in seen:
                continue
            seen.add(identity)
            reports[mid].append({
                "kind": kind, "reported_at": after["created_at"], "quote": report["quote"],
                "before": _reference(before), "after": _reference(after),
                **({"plan_status": state} if kind == "plan_report" else {}),
                **_source_event(metadata, temporal, report, store),
            })
    return reports, incomplete


def _raw_change(text, phrase):
    if _NONLITERAL.search(text):
        return None
    for clause in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
        if clause.endswith("?") or not _matches(clause, phrase):
            continue
        if _CORRECTION.match(clause):
            return "correction"
        if _UPDATE.match(clause):
            return "reported_update"
    return None


def _read_reports(archive, conn, *, anchor, end, linked_only=False):
    # Same ordering as archive history. A later insertion of an older report
    # does not move that report into the requested interval.
    sql = (
        f"SELECT id, session_id, created_at, {_PREVIEW} FROM messages WHERE {OWNER_SQL} "
        "AND role='user' AND substr(session_id, -11) != ':noremember' "
    )
    params = [PREVIEW_CHARS, archive.user_id]
    if anchor is not None:
        sql += " AND (created_at, id) > (?, ?)"
        params.extend(_position(anchor))
    if end is not None:
        sql += " AND (created_at, id) <= (?, ?)"
        params.extend(_position(end))
    if linked_only:
        report_ids = " UNION ALL ".join(
            f"SELECT json_extract(metadata, '$.{field}.report.message_id') "
            "FROM memories WHERE json_valid(metadata) "
            f"AND json_type(metadata, '$.{field}.report.message_id')='integer'"
            for field, *_ in _RECEIPTS
        )
        sql += f" AND id IN ({report_ids})"
    rows = conn.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?",
                        (*params, SCAN_LIMIT + 1)).fetchall()
    messages = {mid: {"message_id": mid, "session_id": session, "role": "user",
                      "created_at": stamp, "text": text}
                for mid, session, stamp, text in reversed(rows[:SCAN_LIMIT])}
    return messages, len(rows) > SCAN_LIMIT


def _infer_anchor(archive, conn, phrase, end):
    messages, truncated = _read_reports(archive, conn, anchor=None, end=end, linked_only=True)
    linked, incomplete = _linked_reports(archive, conn, messages, phrase)
    candidates = {change["before"]["message_id"]: change["before"]
                  for reports in linked.values() for change in reports}
    coverage = {"inspected_linked_reports": len(messages),
                "scan_complete": not truncated and not incomplete}
    if len(candidates) == 1 and coverage["scan_complete"]:
        return _owned_source(archive, conn, next(iter(candidates))), coverage, linked
    if len(candidates) > 1:
        status = "anchor_ambiguous"
    elif coverage["scan_complete"]:
        status = "anchor_not_found"
    else:
        status = "anchor_search_incomplete"
    return None, {"status": status, "changes": [], "anchor_search": coverage,
                  "note": "The earlier exchange is unresolved. Ask one short question "
                          "about which earlier exchange the user means."}, {}


def read_changes(archive, conn, after_message_id, topic, *, through_message_id, limit):
    phrase = _normalize(topic)
    if not phrase or not 1 <= limit <= MAX_RESULTS:
        raise ValueError("Changes need a literal topic and a limit of 1-8.")
    anchor = anchor_search = None
    linked, receipts_incomplete = {}, False
    if after_message_id is not None:
        anchor = _owned_source(archive, conn, after_message_id)
        if anchor is None:
            return {"status": "anchor_not_found", "changes": []}
    end = None
    if through_message_id is not None:
        end = _owned_source(archive, conn, through_message_id)
        if end is None:
            return {"status": "end_not_found", "changes": []}
    if anchor is None:
        anchor, anchor_search, linked = _infer_anchor(archive, conn, phrase, end)
        if anchor is None:
            return anchor_search
    if end is not None and _position(end) <= _position(anchor):
        raise ValueError("through_message_id must point to a later exchange.")

    messages, reports_truncated = _read_reports(archive, conn, anchor=anchor, end=end)
    if after_message_id is not None:
        linked, receipts_incomplete = _linked_reports(archive, conn, messages, phrase)

    baseline = conn.execute(
        f"SELECT {_PREVIEW} FROM messages WHERE session_id=? AND role='user' "
        "AND (created_at, id) <= (?, ?) ORDER BY created_at DESC, id DESC LIMIT ?",
        (PREVIEW_CHARS, anchor["session_id"], *_position(anchor), BASELINE_ROWS),
    ).fetchall()
    seen = {_normalize(text) for (text,) in baseline if text}
    changes = [change for reports in linked.values() for change in reports]
    long_messages = 0
    for mid, message in messages.items():
        text = message["text"]
        normalized = _normalize(text) if text else ""
        if text is None:
            long_messages += 1
        if text and mid not in linked:
            kind = _raw_change(text, phrase) if normalized not in seen else None
            if kind:
                changes.append({"kind": kind, "reported_at": message["created_at"], "quote": text,
                                "before": None, "after": _reference(message)})
        if normalized:
            seen.add(normalized)

    scan_complete = not reports_truncated and not receipts_incomplete and not long_messages
    changes.sort(key=lambda item: (item["reported_at"], item["after"]["message_id"]))
    selected = changes[-limit:]
    while selected and len(json.dumps(selected, ensure_ascii=False)) > TEXT_CHARS:
        selected = selected[1:]
    status = "no_supported_changes" if scan_complete else "partial_scan"
    return {
        "status": "ok" if selected else status,
        "anchor": _reference(anchor), "through": _reference(end) if end else None,
        **({"anchor_search": anchor_search, "anchor_resolution": "unique_linked_topic"}
           if after_message_id is None else {}),
        "topic": topic, "changes": selected,
        "coverage": {
            "selection": "literal_topic_and_cited_links", "scan_complete": scan_complete,
            "inspected_user_messages": len(messages), "long_messages_skipped": long_messages,
            "results_truncated": len(selected) < len(changes),
            "oldest_inspected_message_id": next(iter(messages), None),
        },
        "authority": "Retained reports from the user. In a quote, 'I' means the user. "
                     "Report time is when the user said it. "
                     "Only a source-backed event date or time describes when it happened. "
                     "A missing before source means the original claim was not linked. "
                     "Results are selective, including when the scan completed.",
    }
