"""Tests for the running-tasks inner-life provider — brain-orchestration chunk 6.

Splits into two parts:

* :class:`FormatLineTests` — exhaustive table-driven coverage of
  :func:`_format_running_task_line`. Pure function, no host.
* :class:`RenderBlockTests` — coverage of
  :meth:`InnerLifeProvidersMixin._render_running_tasks_block` via a
  minimal stub-host that satisfies the mixin's read contract
  (``_settings``, ``_user_id``, ``_task_orchestrator``,
  ``user_display_name``). Same pattern as
  ``tests/test_task_orchestration_mixin.py`` — keeps the suite
  fast and decoupled from the 6000-line :class:`SessionController`.
"""
from __future__ import annotations

import dataclasses
import unittest
from typing import Any

from app.core.infra.settings import load_settings
from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
    _format_running_task_line,
)
from app.core.tasks import (
    STATUS_AWAITING_INPUT,
    STATUS_PAUSED,
    STATUS_RUNNING,
)


# Common Unicode ellipsis used by the formatter — pinned here so a
# test failure points at the exact character.
_ELLIPSIS = "…"


@dataclasses.dataclass
class _FakeTaskRow:
    """Minimal stand-in for :class:`TaskRow`.

    Only carries the fields the formatter / provider read. Lets
    tests sidestep the SQLite write path entirely.
    """

    handler_name: str = "handler"
    title: str = ""
    status: str = STATUS_RUNNING
    progress: float | None = None
    last_message: str | None = None


class FormatLineTests(unittest.TestCase):
    """Pin :func:`_format_running_task_line` shape + edge cases."""

    def test_minimum_shape(self) -> None:
        row = _FakeTaskRow(handler_name="file_search")
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running)",
        )

    def test_title_wins_over_handler(self) -> None:
        row = _FakeTaskRow(handler_name="file_search", title="meetings")
        self.assertEqual(
            _format_running_task_line(row),
            "- meetings (running)",
        )

    def test_blank_title_falls_back_to_handler(self) -> None:
        row = _FakeTaskRow(handler_name="file_search", title="   ")
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running)",
        )

    def test_progress_renders_as_percent(self) -> None:
        row = _FakeTaskRow(
            handler_name="file_search", progress=0.6,
        )
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running, 60%)",
        )

    def test_progress_zero_renders(self) -> None:
        row = _FakeTaskRow(handler_name="file_search", progress=0.0)
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running, 0%)",
        )

    def test_progress_one_renders(self) -> None:
        row = _FakeTaskRow(handler_name="file_search", progress=1.0)
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running, 100%)",
        )

    def test_progress_above_one_clamps(self) -> None:
        row = _FakeTaskRow(handler_name="file_search", progress=1.5)
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running, 100%)",
        )

    def test_progress_below_zero_clamps(self) -> None:
        row = _FakeTaskRow(handler_name="file_search", progress=-0.2)
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running, 0%)",
        )

    def test_progress_garbage_drops_silently(self) -> None:
        row = _FakeTaskRow(
            handler_name="file_search", progress="oops",  # type: ignore[arg-type]
        )
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running)",
        )

    def test_last_message_renders_quoted(self) -> None:
        row = _FakeTaskRow(
            handler_name="file_search",
            last_message="scanning directory tree",
        )
        self.assertEqual(
            _format_running_task_line(row),
            '- file_search (running, "scanning directory tree")',
        )

    def test_last_message_truncated(self) -> None:
        long = "x" * 100
        row = _FakeTaskRow(
            handler_name="file_search", last_message=long,
        )
        out = _format_running_task_line(row)
        self.assertIn(_ELLIPSIS, out)
        # The truncated message should be 60 chars max in the quoted
        # portion (59 + ellipsis).
        quoted = out.split('"')[1]
        self.assertEqual(len(quoted), 60)

    def test_label_truncated(self) -> None:
        long = "x" * 60
        row = _FakeTaskRow(handler_name=long)
        out = _format_running_task_line(row)
        self.assertIn(_ELLIPSIS, out)
        # 39 chars of label + ellipsis = 40 chars.
        label = out.split(" (")[0].lstrip("- ")
        self.assertEqual(len(label), 40)

    def test_awaiting_input_status(self) -> None:
        row = _FakeTaskRow(
            handler_name="file_search", status=STATUS_AWAITING_INPUT,
        )
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (awaiting_input)",
        )

    def test_progress_and_last_message_combine(self) -> None:
        row = _FakeTaskRow(
            handler_name="file_search",
            progress=0.45,
            last_message="halfway there",
        )
        self.assertEqual(
            _format_running_task_line(row),
            '- file_search (running, 45%, "halfway there")',
        )

    def test_blank_last_message_dropped(self) -> None:
        row = _FakeTaskRow(
            handler_name="file_search",
            last_message="   ",
        )
        self.assertEqual(
            _format_running_task_line(row),
            "- file_search (running)",
        )


class _FakeOrchestrator:
    """Stub for :class:`TaskOrchestrator` exposing only ``list_running``.

    The mixin's provider only calls ``list_running(user_id=...)`` so
    a richer stub isn't needed. Tests inject the row set directly.
    """

    def __init__(self, rows: list[_FakeTaskRow]) -> None:
        self.rows = rows
        self.last_user_id: Any = None

    def list_running(self, *, user_id: Any = None) -> list[_FakeTaskRow]:
        self.last_user_id = user_id
        return self.rows


class _Host(InnerLifeProvidersMixin):
    """Stub host with just the attributes the running-tasks
    provider reads. ``user_display_name`` is a property on the
    real :class:`SessionController` so we mirror that shape."""

    def __init__(
        self,
        *,
        settings: Any,
        user_id: str = "test-user",
        user_display_name: str = "Jacob",
        orchestrator: _FakeOrchestrator | None = None,
    ) -> None:
        self._settings = settings
        self._user_id = user_id
        self._user_display_name_value = user_display_name
        self._task_orchestrator = orchestrator

    @property
    def user_display_name(self) -> str:
        return self._user_display_name_value


def _settings_with(**agent_overrides: Any):
    base = load_settings(None)
    if not agent_overrides:
        return base
    agent = dataclasses.replace(base.agent, **agent_overrides)
    return dataclasses.replace(base, agent=agent)


class RenderBlockTests(unittest.TestCase):
    """Pin :meth:`_render_running_tasks_block` semantics."""

    def test_no_orchestrator_returns_empty(self) -> None:
        host = _Host(settings=_settings_with(), orchestrator=None)
        self.assertEqual(host._render_running_tasks_block(), "")

    def test_master_switch_off_returns_empty(self) -> None:
        # The block switch off → empty regardless of running tasks.
        host = _Host(
            settings=_settings_with(tasks_running_block_enabled=False),
            orchestrator=_FakeOrchestrator(
                [_FakeTaskRow(handler_name="file_search")]
            ),
        )
        self.assertEqual(host._render_running_tasks_block(), "")

    def test_tasks_disabled_returns_empty(self) -> None:
        # The whole task subsystem disabled → empty.
        host = _Host(
            settings=_settings_with(tasks_enabled=False),
            orchestrator=_FakeOrchestrator(
                [_FakeTaskRow(handler_name="file_search")]
            ),
        )
        self.assertEqual(host._render_running_tasks_block(), "")

    def test_no_running_tasks_returns_empty(self) -> None:
        host = _Host(
            settings=_settings_with(),
            orchestrator=_FakeOrchestrator([]),
        )
        self.assertEqual(host._render_running_tasks_block(), "")

    def test_one_running_task_renders_header_plus_bullet(self) -> None:
        host = _Host(
            settings=_settings_with(),
            user_display_name="Jacob",
            orchestrator=_FakeOrchestrator(
                [_FakeTaskRow(handler_name="file_search", progress=0.6)]
            ),
        )
        out = host._render_running_tasks_block()
        self.assertIn("Tasks running for Jacob right now:", out)
        self.assertIn("- file_search (running, 60%)", out)

    def test_multiple_running_tasks_each_on_a_line(self) -> None:
        rows = [
            _FakeTaskRow(handler_name="file_search", progress=0.5),
            _FakeTaskRow(
                handler_name="file_read", status=STATUS_AWAITING_INPUT,
            ),
        ]
        host = _Host(
            settings=_settings_with(),
            orchestrator=_FakeOrchestrator(rows),
        )
        out = host._render_running_tasks_block()
        lines = out.split("\n")
        # 1 header + 2 bullets = 3 lines.
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], "- file_search (running, 50%)")
        self.assertEqual(lines[2], "- file_read (awaiting_input)")

    def test_paused_tasks_filtered_out(self) -> None:
        # Paused tasks survive recovery but aren't actively working —
        # they don't belong in the "currently doing" cluster.
        rows = [
            _FakeTaskRow(handler_name="file_search", status=STATUS_PAUSED),
            _FakeTaskRow(handler_name="file_read", status=STATUS_RUNNING),
        ]
        host = _Host(
            settings=_settings_with(),
            orchestrator=_FakeOrchestrator(rows),
        )
        out = host._render_running_tasks_block()
        self.assertIn("file_read", out)
        self.assertNotIn("file_search", out)

    def test_passes_user_id_to_orchestrator(self) -> None:
        orch = _FakeOrchestrator([])
        host = _Host(
            settings=_settings_with(),
            user_id="jacob-1",
            orchestrator=orch,
        )
        host._render_running_tasks_block()
        self.assertEqual(orch.last_user_id, "jacob-1")

    def test_aggregation_cap_truncates_with_overflow_line(self) -> None:
        # Default cap is 5 bullets — a 7-task user gets 5 bullets +
        # "and 2 more" overflow line.
        rows = [
            _FakeTaskRow(handler_name=f"task_{i}") for i in range(7)
        ]
        host = _Host(
            settings=_settings_with(),
            orchestrator=_FakeOrchestrator(rows),
        )
        out = host._render_running_tasks_block()
        lines = out.split("\n")
        # 1 header + 5 bullets + 1 overflow line = 7 lines.
        self.assertEqual(len(lines), 7)
        self.assertIn("and 2 more", lines[-1])

    def test_orchestrator_exception_returns_empty(self) -> None:
        # Best-effort: a broken orchestrator must NOT crash the
        # prompt build. Returns "" + DEBUG log.
        class _Boom:
            def list_running(self, *, user_id=None):
                raise RuntimeError("orchestrator boom")

        host = _Host(
            settings=_settings_with(),
            orchestrator=_Boom(),  # type: ignore[arg-type]
        )
        # No exception, empty block.
        self.assertEqual(host._render_running_tasks_block(), "")


if __name__ == "__main__":
    unittest.main()
