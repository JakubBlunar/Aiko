"""Equal activity counts can support different accounts of Aiko's choices."""

import pytest

from app.core.concepts.concept_learning_event_store import ConceptLearningEventStore
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.self_history import build_self_history
from app.core.infra.chat_database import ChatDatabase
from app.core.world.idle_life_store import IdleLifeStore
from scripts.review_self_choices import EpisodeReview, compare_choices


_SKETCH = "home_new_sketch"
_READ = "read_book"
_REST = "nap"


@pytest.fixture
def replay(tmp_path):
    db = ChatDatabase(tmp_path / "self-choice-replay.db")
    try:
        yield db, IdleLifeStore(db)
    finally:
        db._get_conn().close()


def _episode(
    store, run_id, *, picked=_SKETCH, route="model", unprompted=True, episode_id=None,
    eligible=(_SKETCH, _READ, _REST), completed=True, fallback=False,
    options=None, choice_reason="own_interest",
):
    def record(kind, payload):
        assert store.record(kind, "away_activity", payload, run_id=run_id)

    record("candidates", {"eligible": [{"key": key} for key in eligible]})
    if options is not None:
        record("model_options", options)
        record("context_attribution", {"choice_reason": choice_reason})
    if fallback:
        record("planning_fallback", {"reason": "model_failed_or_invalid_plan"})
    record("choice", {"key": picked, "route": route})
    if completed:
        record("step_completed", {"key": picked})
    return EpisodeReview(run_id, episode_id or run_id, unprompted)


@pytest.mark.parametrize(("case", "repeated", "changed"), [
    ("assigned", False, False),
    ("voluntary", True, False),
    ("changed", True, True),
])
def test_equal_sketch_counts_support_different_choice_histories(replay, case, repeated, changed):
    db, store = replay
    earlier = tuple(
        _episode(store, f"early-{i}", picked=_READ, unprompted=case == "changed")
        for i in range(2)
    )
    later = tuple(
        _episode(store, f"late-{i}", unprompted=case != "assigned") for i in range(3)
    )
    report = compare_choices(store, _SKETCH, earlier=earlier, later=later)
    assert len(report["later"]["completed"]) == 3
    assert report["repeated_return"] is repeated
    assert report["changed_choice_pattern"] is changed

    # The current Memory Story can show a standing belief about the activity,
    # but idle receipts alone produce no existing concept learning event.
    concepts = ConceptStore(db)
    concepts.add(Concept(
        user_id="replay", kind="narrative", subject="aiko", status="active",
        label="I have been sketching", confidence=0.9, evidence_count=3,
        first_evidence_at="2026-11-01T12:00:00+00:00",
    ))
    arc = build_self_history(
        concept_store=concepts, learning_store=ConceptLearningEventStore(db),
    )
    assert arc.thin_record
    assert [entry.change for era in arc.eras for entry in era.entries] == ["settled"]


def test_resume_and_cued_fallback_or_unfinished_actions_never_add_returns(replay):
    _, store = replay
    reviews = (
        _episode(store, "start", episode_id="same"),
        _episode(store, "resume", episode_id="same"),
        _episode(store, "cued", unprompted=False),
        _episode(store, "weighted", route="weighted"),
        _episode(store, "fallback", fallback=True),
        _episode(store, "only-option", eligible=(_SKETCH,)),
        _episode(store, "incomplete", completed=False),
    )
    report = compare_choices(store, _SKETCH, earlier=(), later=reviews)
    assert report["later"]["returns"] == ["same"]
    assert not report["repeated_return"]
    with pytest.raises(ValueError, match="one comparison window"):
        compare_choices(store, _SKETCH, earlier=reviews[:1], later=reviews[1:])
    with pytest.raises(ValueError, match="only be reviewed once"):
        compare_choices(store, _SKETCH, earlier=(), later=(reviews[0], reviews[0]))
    with pytest.raises(ValueError, match="chronological and disjoint"):
        compare_choices(store, _SKETCH, earlier=reviews[-1:], later=reviews[:1])


def test_choice_reviews_use_actual_model_options_and_reject_cues(replay):
    _, store = replay
    inputs = {"version": 1, "optional_inspiration": False,
              "daily_intention": False, "ongoing_interest": False}
    options = {"choice_inputs": inputs, "offered": [{"action": "new_sketch"}]}

    def run(name, **overrides):
        return _episode(store, name, **{
            "options": options, "unprompted": None, "eligible": (_READ, _REST), **overrides,
        })

    earlier = [run(f"read-{i}", picked=_READ) for i in range(2)]
    later = [run(f"sketch-{i}") for i in range(2)]
    for field in ("optional_inspiration", "daily_intention", "ongoing_interest"):
        cued = {**options, "choice_inputs": {**inputs, field: True}}
        later.append(run(field, options=cued, unprompted=True))
    later.extend([
        run("old", options=None),
        run("continuing", choice_reason="continue_work"),
        run("no-alternatives", eligible=()),
        run("not-shown", options={**options, "offered": []}, eligible=(_SKETCH, _REST)),
        run("reviewer-veto", unprompted=False),
    ])

    review = compare_choices(store, _SKETCH, earlier=earlier, later=later)
    assert review["earlier"]["chose_alternative"] == ["read-0", "read-1"]
    assert review["later"]["returns"] == ["sketch-0", "sketch-1"]
    assert len(review["later"]["completed"]) == len(later)
    assert review["changed_choice_pattern"]
