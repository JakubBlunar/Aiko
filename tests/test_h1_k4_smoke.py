"""H1 + K4 end-to-end smoke test.

Mirrors the plan's manual validation step in code:

  * The user sends a vent message ("yeah I'm just venting about work...").
  * Aiko replies with an inline ``[[arc:support]]`` self-tag.
  * After the post-turn flow runs, both ``messages.dialogue_act`` (user
    row) and ``messages.arc`` (Aiko row) are populated, and
    ``ArcStore`` advances to ``support`` at confidence 0.85.

This exercises the real :mod:`app.core.conversation.dialogue_act_tagger` regex hot
path, the real :func:`parse_arc_tags`, the real :class:`ArcStore`
self-tag write, and the real :class:`ChatDatabase` column updates --
the same code paths the running app drives from
``post_turn_mixin._post_turn_inner_life``.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.conversation_arc import ArcStore, VALID_ARCS
from app.core.conversation.dialogue_act_tagger import tag_regex
from app.core.services.response_text_service import (
    parse_arc_tags,
    strip_all_meta_tags,
)


class H1K4SmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self._tmp.name) / "smoke.db")
        self.store = ArcStore(self.db)
        self.user_id = "smoke-user"
        self.session_id = "smoke-session"

    def tearDown(self) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_vent_user_then_arc_support_aiko_populates_both_columns(self) -> None:
        user_text = (
            "ugh i HATE my boss right now, why does work always have to "
            "be like this. i can't even deal."
        )
        raw_assistant = (
            "Hey, that sounds really heavy. I'm here. [[arc:support]]"
        )

        user_msg_id = self.db.add_message(
            self.session_id, role="user", content=user_text,
        )
        assistant_msg_id = self.db.add_message(
            self.session_id, role="assistant", content=raw_assistant,
        )
        self.assertGreater(user_msg_id, 0)
        self.assertGreater(assistant_msg_id, 0)

        act_result = tag_regex(user_text)
        self.assertEqual(act_result.act, "vent")
        self.assertGreater(act_result.confidence, 0.45)

        self.assertTrue(
            self.db.update_message_dialogue_act(user_msg_id, act_result.act)
        )

        tags = [t for t in parse_arc_tags(raw_assistant) if t in VALID_ARCS]
        self.assertEqual(tags, ["support"])
        self_tag = tags[-1]

        state = self.store.set_from_self_tag(
            self.user_id, self_tag, since_turn=2,
        )
        self.assertEqual(state.arc, "support")
        self.assertAlmostEqual(state.confidence, 0.85, places=6)

        self.assertTrue(
            self.db.update_message_arc(assistant_msg_id, self_tag)
        )
        self.assertTrue(
            self.db.update_message_arc(user_msg_id, state.arc)
        )

        signals = self.db.get_message_signals([user_msg_id, assistant_msg_id])
        self.assertEqual(signals[user_msg_id], ("support", "vent"))
        self.assertEqual(signals[assistant_msg_id], ("support", None))

        cleaned = strip_all_meta_tags(raw_assistant)
        self.assertNotIn("[[arc:", cleaned)
        self.assertNotIn("support]]", cleaned)
        self.assertIn("here", cleaned.lower())

        store_state = self.store.get(self.user_id)
        assert store_state is not None
        self.assertEqual(store_state.arc, "support")
        self.assertAlmostEqual(store_state.confidence, 0.85, places=6)


if __name__ == "__main__":
    unittest.main()
