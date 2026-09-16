"""Live mode Pass 21: title-free activity transition notices (L1)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.live.activity_notices import FOCUS_BOUNDARY_S, MEDIA_FOCUS_S
from app.core.live.assembler import LiveAssembleInput, assemble_live_situation
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.notice import notices_from_trigger
from app.core.live.policy_context import situation_summary
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.urge_store import LiveUrgeStore
from tests.test_live_mode_pass11 import _NOW, _coding_evidence, _frame, _snapshot


def _kinds(notices: tuple) -> set[str]:
    return {item.kind for item in notices}


def _blob(notices: tuple, *parts: object) -> str:
    chunks = [str(item.to_payload()) for item in notices]
    chunks.extend(str(part) for part in parts)
    return " ".join(chunks).lower()


def _shared_inferred() -> SimpleNamespace:
    return SimpleNamespace(
        shared=True,
        shared_activity="watching anime together",
        evidence_message_ids=(),
        updated_at="2026-09-13T12:00:00Z",
    )


def _media_frame(**kwargs: object):
    duration = int(kwargs.pop("duration_seconds", 90) or 90)
    app = str(kwargs.pop("app", "VLC") or "VLC")
    evidence = kwargs.pop(
        "activity_evidence",
        _coding_evidence(app=app, duration_seconds=duration, confidence=0.7),
    )
    return _frame(
        user_active_app=app,
        activity_evidence=evidence,
        **kwargs,
    )


class ActivityTransitionNoticeTests(unittest.TestCase):
    def test_coding_session_notices_focus_started(self) -> None:
        frame = _frame(user_active_app="Cursor")
        notices = notices_from_trigger("activity.session_changed", frame)
        self.assertIn("focus_started", _kinds(notices))
        self.assertNotIn("user_focus", _kinds(notices))
        self.assertEqual(notices[0].subject, "coding")
        store = LiveUrgeStore()
        created = store.ingest_notices(notices, now_mono_ms=10_000.0)
        self.assertTrue(any(urge.kind == "remain_present" for urge in created))
        self.assertTrue(any(urge.subject == "coding" for urge in created))

    def test_same_session_does_not_renotice(self) -> None:
        previous = _frame(user_active_app="Cursor")
        frame = _frame(user_active_app="Cursor")
        notices = notices_from_trigger(
            "activity.session_changed", frame, previous=previous,
        )
        self.assertNotIn("focus_started", _kinds(notices))
        self.assertNotIn("app_category_changed", _kinds(notices))

    def test_return_from_idle_notices_machine_and_focus(self) -> None:
        previous = _frame(
            user_active_app="",
            activity_evidence=_coding_evidence(
                app="", source="idle", os_idle="idle", confidence=0.0,
            ),
        )
        frame = _frame(user_active_app="Cursor")
        notices = notices_from_trigger(
            "activity.session_changed", frame, previous=previous,
        )
        kinds = _kinds(notices)
        self.assertIn("returned_to_machine", kinds)
        self.assertIn("focus_started", kinds)
        self.assertEqual(
            {item.subject for item in notices if item.kind == "returned_to_machine"},
            {"machine"},
        )

    def test_natural_boundary_after_long_focus(self) -> None:
        previous = _frame(
            user_active_app="Cursor",
            activity_evidence=_coding_evidence(
                duration_seconds=FOCUS_BOUNDARY_S,
            ),
        )
        idle = _frame(
            user_active_app="",
            activity_evidence=_coding_evidence(
                app="", source="idle", os_idle="idle", confidence=0.0,
            ),
        )
        notices = notices_from_trigger(
            "activity.idle", idle, previous=previous,
        )
        self.assertEqual(_kinds(notices), {"focus_boundary"})
        self.assertEqual(notices[0].subject, "presence")
        empty = notices_from_trigger("activity.idle", idle)
        self.assertEqual(empty, ())

    def test_category_change_coding_to_media(self) -> None:
        previous = _frame(user_active_app="Cursor")
        frame = _media_frame()
        notices = notices_from_trigger(
            "activity.session_changed", frame, previous=previous,
        )
        kinds = _kinds(notices)
        self.assertIn("app_category_changed", kinds)
        self.assertNotIn("focus_started", kinds)
        self.assertEqual(
            {item.subject for item in notices if item.kind == "app_category_changed"},
            {"media"},
        )

    def test_media_focus_after_threshold(self) -> None:
        short = _media_frame(duration_seconds=MEDIA_FOCUS_S - 1)
        notices = notices_from_trigger("activity.session_changed", short)
        self.assertNotIn("focus_started", _kinds(notices))
        long = _media_frame(duration_seconds=MEDIA_FOCUS_S)
        notices = notices_from_trigger("activity.session_changed", long)
        self.assertIn("focus_started", _kinds(notices))
        self.assertEqual(
            {item.subject for item in notices if item.kind == "focus_started"},
            {"media"},
        )

    def test_stale_and_weak_and_lock_stay_silent(self) -> None:
        stale = _frame(
            user_active_app="Cursor",
            activity_evidence=_coding_evidence(stale=True, confidence=0.0),
        )
        stale_notices = notices_from_trigger("activity.session_changed", stale)
        self.assertFalse(_kinds(stale_notices) & {
            "focus_started", "user_focus", "app_category_changed",
        })
        weak = _frame(
            user_active_app="Chrome",
            activity_evidence=_coding_evidence(
                app="Chrome", confidence=0.35, duration_seconds=12,
            ),
        )
        weak_notices = notices_from_trigger("activity.session_changed", weak)
        self.assertFalse(_kinds(weak_notices) & {
            "focus_started", "user_focus", "shared_activity_resumed",
        })
        locked = _frame(
            user_active_app="",
            activity_evidence=_coding_evidence(
                app="", source="lock", os_idle="locked", confidence=0.0,
            ),
        )
        lock_notices = notices_from_trigger("activity.lock", locked)
        self.assertEqual(lock_notices, ())
        missing = assemble_live_situation(
            LiveAssembleInput(
                snapshot=_snapshot(user_active_app="Cursor"),
                impulses=(),
                now=_NOW,
                monotonic_ms=10_000.0,
                activity_evidence=None,
            ),
            generation=1,
        )
        missing_notices = notices_from_trigger(
            "activity.session_changed", missing,
        )
        self.assertFalse(_kinds(missing_notices) & {
            "focus_started", "user_focus", "app_category_changed",
        })

    def test_shared_resume_is_delight_coding_is_not(self) -> None:
        previous = _frame(
            user_active_app="",
            activity_evidence=_coding_evidence(
                app="", source="idle", os_idle="idle", confidence=0.0,
            ),
        )
        shared = _media_frame(
            inferred=_shared_inferred(),
            inferred_stale=False,
            shared_commitment_active=True,
            world_compatible=True,
        )
        self.assertEqual(shared.shared.sharing, "shared")
        notices = notices_from_trigger(
            "activity.session_changed", shared, previous=previous,
        )
        self.assertIn("shared_activity_resumed", _kinds(notices))
        store = LiveUrgeStore()
        created = store.ingest_notices(notices, now_mono_ms=10_000.0)
        self.assertTrue(any(urge.kind == "share_delight" for urge in created))

        coding = _frame(
            user_active_app="Cursor",
            inferred=_shared_inferred(),
            inferred_stale=False,
            shared_commitment_active=True,
            ritual_priors=("watching anime together",),
        )
        coding_notices = notices_from_trigger(
            "activity.session_changed", coding, previous=previous,
        )
        self.assertNotIn("shared_activity_resumed", _kinds(coding_notices))
        self.assertNotIn("shared_commitment", _kinds(coding_notices))
        created = store.ingest_notices(coding_notices, now_mono_ms=11_000.0)
        self.assertFalse(any(
            urge.kind == "share_delight" and urge.state == "candidate"
            for urge in created
        ))

    def test_titles_never_reach_notices_or_prompt(self) -> None:
        frame = _frame(user_active_app="Cursor")
        notices = notices_from_trigger("activity.session_changed", frame)
        self.assertTrue(notices)
        store = LiveUrgeStore()
        created = store.ingest_notices(notices, now_mono_ms=10_000.0)
        prompt = LivePolicyPromptAssembler().assemble(
            frame=frame,
            urges=created,
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
        ).messages[0]["content"]
        rendered = LivePolicyPromptAssembler()._render_situation(frame)
        summary = situation_summary(frame)
        blob = _blob(notices, prompt, rendered)
        self.assertNotIn("secret", blob)
        self.assertNotIn("title=", summary)
        self.assertNotIn("title=", rendered)
        self.assertNotIn("title=", prompt)
        for item in notices:
            self.assertNotEqual(item.subject, frame.shared.user_active_app)
            self.assertLessEqual(len(item.subject), 40)
        self.assertNotIn("title", str(frame.to_payload()))


class InclinationPreviousFrameTests(unittest.TestCase):
    def test_runtime_uses_previous_frame_for_same_session(self) -> None:
        runtime = LiveInclinationRuntime()
        first = _frame(user_active_app="Cursor", trigger_kind="activity.session_changed")
        runtime.apply(
            first,
            trigger_kind="activity.session_changed",
            now_mono_ms=10_000.0,
        )
        self.assertTrue(any(
            urge.kind == "remain_present" for urge in runtime.urges.active()
        ))
        second = _frame(user_active_app="Cursor", trigger_kind="activity.session_changed")
        runtime.apply(
            second,
            trigger_kind="activity.session_changed",
            now_mono_ms=11_000.0,
        )
        self.assertFalse(any(
            item.kind == "focus_started" for item in runtime.urges.last_notices()
        ))


if __name__ == "__main__":
    unittest.main()
