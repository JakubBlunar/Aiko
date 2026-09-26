"""Live mode Pass 27: peek-only numbered urge menu (L10)."""
from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.live.arbiter import arbitrate_live_proposal
from app.core.live.cue_adapter import CueUrgeAdapter
from app.core.live.main_wake import admit_main_wake
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.urge import LiveUrge
from app.core.live.urge_menu import build_urge_menu, render_urge_menu
from app.core.live.urge_store import LiveUrgeStore
from tests.test_live_mode_pass7 import (
    _FakePolicyClient,
    _controller,
    _frame,
    _proposal,
)


def _urge(**kwargs: object) -> LiveUrge:
    fields: dict[str, object] = dict(
        urge_id="u1",
        kind="share_delight",
        subject="shared_activity",
        created_at="2026-09-14T12:00:00Z",
        created_monotonic_ms=9_000.0,
        expires_after_ms=30_000,
        source="test",
        state="candidate",
    )
    fields.update(kwargs)
    return LiveUrge(**fields)  # type: ignore[arg-type]


class UrgeMenuTests(unittest.TestCase):
    def test_cue_purpose_preserves_shares_without_bypassing_question_limit(self) -> None:
        frame = _frame()
        frame = replace(
            frame, constraints=replace(frame.constraints, questions_allowed=False),
        )
        for cue_type, purpose, expected in (
            ("away_activities", "share", ""),
            ("curiosity_seed", "ask", "questions_blocked"),
        ):
            with self.subTest(cue_type=cue_type):
                row = SimpleNamespace(
                    id=4, cue_type=cue_type, subject="film photography",
                    last_surfaced_at=None,
                )
                store = LiveUrgeStore()
                urge = CueUrgeAdapter(pending_provider=lambda row=row: [row]).project(
                    store, now_mono_ms=5_000.0,
                )[0]
                self.assertEqual(urge.purpose, purpose)
                self.assertEqual(
                    admit_main_wake(
                        intent="request_main_speech", user_intent=False,
                        selected_urge_id=urge.urge_id, urges=store.active(),
                        frame=frame, decided_generation=None,
                        now_mono_ms=5_001.0, budget_remaining=1,
                    ),
                    expected,
                )
                self.assertIsNone(row.last_surfaced_at)

    def test_unknown_cue_purpose_cannot_gain_speech(self) -> None:
        row = SimpleNamespace(
            id=4, cue_type="unknown_type", subject="film photography",
            last_surfaced_at=None,
        )
        store = LiveUrgeStore()
        urge = CueUrgeAdapter(pending_provider=lambda: [row]).project(
            store, now_mono_ms=5_000.0,
        )[0]
        self.assertEqual(
            admit_main_wake(
                intent="request_main_speech", user_intent=False,
                selected_urge_id=urge.urge_id, urges=store.active(), frame=_frame(),
                decided_generation=None, now_mono_ms=5_001.0, budget_remaining=1,
            ),
            "unknown_cue_purpose",
        )

    def test_cue_purpose_survives_park_and_expiry(self) -> None:
        row = SimpleNamespace(
            id=4, cue_type="away_activities", subject="film photography",
            last_surfaced_at=None,
        )
        store = LiveUrgeStore()
        adapter = CueUrgeAdapter(pending_provider=lambda: [row])
        urge = adapter.project(store, now_mono_ms=5_000.0)[0]
        store.park(urge.urge_id)
        self.assertEqual(adapter.project(store, now_mono_ms=6_000.0)[0].purpose, "share")
        store.expire_due(50_000.0)
        self.assertEqual(store.all_urges()[0].purpose, "share")

    def test_numbers_ids_and_drops_unsafe_subjects(self) -> None:
        items = build_urge_menu((
            _urge(urge_id="safe", subject="film photography"),
            _urge(
                urge_id="url",
                kind="ask_about_result",
                subject="https://intranet/secret.md",
            ),
            _urge(
                urge_id="deictic",
                kind="ask_about_result",
                subject="see her today",
            ),
            _urge(
                urge_id="long",
                kind="ask_about_result",
                subject="a" * 80,
            ),
        ))
        by_id = {item.urge_id: item for item in items}
        self.assertEqual(by_id["safe"].subject, "film photography")
        self.assertEqual(by_id["url"].subject, "")
        self.assertEqual(by_id["deictic"].subject, "")
        self.assertEqual(by_id["long"].subject, "")
        self.assertEqual(tuple(item.urge_id for item in items), (
            "url", "deictic", "long", "safe",
        ))
        text = render_urge_menu((
            _urge(urge_id="safe", subject="film photography"),
            _urge(urge_id="url", subject="https://intranet/secret.md"),
        ))
        self.assertIn("1. id=safe kind=share_delight subject=film photography", text)
        self.assertNotIn("intranet", text)
        self.assertNotIn("secret.md", text)
        self.assertNotIn("https://", text)

    def test_empty_menu_cannot_request_main_speech(self) -> None:
        empty = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id=""),
            snapshot_generation=1,
            known_urge_ids=(),
        )
        self.assertFalse(empty.accepted)
        self.assertEqual(empty.reason, "missing_urge")
        self.assertFalse(empty.talk_about)
        invented = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="u1"),
            snapshot_generation=1,
            known_urge_ids=(),
        )
        self.assertFalse(invented.accepted)
        self.assertEqual(invented.reason, "unknown_urge")
        self.assertFalse(invented.talk_about)
        wait = arbitrate_live_proposal(
            _proposal(intent="wait", selected_urge_id=""),
            snapshot_generation=1,
            known_urge_ids=(),
        )
        self.assertTrue(wait.accepted)

    def test_unknown_id_rejected_known_id_talk_about(self) -> None:
        unknown = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="nope"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
        )
        self.assertFalse(unknown.accepted)
        self.assertEqual(unknown.reason, "unknown_urge")
        self.assertFalse(unknown.talk_about)
        known = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="u1"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
        )
        self.assertTrue(known.accepted)
        self.assertTrue(known.talk_about)

    def test_prompt_is_numbered_and_omits_cue_bodies(self) -> None:
        row = SimpleNamespace(
            id=4,
            cue_type="curiosity_seed",
            subject="https://intranet/secret.md",
            last_surfaced_at="2026-09-01T00:00:00+00:00",
            text="SECRET BODY about the quarterly plan",
        )
        take = MagicMock()
        store = LiveUrgeStore()
        created = CueUrgeAdapter(pending_provider=lambda: [row]).project(
            store, now_mono_ms=5_000.0,
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].subject, "curiosity_seed")
        self.assertEqual(row.last_surfaced_at, "2026-09-01T00:00:00+00:00")
        take.assert_not_called()
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_frame(),
            urges=created,
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
        )
        text = prompt.messages[0]["content"]
        self.assertIn("CANDIDATE URGES", text)
        self.assertIn(f"1. id={created[0].urge_id} kind=ask_about_result", text)
        self.assertIn("subject=curiosity_seed", text)
        self.assertNotIn("SECRET BODY", text)
        self.assertNotIn("intranet", text)
        self.assertNotIn("quarterly plan", text)
        self.assertGreater(prompt.region_tokens["urges"], 0)

    def test_controller_empty_menu_does_not_wake(self) -> None:
        enqueued: list[dict] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": "u1",
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "want_to_talk",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            on_main_wake=lambda payload: enqueued.append(payload) or True,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": ()},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertFalse(result.accepted)
        self.assertFalse(result.talk_about)
        self.assertEqual(result.reason, "unknown_urge")
        self.assertEqual(enqueued, [])
        self.assertFalse(controller.last_proposal.get("executed"))

    def test_empty_prompt_says_none(self) -> None:
        text = render_urge_menu(())
        self.assertEqual(text, "CANDIDATE URGES: none")
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_frame(),
            urges=(),
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
        ).messages[0]["content"]
        self.assertIn("CANDIDATE URGES: none", prompt)
        self.assertIn("If the menu is none, do not request_main_speech", prompt)


if __name__ == "__main__":
    unittest.main()
