"""Keep provider outcomes out of memory evidence, including delayed writers."""

import sqlite3


def check_turn_evidence(
    conn, metadata: dict | None, source_message_id: int | None, source_session: str | None = None,
):
    result = dict(metadata or {})
    snapshot = result.get("turn_outcome")
    if isinstance(snapshot, dict) and snapshot.get("status") != "completed":
        return False, result
    admission = result.get("admission") or {}
    ids = {value for value in (
        source_message_id,
        admission.get("proposal_message_id") if isinstance(admission, dict) else None,
        snapshot.get("assistant_message_id") if isinstance(snapshot, dict) else None,
    ) if type(value) is int and value > 0}
    if isinstance(admission, dict):
        source_ids = admission.get("source_message_ids")
        if isinstance(source_ids, (list, tuple)):
            ids.update(v for v in source_ids if type(v) is int and v > 0)
    if not ids:
        return snapshot is None, result
    try:
        rows = conn.execute(
            "SELECT assistant_message_id, status, provider_id, model, route_revision, attempt_id, "
            "session_id "
            "FROM turn_outcomes WHERE assistant_message_id IN ("
            + ",".join("?" for _ in ids) + ")", tuple(sorted(ids)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: turn_outcomes" not in str(exc):
            raise
        # Legacy stores have no provenance. Do not manufacture a known origin.
        return snapshot is None, result
    if any(row[1] != "completed" for row in rows):
        return False, result
    if source_session is not None and any(row[6] != source_session for row in rows):
        return False, result
    if isinstance(snapshot, dict) and not any(
        row[5] == snapshot.get("attempt_id") for row in rows
    ):
        # A queued job's evidence may have been deleted since scheduling.
        return False, result
    if rows:
        result["turn_sources"] = [dict(zip(
            ("assistant_message_id", "status", "provider_id", "model",
             "route_revision", "attempt_id"),
            row[:6], strict=True,
        )) for row in rows]
        result["source_authority"] = "generated_output"
    return True, result
