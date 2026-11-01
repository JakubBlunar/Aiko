"""Archive summary provenance, lifecycle and durable idle job contracts."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.conversation.archive import ConversationArchive, resolve_range
from app.core.conversation.archive_review import ArchiveReview, MAX_JOBS
from app.core.conversation.archive_summary import summary_prompt, validate_summary
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.archive_summary_worker import ArchiveSummaryWorker, register_archive_workers

DAY, ZONE = "2026-10-20", "Europe/Bratislava"


@pytest.fixture
def db(tmp_path):
    value = ChatDatabase(tmp_path / "review.db")
    yield value
    value._get_conn().close()


def add(db, text="I decided to practice guitar.", owner="alice", role="user", day=DAY):
    stamp = datetime.fromisoformat(day + "T12:00:00+00:00")
    with patch.object(timephrase, "utcnow", return_value=stamp):
        return db.add_message(owner + ":main", role, text)


def raw(
    mid,
    text="The user decided to practice guitar.",
    quote="I decided to practice guitar.",
    kind="decision",
    event_date=None,
):
    return json.dumps(
        {
            "bullets": [
                {
                    "kind": kind,
                    "text": text,
                    "event_date": event_date,
                    "sources": [{"message_id": mid, "quote": quote}],
                }
            ]
        }
    )


def overview(db):
    span = resolve_range(start=DAY, end=DAY, tz_name=ZONE)
    return ConversationArchive(db, "alice").overviews(span)["overviews"]


def client_for(mid, **kwargs):
    return SimpleNamespace(chat_json=Mock(return_value=(raw(mid, **kwargs), None)))


def test_request_is_content_free_idempotent_and_survives_reopened_database(db, tmp_path):
    add(db)
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)
    review.enqueue(DAY, ZONE)
    assert review.jobs() == [{"local_day": DAY, "timezone": ZONE, "state": "pending", "reason": ""}]
    other = ChatDatabase(tmp_path / "review.db")
    try:
        assert ArchiveReview(other, "alice").pending() == (DAY, ZONE)
    finally:
        other._get_conn().close()
    assert not ArchiveReview(db, "bob").jobs()


def test_worker_generates_cited_summary_and_tool_can_drill_to_original(db):
    mid = add(db)
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)
    client = client_for(mid)
    assert review.run_one(client, "test-model", 8192)["status"] == "built"
    record = overview(db)[0]
    assert record["kind"] == "extractive_outline"
    summary = record["summary"]
    assert summary["source_text_complete"]
    assert summary["bullets"][0]["kind"] == "decision"
    assert summary["bullets"][0]["event_date"] is None
    assert not review.jobs()
    from app.llm.tools.conversation_history import ConversationHistoryTool

    tool = ConversationHistoryTool(db, "alice", ZONE)
    result = json.loads(tool.run({"mode": "overviews", "start": DAY, "end": DAY}))
    source = result["overviews"][0]["summary"]["bullets"][0]["sources"][0]
    original = json.loads(tool.run({"mode": "message", "message_id": source["message_id"]}))
    assert source["quote"] in original["message"]["text"]
    options = client.chat_json.call_args.kwargs
    assert options["surface"] == "archive_summary"
    assert options["timeout_seconds"] == 90


@pytest.mark.parametrize("mutation", ["edit", "delete", "insert", "owner", "role", "timestamp"])
def test_saved_summary_is_invalidated_by_every_source_mutation(db, mutation):
    mid = add(db)
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)
    assert review.run_one(client_for(mid), "test", 8192)["status"] == "built"
    if mutation == "edit":
        db.update_message_content(mid, "I changed my decision.")
    elif mutation == "delete":
        db.execute_commit("DELETE FROM messages WHERE id=?", (mid,))
    elif mutation == "insert":
        add(db, "A correction to the plan.")
    else:
        column, value = {
            "owner": ("session_id", "bob:main"),
            "role": ("role", "system"),
            "timestamp": ("created_at", "2026-10-21T12:00:00+00:00"),
        }[mutation]
        db.execute_commit(f"UPDATE messages SET {column}=? WHERE id=?", (value, mid))
    assert overview(db) == []


@pytest.mark.parametrize("action", ["delete", "edit", "insert", "cancel", "cancel_requeue"])
def test_inflight_changes_cannot_resurrect_summary_or_settle_replacement_job(db, action):
    mid = add(db)
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)

    def respond(*args, **kwargs):
        if action == "delete":
            db.execute_commit("DELETE FROM messages WHERE id=?", (mid,))
        elif action == "edit":
            db.update_message_content(mid, "Changed source")
        elif action == "insert":
            add(db, "A new statement.")
        else:
            review.remove(DAY, ZONE)
            if action == "cancel_requeue":
                review.enqueue(DAY, ZONE)
        return raw(mid), None

    assert (
        review.run_one(SimpleNamespace(chat_json=respond), "test", 8192)["status"]
        == "source_changed"
    )
    assert all("summary" not in row for row in overview(db))
    if action == "cancel_requeue":
        assert review.jobs()[0]["state"] == "pending"


def test_unrelated_edits_do_not_permanently_block_an_old_outline(db):
    mid = add(db)
    ConversationArchive(db, "alice").build_day(DAY, ZONE)
    other = add(db, day="2026-10-19")
    db.update_message_content(other, "Unrelated correction")
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)
    assert review.run_one(client_for(mid), "test", 8192)["status"] == "built"


def test_failure_does_not_store_provider_text_and_needs_explicit_retry(db):
    add(db)
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)
    client = SimpleNamespace(chat_json=Mock(side_effect=RuntimeError("private prompt")))
    assert review.run_one(client, "test", 8192)["status"] == "generation_failed"
    assert review.pending() is None
    assert review.jobs()[0]["reason"] == "generation_failed"
    assert "private" not in str(review.jobs())
    review.enqueue(DAY, ZONE)
    assert review.pending() == (DAY, ZONE)


def test_restart_reclaims_expired_job_but_not_active_lease_and_bounds_recovery(db):
    mid = add(db)
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)
    review._claim()
    assert review.pending() is None
    future = timephrase.utcnow() + timedelta(minutes=6)
    with patch.object(timephrase, "utcnow", return_value=future):
        assert (
            ArchiveReview(db, "alice").run_one(client_for(mid), "test", 8192)["status"] == "built"
        )
    review.enqueue(DAY, ZONE)
    review._claim()
    with patch.object(timephrase, "utcnow", return_value=future):
        review._claim()
    with patch.object(timephrase, "utcnow", return_value=future + timedelta(minutes=6)):
        assert review._claim() is None
    assert review.jobs()[0]["state"] == "failed"


def test_queue_cap_is_per_user_and_cancel_restores_capacity(db):
    review = ArchiveReview(db, "alice")
    for i in range(MAX_JOBS):
        review.enqueue((datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat(), ZONE)
    with pytest.raises(ValueError, match="full"):
        review.enqueue(DAY, ZONE)
    ArchiveReview(db, "bob").enqueue(DAY, ZONE)
    review.remove("2026-01-01", ZONE)
    review.enqueue(DAY, ZONE)


@pytest.mark.parametrize(
    "changes",
    [
        {"quote": "invented quote"},
        {"mid": 999},
        {"text": "Today the user practiced."},
        {"kind": "assistant_suggestion"},
        {"kind": "unknown"},
        {"event_date": "tomorrow"},
    ],
)
def test_invalid_sources_speaker_and_stale_time_are_rejected(db, changes):
    mid = add(db)
    row = ConversationArchive(db, "alice").message(mid)["message"]
    kwargs = {"mid": mid, **changes}
    with pytest.raises(ValueError):
        validate_summary(raw(**kwargs), [row])


def test_prompt_uses_timestamp_age_anchor_and_stored_time_rule_and_budget(db):
    for _ in range(20):
        add(db, "A long message " * 120)
    span = resolve_range(start=DAY, end=DAY, tz_name=ZONE)
    rows = ConversationArchive(db, "alice").messages(span)["messages"]
    clock = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    with patch.object(timephrase, "now", return_value=clock):
        packed = summary_prompt(rows, DAY, context_window=4096)
    assert packed is not None and packed.item_count < len(rows)
    assert packed.input_tokens <= packed.input_limit
    text = str(packed.messages)
    assert "message_id=" in text and "2026-10-20T12:00:00" in text
    assert timephrase.today_anchor(clock) in text
    assert timephrase.STORED_TEXT_TIME_RULE in packed.messages[0]["content"]


def test_shortened_source_never_claims_full_summary_coverage(db):
    mid = add(db, "I decided to practice guitar." + " More dialogue." * 200)
    review = ArchiveReview(db, "alice")
    review.enqueue(DAY, ZONE)
    assert review.run_one(client_for(mid), "test", 8192)["status"] == "built"
    assert not overview(db)[0]["summary"]["source_text_complete"]


def test_worker_advertises_llm_lane_and_uses_current_route_client(db):
    mid = add(db)
    client = client_for(mid)
    scheduler = Mock()
    session = SimpleNamespace(
        _chat_db=db,
        _user_id="alice",
        _idle_scheduler=scheduler,
        _settings=SimpleNamespace(
            tools=SimpleNamespace(enabled=True, daily_overviews=True, archive_timezone=ZONE)
        ),
        _maintenance_client=None,
        _worker_route_model_ctx=lambda: ("new-model", 8192),
    )
    register_archive_workers(session)
    workers = [call.args[0] for call in scheduler.register.call_args_list]
    worker = next(worker for worker in workers if isinstance(worker, ArchiveSummaryWorker))
    ArchiveReview(db, "alice").enqueue(DAY, ZONE)
    assert worker.demand(now=None, last_run_at=None).needs_llm
    session._maintenance_client = client
    assert worker.run()["status"] == "built"
    assert client.chat_json.call_args.kwargs["model"] == "new-model"
    assert not worker.demand(now=None, last_run_at=None).needs_llm
    session._settings.tools.enabled = False
    assert not worker.is_ready(now=None, last_run_at=None)
    assert worker.run()["status"] == "disabled"


def test_multi_day_directory_is_compact_and_summary_changes_expire_its_cursor(db):
    archive = ConversationArchive(db, "alice")
    for i in range(16):
        day = (datetime(2026, 10, 1) + timedelta(days=i)).date().isoformat()
        add(db, day=day)
        archive.build_day(day, ZONE)
    span = resolve_range(start="2026-10-01", end="2026-10-16", tz_name=ZONE)
    directory = archive.overviews(span)
    assert directory["next_cursor"]
    mid = db.execute_fetchone("SELECT MIN(id) FROM messages")[0]
    response = json.loads(raw(mid))
    response["bullets"] *= 6
    review = ArchiveReview(db, "alice")
    review.enqueue("2026-10-01", ZONE)
    client = SimpleNamespace(chat_json=lambda *a, **k: (json.dumps(response), None))
    assert review.run_one(client, "test", 8192)["status"] == "built"
    assert archive.overviews(cursor=directory["next_cursor"])["status"] == "stale_cursor"
    compact = archive.overviews(span)["overviews"][0]["summary"]
    assert len(compact["bullets"]) == 1 and compact["bullets_omitted"]
    day = resolve_range(start="2026-10-01", end="2026-10-01", tz_name=ZONE)
    assert len(archive.overviews(day)["overviews"][0]["summary"]["bullets"]) == 6
