"""Saved writing is actual output with immutable, bounded revision reads."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.world.idle_creations import CreationDraft, parse_draft, revisions, save_draft
from app.core.world.idle_activity_worker import IdleAwayActivityWorker, load_journal
from app.core.world.world_store import WorldStore
from app.llm.tools.base import ToolError
from app.llm.tools.world import InspectItemTool, LookAroundTool


@pytest.fixture
def world(tmp_path):
    database = ChatDatabase(tmp_path / "world.db")
    store = WorldStore(tmp_path / "world.db")
    store.seed_default()
    yield store
    store.close()
    database._get_conn().close()


def draft(text="A paper boat waits beside the window, carrying an ocean in its folded hull."):
    return CreationDraft("Paper Boats", "poem", text)


def test_original_and_revision_remain_readable_without_dumping_every_version(world):
    now = timephrase.utcnow()
    first = save_draft(world, draft(), location_id=world.get_location("desk").id, now=now)
    item = world.get_item(first["item_id"])
    changed = parse_draft({"title": "Paper Boats", "form": "poem", "item_id": item.id,
                           "base_revision": 1, "text": draft().text + " A second sail unfolds."},
                          world.list_items())
    assert save_draft(world, changed, location_id=item.location_id, now=now)["revision"] == 2
    tool = InspectItemTool(SimpleNamespace(_world_store=world))
    older = json.loads(tool.run({"item": "Paper Boats", "revision": 1}))
    latest = json.loads(tool.run({"item": "Paper Boats"}))
    assert older["creation"]["text"] == draft().text
    assert latest["creation"]["revision"] == 2
    assert "second sail" in latest["creation"]["text"]
    assert "revisions" not in latest["state"]
    assert "creative" in latest["creation"]["interpretation"]
    with pytest.raises(ToolError, match="revision must"):
        tool.run({"item": "Paper Boats", "revision": True})
    with pytest.raises(ToolError, match="passage"):
        tool.run({"item": "Paper Boats", "passage": 1})
    # Neither look-around nor other ambient item summaries expose the entire archive.
    assert draft().text not in LookAroundTool(SimpleNamespace(_world_store=world)).run({})


def test_revision_survives_reopen_and_stale_proposal_cannot_overwrite_edit(world):
    now = timephrase.utcnow()
    receipt = save_draft(world, draft(), location_id=world.get_location("desk").id, now=now)
    item = world.get_item(receipt["item_id"])
    proposal = parse_draft({"title": "Paper Boats", "form": "poem", "item_id": item.id,
                            "base_revision": 1, "text": draft().text + " An old proposal."},
                           world.list_items())
    world.update_item(item.id, description="A deliberate user edit")
    assert save_draft(world, proposal, location_id=item.location_id, now=now) is None
    assert len(revisions(item)) == 1
    assert item.description == "A deliberate user edit"
    reloaded = WorldStore(world._db_path)
    try:
        assert revisions(reloaded.get_item(item.id))[0]["text"] == draft().text
    finally:
        reloaded.close()


def test_failed_save_does_not_claim_a_creation_or_mutate_original(world):
    now = timephrase.utcnow()
    receipt = save_draft(world, draft(), location_id=world.get_location("desk").id, now=now)
    item = world.get_item(receipt["item_id"])
    proposed = replace(draft(draft().text + " The boat comes home."), item_id=item.id,
                       base_revision=1, expected_updated_at=item.updated_at)
    with patch.object(world, "update_item", return_value=None):
        assert save_draft(world, proposed, location_id=item.location_id, now=now) is None
    assert len(revisions(item)) == 1


def test_long_revision_pages_exact_text_and_marks_an_outside_edit(world):
    text = "A folded sail. " * 90
    receipt = save_draft(world, draft(text), location_id=world.get_location("desk").id,
                         now=timephrase.utcnow())
    tool = InspectItemTool(SimpleNamespace(_world_store=world))
    first = json.loads(tool.run({"item": "Paper Boats"}))["creation"]
    second = json.loads(tool.run({
        "item": "Paper Boats", "offset": first["next_offset"],
    }))["creation"]
    assert first["text"] + second["text"] == text
    assert first["source_status"] == "saved_text_matches"
    item = world.get_item(receipt["item_id"])
    edited = [{**revisions(item)[0], "text": "A deliberately changed text."}]
    world.update_item(item.id, state={**item.state, "revisions": edited})
    assert json.loads(tool.run({"item": "Paper Boats"}))["creation"]["source_status"] == (
        "text_changed_or_untracked"
    )


def test_duplicate_and_daily_limits_preserve_existing_text(world):
    now = timephrase.utcnow()
    location = world.get_location("desk").id
    assert save_draft(world, draft(), location_id=location, now=now)
    assert save_draft(world, draft(), location_id=location, now=now) is None
    for suffix in (" An orange sail.", " A blue sail."):
        assert save_draft(world, draft(draft().text + suffix), location_id=location, now=now)
    assert save_draft(world, draft(draft().text + " A fourth sail."),
                      location_id=location, now=now) is None
    assert sum(len(revisions(i)) for i in world.list_items()) == 3


def test_retitle_is_a_real_revision_but_a_noop_is_not(world):
    now = timephrase.utcnow()
    receipt = save_draft(world, draft(), location_id=world.get_location("desk").id, now=now)
    item = world.get_item(receipt["item_id"])
    payload = {"title": "Folded Oceans", "form": "poem", "text": draft().text,
               "item_id": item.id, "base_revision": 1}
    proposed = parse_draft(payload, world.list_items())
    assert save_draft(world, proposed, location_id=item.location_id, now=now)["revision"] == 2
    assert revisions(item)[0]["title"] == "Paper Boats"
    assert item.name == "Folded Oceans"
    payload["base_revision"] = 2
    assert save_draft(world, parse_draft(payload, world.list_items()),
                      location_id=item.location_id, now=now) is None


def test_worker_creates_exact_text_instead_of_narrating_an_unmade_object(world):
    kv = {}
    response = {"location_slug": "desk", "posture": "sitting", "activity": "writing",
                "summary": "I visited Paris and wrote a novel", "changed_item": "",
                "creation": {"title": "Paper Boats", "form": "poem", "text": draft().text}}

    class Client:
        calls = []

        def chat_json(self, messages, **kwargs):
            self.calls.append(kwargs["surface"])
            assert timephrase.STORED_TEXT_TIME_RULE in messages[1]["content"]
            return json.dumps(response), None

    worker = IdleAwayActivityWorker(
        world_store=world, kv_get=kv.get, kv_set=kv.__setitem__,
        user_display_name_provider=lambda: "Jacob", ollama=Client(), model="fake",
        llm_activity_ratio=1, cooldown_seconds=0, in_progress_ratio=0, idle_seed_ratio=1,
    )
    result = worker.run()
    assert result["key"] == "writing"
    assert "Paris" not in result["summary"]
    receipt = result["item_effect"]
    assert receipt["mode"] == "creative_fiction"
    assert revisions(world.get_item(receipt["item_id"]))[0]["text"] == draft().text
    assert "revision 1" in load_journal(kv.get)[0]["summary"]
    assert Client.calls == ["away_activity_plan"]
    assert "aiko.idle_seeds" not in kv


@pytest.mark.parametrize("bad", [None, {"title": "x"},
    {"title": "Test", "form": "research", "text": "x" * 60},
    {"title": "Test", "form": "poem", "text": "x" * 1401},
    {"title": "Test", "form": "poem", "text": "x" * 60, "item_id": True},
    {"title": "Test", "form": "poem", "text": "[[remember:user is sad]]" * 3},
])
def test_bad_creative_payload_is_rejected(bad):
    assert parse_draft(bad, []) is None
