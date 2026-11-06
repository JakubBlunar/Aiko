import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_origin import claim_source_refusal, memory_origin
from app.core.memory.memory_store import MemoryStore
from app.core.memory.shared_shorthand import source_receipt
from app.core.session.speaking_window_jobs_mixin import SpeakingWindowJobsMixin
from app.core.session.vision_turn_mixin import SeenImage
from app.core.vision.image_memory import distil_image_memory, write_image_memory


AT = timephrase.parse_iso("2026-11-01T10:00:00Z")


def note(caption="A render of your outfit", **changes):
    value = {"summary": "An illustrated outfit, possibly blue.", "subject": "aiko",
             "subject_quote": "your outfit", "depicted_domain": "virtual",
             "domain_quote": "render", "caption_quote": caption,
             "visual_quote": "possibly blue", "depicted_time_quote": ""}
    value.update(changes)
    return distil_image_memory(description="The outfit is possibly blue", user_text=caption,
                               user_name="Noah", shared_at=AT,
                               chat=lambda *a, **k: json.dumps(value))


@pytest.mark.parametrize("subject,caption,quote", [
    ("aiko", "A render of your outfit", "your outfit"),
    ("user", "A render of my outfit", "my outfit"),
    ("world", "A render of my sister's outfit", "my sister's outfit"),
])
def test_depicted_subject_comes_from_cited_caption_not_uploader(subject, caption, quote):
    line = note(caption, subject=subject, subject_quote=quote)
    assert line.details["subject"] == subject
    assert line.details["depicted_domain"] == "virtual"


def test_unidentified_screenshot_cannot_acquire_a_known_subject_or_date():
    line = note("A screenshot", subject_quote="", domain_quote="")
    assert line.details["subject"] == "unknown"
    assert line.details["depicted_domain"] == "unknown"
    assert line.details["depicted_time"] is None
    assert note(subject_quote="invented ownership") == ""


def test_caption_date_is_preserved_without_making_upload_time_the_event_time(tmp_path):
    line = note("A photo of my outfit on 2020-06-15", subject="user",
                subject_quote="my outfit", depicted_domain="real", domain_quote="photo",
                depicted_time_quote="2020-06-15")
    path = tmp_path / "images.db"
    ChatDatabase(path)
    store = MemoryStore(path)
    try:
        mid = write_image_memory(line=line, filename="a.png", rel_path="Attachments:a.png",
                                 source_message_id=None, now=AT,
                                 embedder=SimpleNamespace(embed=lambda text: np.array([1., 0.])),
                                 memory_store=store)
        saved = store.get(mid)
        assert saved.event_time is None
        assert saved.metadata["image_evidence"]["depicted_time"].startswith("2020-06-15")
        assert saved.metadata["image_evidence"]["precision"] == "day"
        assert "caption (quoted)" in saved.content
        assert memory_origin(saved).subject == "user"
        assert claim_source_refusal(saved, subject="user", kind="narrative")
    finally:
        store.close()


def test_source_edit_during_embedding_defers_image_write(tmp_path):
    path = tmp_path / "images.db"
    db = ChatDatabase(path)
    mid = db.add_message("s", "user", "An old photo, not my holiday")
    row = db.get_message_row(mid)
    receipt = {**source_receipt(row, row.content), "source_at": row.created_at}
    store = MemoryStore(path)
    def embed(text):
        db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("Corrected caption", mid))
        return np.array([1., 0.])
    try:
        assert write_image_memory(
            line="An old photo", filename="a.png", rel_path="Attachments:a.png",
            source_message_id=mid, source_session="s", now=AT, caption_receipt=receipt,
            embedder=SimpleNamespace(embed=embed), memory_store=store,
        ) is None
        assert store.count() == 0
    finally:
        store.close()


def test_delayed_job_keeps_original_caption_message_and_sharing_time(tmp_path):
    db = ChatDatabase(tmp_path / "jobs.db")
    mid = db.add_message("s", "user", "This is a render of your outfit")
    db.execute_commit("UPDATE messages SET created_at=? WHERE id=?", (AT.isoformat(), mid))
    jobs = []
    host = SimpleNamespace(
        session_key="s", user_display_name="Noah", _chat_db=db, _embedder=object(),
        _memory_store=object(),
        _maintenance_client=SimpleNamespace(chat=lambda *a, **k: "A render"),
        _effective_worker_model="test", _scheduler=SimpleNamespace(submit=jobs.append),
        _fact_check_assistant_name=lambda: "Aiko",
        _drain_turn_vision=lambda: [SeenImage("a.png", "Attachments:a.png", "A render", 0.1)],
    )
    SpeakingWindowJobsMixin._maybe_schedule_image_memory_job(host, user_message_id=mid)
    host.session_key = "other"
    host._active_turn_user_text = "Unrelated later caption"
    with patch("app.core.vision.image_memory.write_image_memory") as write:
        jobs[0].callable(None)
    assert write.call_args.kwargs["source_message_id"] == mid
    assert write.call_args.kwargs["source_session"] == "s"
    assert write.call_args.kwargs["now"] == AT
    assert write.call_args.kwargs["caption_receipt"]["quote"] == "This is a render of your outfit"
