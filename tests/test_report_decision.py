"""Unit tests for the C6 worker-model task-report decision.

Covers :func:`app.core.tasks.report_decision.decide_task_report`:

* No worker client / no model -> conservative park fallback.
* Well-formed verdict JSON -> parsed action + clipped angle.
* Malformed action, bad JSON, transport exception -> park fallback
  (never ``surface_now``).
* The stripped prompt carries provenance + origin_prompt so the worker
  can weigh user-asked vs self-started work.
"""
from __future__ import annotations

import json
import unittest

from app.core.tasks.report_decision import (
    ACTION_DROP,
    ACTION_PARK,
    ACTION_SURFACE,
    PROVENANCE_SELF,
    PROVENANCE_USER,
    ReportVerdict,
    decide_task_report,
)


class _FakeClient:
    """Records the messages it was called with and returns a canned blob."""

    def __init__(self, content: str | None = None, raise_exc: bool = False):
        self._content = content
        self._raise = raise_exc
        self.calls: list[dict] = []

    def chat_json(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self._raise:
            raise RuntimeError("boom")
        return (self._content or "", None)


class FallbackTests(unittest.TestCase):
    def test_no_client_parks(self) -> None:
        v = decide_task_report(
            ollama=None, model="m", title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_PARK)
        self.assertEqual(v.reason, "no_worker_client")
        self.assertEqual(v.angle, "")

    def test_no_model_parks(self) -> None:
        v = decide_task_report(
            ollama=_FakeClient('{"action":"drop"}'), model="",
            title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_PARK)
        self.assertEqual(v.reason, "no_worker_client")

    def test_transport_exception_parks(self) -> None:
        v = decide_task_report(
            ollama=_FakeClient(raise_exc=True), model="m",
            title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_PARK)
        self.assertEqual(v.reason, "llm_error")

    def test_bad_json_parks(self) -> None:
        v = decide_task_report(
            ollama=_FakeClient("not json"), model="m", title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_PARK)
        self.assertEqual(v.reason, "parse_error")

    def test_non_dict_json_parks(self) -> None:
        v = decide_task_report(
            ollama=_FakeClient("[1, 2, 3]"), model="m", title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_PARK)
        self.assertEqual(v.reason, "parse_error")

    def test_unknown_action_parks(self) -> None:
        v = decide_task_report(
            ollama=_FakeClient('{"action":"explode"}'), model="m",
            title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_PARK)
        self.assertEqual(v.reason, "bad_action")


class ParseTests(unittest.TestCase):
    def test_surface_with_angle(self) -> None:
        blob = json.dumps(
            {"action": "surface_now", "angle": "mention the 3 docs you found"}
        )
        v = decide_task_report(
            ollama=_FakeClient(blob), model="m", title="search", summary="3 hits",
        )
        self.assertEqual(v.action, ACTION_SURFACE)
        self.assertEqual(v.angle, "mention the 3 docs you found")
        self.assertEqual(v.reason, "llm")

    def test_drop_action(self) -> None:
        v = decide_task_report(
            ollama=_FakeClient('{"action":"drop","angle":""}'), model="m",
            title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_DROP)

    def test_angle_is_clipped(self) -> None:
        long_angle = "x" * 500
        blob = json.dumps({"action": "park_for_natural_opening", "angle": long_angle})
        v = decide_task_report(
            ollama=_FakeClient(blob), model="m", title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_PARK)
        self.assertLessEqual(len(v.angle), 160)
        self.assertTrue(v.angle.endswith("…"))

    def test_action_case_insensitive(self) -> None:
        v = decide_task_report(
            ollama=_FakeClient('{"action":"SURFACE_NOW"}'), model="m",
            title="t", summary="s",
        )
        self.assertEqual(v.action, ACTION_SURFACE)


class PromptTests(unittest.TestCase):
    def test_user_provenance_and_origin_in_prompt(self) -> None:
        client = _FakeClient('{"action":"surface_now"}')
        decide_task_report(
            ollama=client, model="m", title="my task", summary="done",
            provenance=PROVENANCE_USER, origin_prompt="describe this image",
            user_display_name="Jacob",
        )
        text = json.dumps(client.calls[0]["messages"])
        self.assertIn("Jacob", text)
        self.assertIn("explicitly asked", text)
        self.assertIn("describe this image", text)

    def test_self_provenance_in_prompt(self) -> None:
        client = _FakeClient('{"action":"drop"}')
        decide_task_report(
            ollama=client, model="m", title="t", summary="s",
            provenance=PROVENANCE_SELF,
        )
        text = json.dumps(client.calls[0]["messages"])
        self.assertIn("yourself", text)

    def test_surface_kwarg_passed(self) -> None:
        client = _FakeClient('{"action":"drop"}')
        decide_task_report(ollama=client, model="m", title="t", summary="s")
        self.assertEqual(
            client.calls[0]["kwargs"].get("surface"), "task_report_decision"
        )

    def test_verdict_is_frozen(self) -> None:
        v = ReportVerdict(action=ACTION_PARK)
        with self.assertRaises(Exception):
            v.action = ACTION_SURFACE  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
