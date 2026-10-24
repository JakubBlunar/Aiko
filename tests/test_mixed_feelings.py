"""K104 cause isolation, replay safety and proportionate mixed rendering."""
from datetime import datetime, timedelta, timezone

from app.core.affect import emotion_episodes as ee


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def add(state, emotion="warm_glow", cause="our garden project", **kwargs):
    return ee.add_episode(
        state, emotion=emotion, cause=cause, source="test", now=NOW,
        intensity=kwargs.pop("intensity", 0.6), **kwargs,
    )


def test_replayed_observation_does_not_refresh_decay_or_amplify():
    state = add(ee.EpisodeState())
    decayed = ee.apply_decay(state, NOW + timedelta(hours=1))
    assert add(decayed) is decayed
    assert decayed.episodes[0].intensity < 0.6


def test_matching_event_updates_without_overwriting_another_cause():
    state = add(ee.EpisodeState(), cause_id="event:1")
    state = add(state, cause="our music project", cause_id="event:2")
    updated = add(state, cause="the garden succeeded", cause_id="event:1", intensity=0.8)
    assert len(updated.episodes) == 2
    assert updated.episodes[0].id == state.episodes[0].id
    assert updated.episodes[1] == state.episodes[1]
    assert updated.episodes[0].intensity == 0.8


def test_scoped_resolution_survives_serialization_and_blocks_replayed_event():
    state = add(ee.EpisodeState(), "miffed", cause_id="event:1")
    state = add(state, "miffed", cause="our music project", cause_id="event:2")
    state = ee.resolve(state, "miffed", episode_id=state.episodes[0].id, reason="clarified")
    restored = ee.deserialize(ee.serialize(state))
    assert add(restored, "miffed", cause_id="event:1") is restored
    assert len(restored.episodes) == 1
    assert restored.episodes[0].cause == "our music project"
    assert len(add(restored, "miffed", cause_id="event:3").episodes) == 2


def test_apology_for_music_does_not_resolve_garden():
    state = add(ee.EpisodeState(), "miffed")
    ep = state.episodes[0]
    assert not ee.detect_acknowledgment(ep, "Sorry I forgot our music project")
    assert ee.detect_acknowledgment(ep, "Sorry I dismissed our garden project")
    assert not ee.detect_acknowledgment(ep, "Sorry about music. The garden project is fine")
    assert not ee.detect_acknowledgment(ep, "I am not sorry about the garden project")
    assert ee.detect_acknowledgment(ep, "To clarify, I meant the garden project needs more time")


def test_positive_and_relevant_unresolved_feeling_can_coexist():
    state = add(ee.EpisodeState(), "miffed", cause="the garden plan was dismissed")
    state = add(state, "warm_glow", cause="the music project succeeded")
    selected = ee.select_for_turn(state, "About the garden plan")
    assert len(selected) == 2
    block = ee.render_mixed_block(selected)
    assert "garden plan" in block and "music project" in block
    assert len(block) <= 800
    assert "[[reaction:" not in block
    assert "uncertainty" in block
    # An unrelated topic does not re-open the grievance.
    unrelated = ee.select_for_turn(state, "What should we cook?")
    assert [ep.emotion for ep in unrelated] == ["warm_glow"]


def test_two_negative_causes_do_not_stack_registers():
    state = add(ee.EpisodeState(), "miffed")
    state = add(state, "hurt", cause="the garden project was dismissed")
    assert len(ee.select_for_turn(state, "our garden project")) == 1


def test_legacy_rows_get_a_stable_cause_key_without_migration():
    state = ee.deserialize('{"episodes":[{"emotion":"warm_glow",'
                           '"cause":"our garden project","intensity":0.6,"source":"test"}]}')
    assert add(state) is state
    assert ee.deserialize(ee.serialize(state)).episodes[0].cause_key
