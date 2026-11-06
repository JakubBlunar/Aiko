from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.core.affect import mood_drift
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.proactive.growth_witness_worker import GrowthWitnessWorker
from app.core.relationship.growth_witness import comparison_receipt


def test_internal_change_publishes_dated_aiko_reflection_not_user_progress(tmp_path):
    db = ChatDatabase(tmp_path / "growth.db")
    cues = CueStore(db)
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    samples = [mood_drift.DriftSample(
        (now.date() - timedelta(days=14-i)).isoformat(), -.5+i/14, .2, .2, .2, .2,
    ) for i in range(15)]
    kv = {mood_drift.KV_SAMPLES: mood_drift.serialize_samples(samples)}
    worker = GrowthWitnessWorker(kv_get=kv.get, kv_set=kv.__setitem__,
        user_display_name_provider=lambda: "User", cue_store_provider=lambda: cues,
        goal_store=SimpleNamespace(list_active=lambda: [SimpleNamespace(
            content="Aiko wants to learn music",
            metadata={"last_progress_note": "Considering it"})]))
    with patch("app.core.proactive.growth_witness_worker._utcnow", return_value=now):
        assert worker.run()["drafted"] == 1
    row = cues.pending("growth_witness")[0]
    assert row.payload["subject"] == "aiko"
    assert "2026-10-23" in row.text and "2026-11-06" in row.text
    assert "your own mood" in row.text
    assert "he seems" not in row.text and "music" not in row.text
    assert worker._goal_detail() == ""
    assert comparison_receipt(samples, now + timedelta(days=8)) is None


def test_missing_or_future_sample_period_cannot_become_a_fresh_cue():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    samples = [mood_drift.DriftSample("2026-11-07", 0, 0, 0, 0, 0)] * 2
    assert comparison_receipt(samples, now) is None
    assert comparison_receipt([], now) is None
