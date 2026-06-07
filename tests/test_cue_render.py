"""Pure-function tests for :func:`render_cue_block` — chunk 4.

The render layer takes parked :class:`TaskCue` objects and produces
the T6 system-prompt block Aiko sees on her next turn. The tests
here pin:

* Empty cue list → empty string (the assembler's ``if block``
  cascade naturally skips empty providers).
* Three lanes (questions / failures / successes) render in priority
  order, each under its own sub-header.
* Bullet format matches the contract: ``- <title> — <body>``.
* ``max_aggregated`` caps total bullets across all three lanes,
  with the priority order resolving overflow.
* Failure cues use ``error`` field; successes use ``summary``;
  questions use ``summary`` + optional ``options``.
"""
from __future__ import annotations

import unittest

from app.core.tasks.cue_render import render_cue_block
from app.core.tasks.task_cue_store import (
    CUE_KIND_INPUT_NEEDED,
    CUE_KIND_RESULT,
    TaskCue,
)


def _result(
    task_id: str,
    *,
    status: str = "done",
    title: str = "",
    summary: str = "",
    error: str | None = None,
) -> TaskCue:
    return TaskCue(
        task_id=task_id,
        session_key="u",
        kind=CUE_KIND_RESULT,
        parked_at=0.0,
        parked_at_wall=0.0,
        title=title,
        status=status,
        summary=summary,
        error=error,
    )


def _input_needed(
    task_id: str,
    *,
    title: str = "",
    summary: str = "",
    options: tuple[str, ...] | None = None,
) -> TaskCue:
    return TaskCue(
        task_id=task_id,
        session_key="u",
        kind=CUE_KIND_INPUT_NEEDED,
        parked_at=0.0,
        parked_at_wall=0.0,
        title=title,
        status="",
        summary=summary,
        options=options,
    )


class EmptyCuesTests(unittest.TestCase):
    def test_empty_iterable_returns_empty_string(self) -> None:
        self.assertEqual(render_cue_block([]), "")

    def test_empty_iterator_returns_empty_string(self) -> None:
        self.assertEqual(render_cue_block(iter([])), "")


class SuccessLaneTests(unittest.TestCase):
    def test_single_success_renders_header_and_bullet(self) -> None:
        out = render_cue_block(
            [_result("t1", title="file_search", summary="found 3 docs")]
        )
        self.assertIn("Tasks that finished since your last message:", out)
        self.assertIn("- file_search — found 3 docs", out)

    def test_cancelled_status_lands_in_success_lane(self) -> None:
        """Cancellations are surfaced under "tasks that finished" with
        the cancellation reason in summary — they're not failures."""
        out = render_cue_block(
            [_result("t1", status="cancelled", title="download", summary="cancelled by user")]
        )
        self.assertIn("Tasks that finished since your last message:", out)
        self.assertNotIn("Tasks that ran into trouble", out)

    def test_summary_falls_back_to_status_when_empty(self) -> None:
        out = render_cue_block(
            [_result("t1", title="ping", status="done", summary="")]
        )
        self.assertIn("- ping — done", out)

    def test_title_falls_back_when_empty(self) -> None:
        out = render_cue_block([_result("t1", title="", summary="ok")])
        self.assertIn("- the task — ok", out)


class FailureLaneTests(unittest.TestCase):
    def test_single_failure_renders_under_failure_header(self) -> None:
        out = render_cue_block(
            [
                _result(
                    "t1",
                    status="failed",
                    title="file_read",
                    error="file too large (max 256 KB)",
                )
            ]
        )
        self.assertIn(
            "Tasks that ran into trouble since your last message:", out
        )
        self.assertIn("- file_read — file too large (max 256 KB)", out)
        # Successes header must NOT appear when there are none.
        self.assertNotIn("Tasks that finished since your last message:", out)

    def test_failure_without_error_falls_back_to_summary(self) -> None:
        out = render_cue_block(
            [_result("t1", status="failed", title="fetch", summary="something glitchy")]
        )
        self.assertIn("- fetch — something glitchy", out)

    def test_failure_with_neither_error_nor_summary(self) -> None:
        out = render_cue_block(
            [_result("t1", status="failed", title="fetch")]
        )
        self.assertIn("- fetch — failed (no error reported)", out)


class QuestionLaneTests(unittest.TestCase):
    def test_input_needed_uses_dedicated_header(self) -> None:
        out = render_cue_block(
            [_input_needed("t1", title="file_search", summary="which one?")]
        )
        self.assertIn("Tasks waiting on your call since your last message:", out)
        self.assertIn("- file_search — which one?", out)

    def test_input_needed_with_options_renders_inline(self) -> None:
        out = render_cue_block(
            [
                _input_needed(
                    "t1",
                    title="file_read",
                    summary="multiple matches; which one?",
                    options=("a.md", "b.md", "c.md"),
                )
            ]
        )
        self.assertIn(
            "- file_read — multiple matches; which one? [a.md / b.md / c.md]",
            out,
        )

    def test_input_needed_options_truncate_with_ellipsis(self) -> None:
        out = render_cue_block(
            [
                _input_needed(
                    "t1",
                    title="search",
                    summary="lots of matches",
                    options=tuple(f"o{i}" for i in range(10)),
                )
            ]
        )
        # First 6 shown, then ellipsis.
        self.assertIn("o0 / o1 / o2 / o3 / o4 / o5 / …", out)


class CombinedTests(unittest.TestCase):
    def test_three_lanes_render_in_priority_order(self) -> None:
        out = render_cue_block(
            [
                _result("ok1", title="task A", summary="ok"),
                _result("bad1", status="failed", title="task B", error="bad"),
                _input_needed("q1", title="task C", summary="?"),
            ]
        )
        # questions > failures > successes
        q_pos = out.find("Tasks waiting")
        f_pos = out.find("Tasks that ran into trouble")
        s_pos = out.find("Tasks that finished")
        self.assertGreater(q_pos, -1)
        self.assertGreater(f_pos, -1)
        self.assertGreater(s_pos, -1)
        self.assertLess(q_pos, f_pos)
        self.assertLess(f_pos, s_pos)

    def test_blank_lines_separate_sections(self) -> None:
        out = render_cue_block(
            [
                _result("ok1", title="A", summary="ok"),
                _input_needed("q1", title="C", summary="?"),
            ]
        )
        # Question header should be on its own block, separated by a
        # blank line from the success header. Confirm via the
        # "\n\n" substring between the two.
        self.assertIn(
            "Tasks waiting on your call since your last message:\n- C — ?\n\nTasks that finished",
            out,
        )


class AggregationCapTests(unittest.TestCase):
    def test_cap_trims_combined_total(self) -> None:
        cues = [_result(f"t{i}", title=f"task {i}", summary="ok") for i in range(10)]
        out = render_cue_block(cues, max_aggregated=3)
        self.assertEqual(out.count("\n- "), 3)

    def test_cap_priority_questions_first(self) -> None:
        """When the cap forces overflow, questions get the slots
        first; failures next; successes last."""
        cues = [
            _result(f"ok{i}", title=f"S{i}", summary="ok") for i in range(3)
        ] + [
            _result(f"fail{i}", status="failed", title=f"F{i}", error="x")
            for i in range(3)
        ] + [
            _input_needed(f"q{i}", title=f"Q{i}", summary="?")
            for i in range(3)
        ]
        out = render_cue_block(cues, max_aggregated=3)
        # All three slots go to questions.
        self.assertIn("- Q0 — ?", out)
        self.assertIn("- Q1 — ?", out)
        self.assertIn("- Q2 — ?", out)
        self.assertNotIn("F0", out)
        self.assertNotIn("S0", out)

    def test_cap_at_one(self) -> None:
        cues = [
            _result("ok", title="S", summary="ok"),
            _result("fail", status="failed", title="F", error="x"),
            _input_needed("q", title="Q", summary="?"),
        ]
        out = render_cue_block(cues, max_aggregated=1)
        # Only the question survives.
        self.assertIn("- Q — ?", out)
        self.assertNotIn("F —", out)
        self.assertNotIn("S —", out)

    def test_cap_default_matches_documented_default(self) -> None:
        """The default ``max_aggregated`` matches the settings
        default (``5``). Pin this so the doc default and the
        render-side default never drift."""
        # 7 successes; default cap should land 5.
        cues = [_result(f"t{i}", title=f"task {i}", summary="ok") for i in range(7)]
        out = render_cue_block(cues)
        self.assertEqual(out.count("\n- "), 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
