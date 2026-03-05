from __future__ import annotations

import json
import unittest

from app.core.planning.action_planner import ActionPlanner


class ActionPlannerIntentDetectionTests(unittest.TestCase):
    def test_model_detector_uses_llm_for_borderline_request(self) -> None:
        calls: list[list[dict[str, str]]] = []

        def planner_chat(messages: list[dict[str, str]]) -> str:
            calls.append(messages)
            return '{"action_intent": true}'

        planner = ActionPlanner(
            planner_chat=planner_chat,
            history_messages=lambda _n: [],
            extract_json_object=lambda text: json.loads(text),
            trace=lambda *_args, **_kwargs: None,
        )

        self.assertTrue(planner.has_action_intent_with_model("Would you handle Notepad for me?"))
        self.assertEqual(len(calls), 1)

    def test_model_detector_short_circuits_obvious_action_requests(self) -> None:
        call_count = {"n": 0}

        def planner_chat(messages: list[dict[str, str]]) -> str:
            _ = messages
            call_count["n"] += 1
            return '{"action_intent": false}'

        planner = ActionPlanner(
            planner_chat=planner_chat,
            history_messages=lambda _n: [],
            extract_json_object=lambda text: json.loads(text),
            trace=lambda *_args, **_kwargs: None,
        )

        self.assertTrue(planner.has_action_intent_with_model("Click the Save button."))
        self.assertEqual(call_count["n"], 0)


if __name__ == "__main__":
    unittest.main()
