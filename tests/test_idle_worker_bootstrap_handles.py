"""Late detector initialization must not hide already registered idle workers."""
from types import SimpleNamespace

from app.core.session.detectors_init_mixin import DetectorsInitMixin


def test_detector_bootstrap_preserves_worker_identity():
    class Host(DetectorsInitMixin):
        pass

    host = Host()
    host._chat_db = None
    host._embedder = None
    host._idle_scheduler = None
    host._memory_store = None
    host._belief_store = None
    workers = {name: object() for name in (
        "_away_activity_worker", "_hobby_worker", "_room_evolution_worker", "_diary_worker",
        "_forward_curiosity_worker", "_concept_hypothesis_worker", "_growth_witness_worker",
        "_aspiration_momentum_worker", "_wellbeing_concern_worker", "_shared_ritual_worker",
        "_self_callback_worker",
    )}
    for name, instance in workers.items():
        setattr(host, name, instance)
    flags = {name: False for name in (
        "topic_stagnation_enabled", "style_tracker_enabled", "style_signal_enabled",
        "sensory_anchor_enabled", "engagement_tracker_enabled",
    )}
    settings = SimpleNamespace(memory=SimpleNamespace(), agent=SimpleNamespace(**flags))
    host._init_detectors_and_state(settings)
    for name, instance in workers.items():
        assert getattr(host, name) is instance
