"""Live mode Pass 4: concept rails, modifiers, ledger. No speech."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.core.concepts.concept_diets import diet_for, registry_problems
from app.core.concepts.policy_selector import select_bounded_concepts
from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.live.modifiers import (
    apply_modifiers_to_urges,
    modifiers_from_situation,
)
from app.core.live.policy_context import LivePolicyContextRuntime
from app.core.live.urge_store import LiveUrgeStore
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.session_controller import SessionController


def _now() -> datetime:
    return datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-11T14:00:00Z",
        input_mode="typed",
        floor_transition="neither",
        dialogue_act="",
        arc="casual_check_in",
        arc_confidence=0.4,
        mood_label="content",
        vitality_band="normal",
        user_present=True,
        user_active_app="",
        world=WorldSituation(location_slug="beanbag", activity="idle"),
        inferred=None,
        inferred_stale=True,
        world_compatible=False,
        conflict_reason="",
        shared_commitment_active=False,
        since_user_activity_ms=0,
        sleep={"status": "awake"},
    )
    fields.update(kwargs)
    return ConversationSituationSnapshot(**fields)  # type: ignore[arg-type]


def _frame(**kwargs: object):
    ritual = kwargs.pop("ritual_priors", ())
    inp = LiveAssembleInput(
        snapshot=_snapshot(**kwargs),
        impulses=(),
        now=_now(),
        monotonic_ms=10_000.0,
        trigger_kind="heartbeat",
        ritual_priors=tuple(ritual) if ritual else (),
    )
    return assemble_live_situation(inp, generation=1)


def _concept(**kwargs: object) -> SimpleNamespace:
    fields = dict(
        concept_id=1,
        kind="ritual",
        label="watching anime together",
        subject="relationship",
        confidence=0.8,
        plasticity=0.3,
        last_reinforced_at="2026-09-11T12:00:00Z",
    )
    fields.update(kwargs)
    return SimpleNamespace(**fields)


class DietTests(unittest.TestCase):
    def test_live_policy_diet_is_registered_and_healthy(self) -> None:
        diet = diet_for("live_policy")
        assert diet is not None
        self.assertIn("value", diet.kinds)
        self.assertIn("boundary", diet.kinds)
        self.assertIn("ritual", diet.kinds)
        self.assertIn("taste", diet.kinds)
        self.assertEqual(registry_problems(), [])


class SelectorTests(unittest.TestCase):
    def test_does_not_write_habituation_or_change_confidence(self) -> None:
        concept = _concept(confidence=0.73)
        with patch(
            "app.core.concepts.concept_surfacing.save_habituation",
        ) as save:
            picks = select_bounded_concepts(
                rails=(concept,),
                relevant=(),
                habituation=1.0,
            )
            save.assert_not_called()
        self.assertEqual(concept.confidence, 0.73)
        self.assertEqual(picks[0].confidence, 0.73)
        self.assertEqual(picks[0].lane, "rail")


class ModifierTests(unittest.TestCase):
    def test_coding_plus_anime_ritual_stays_quiet_focus(self) -> None:
        frame = _frame(
            user_active_app="Cursor",
            ritual_priors=("watching anime together",),
        )
        self.assertEqual(frame.shared.inferred.label, "user_coding")
        mods = modifiers_from_situation(frame, (_concept(),))
        self.assertEqual(mods.speech_budget, "rare")
        self.assertEqual(mods.reason_code, "world_truth_coding")
        self.assertFalse(mods.questions_allowed)
        store = LiveUrgeStore()
        store.propose(
            kind="remain_present",
            subject="coding",
            source="test",
            source_ids=("a",),
            repetition_key="remain:coding",
            now_mono_ms=1_000.0,
        )
        store.propose(
            kind="share_delight",
            subject="coding",
            source="test",
            source_ids=("b",),
            repetition_key="delight:coding",
            now_mono_ms=1_000.0,
        )
        apply_modifiers_to_urges(store, mods)
        active = {urge.kind: urge.state for urge in store.all_urges()}
        self.assertEqual(active.get("remain_present"), "candidate")
        self.assertEqual(active.get("share_delight"), "parked")

    def test_shared_anime_also_sets_rare_speech(self) -> None:
        inferred = SimpleNamespace(
            shared=True,
            shared_activity="watching anime together",
            evidence_message_ids=(),
            updated_at="2026-09-11T14:00:00Z",
        )
        frame = _frame(
            inferred=inferred,
            inferred_stale=False,
            shared_commitment_active=True,
            world_compatible=True,
        )
        self.assertNotEqual(frame.shared.inferred.label, "user_coding")
        mods = modifiers_from_situation(frame, (_concept(),))
        self.assertEqual(mods.speech_budget, "rare")
        self.assertEqual(mods.attention_preference, "shared_activity")
        self.assertFalse(mods.questions_allowed)


class LedgerTests(unittest.TestCase):
    def test_ledger_records_selection_without_action(self) -> None:
        ritual = _concept()
        view = SimpleNamespace(
            for_consumer=lambda _name: [ritual],
            relevant=lambda *_args, **_kwargs: [],
        )
        runtime = LivePolicyContextRuntime(view_provider=lambda: view)
        store = LiveUrgeStore()
        frame = runtime.apply(
            _frame(),
            urges=store,
            trigger_kind="situation.shared_commitment_changed",
        )
        self.assertTrue(frame.continuity.behavior_rails)
        self.assertEqual(frame.constraints.speech_budget in {"rare", "normal"}, True)
        diag = runtime.diagnostics()
        self.assertTrue(diag["policy_ledger"])
        row = diag["policy_ledger"][-1]
        self.assertEqual(row["proposed_action"], "")
        self.assertFalse(row["main_model_used"])
        self.assertIn(ritual.concept_id, row["concept_ids"])


class MixinPolicyTests(unittest.TestCase):
    def test_refresh_stamps_modifiers_without_live_mode_enabled(self) -> None:
        host = _PolicyHost()
        frame = host.refresh_live_situation(trigger_kind="user.message_sent")
        assert frame is not None
        self.assertIn(frame.constraints.speech_budget, {"rare", "normal", "open"})
        diag = host.live_situation_diagnostics()
        self.assertIn("modifiers", diag)
        self.assertIn("policy_ledger", diag)
        self.assertFalse(hasattr(host, "_live_mode_enabled"))

    def test_still_no_live_mode_enabled_flag(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._live_voice_session_active = True
        controller._turn_in_progress = False
        controller._memory_settings = SimpleNamespace(
            idle_worker_quiet_threshold_seconds=30.0,
        )
        controller._last_user_activity_at = 0.0
        self.assertFalse(controller._is_user_idle())
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))


class _PolicyHost(LiveModeMixin):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                behavior_posture="live_presence",
                live_quiet=False,
                live_impulse_bus_enabled=True,
            ),
        )
        self._live_voice_session_active = False
        self._turn_in_progress = False
        self.session_key = "u:s"
        self._user_id = "default"
        self._chat_db = None
        self._cue_store = SimpleNamespace(pending=lambda limit=8: [])
        self._prepared_nudge_store = SimpleNamespace(get_fresh=lambda _uid: None)
        self._snapshot = _snapshot()
        self._init_live_mode()

    def conversation_situation_snapshot(self, **_kwargs):
        return self._snapshot


if __name__ == "__main__":
    unittest.main()
