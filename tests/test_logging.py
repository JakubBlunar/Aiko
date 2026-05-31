"""Tests for the per-turn correlation id, ring buffer, file handler,
read_log_file, and module-level overrides exposed by `app.core.infra.crash_logging`.

These cover the contract documented in AGENTS.md "Debugging via logs" so
future drift in level discipline or formatter shape is caught here.
"""
from __future__ import annotations

import logging
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra import crash_logging
from app.core.infra.crash_logging import (
    LOG_FORMAT,
    _RingBufferHandler,
    _TurnIdFilter,
    configure_logging_full,
    read_log_file,
    set_module_level,
    tail,
)
from app.core.infra.log_context import (
    get_turn_id,
    reset_turn_id,
    set_turn_id,
)


def _make_record(name: str, level: int, msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class TurnIdContextVarTests(unittest.TestCase):
    def test_set_and_get_round_trip(self) -> None:
        token = set_turn_id("abc12345")
        try:
            self.assertEqual(get_turn_id(), "abc12345")
        finally:
            reset_turn_id(token)
        self.assertIsNone(get_turn_id())

    def test_reset_with_invalid_token_clears(self) -> None:
        # Tokens from a different context should not raise.
        token = set_turn_id("first")
        reset_turn_id(token)
        # Calling reset with the same (now-stale) token should not raise
        # and must leave the contextvar empty.
        reset_turn_id(token)
        self.assertIsNone(get_turn_id())

    def test_filter_stamps_record(self) -> None:
        token = set_turn_id("deadbeef")
        try:
            record = _make_record("app.core.test", logging.INFO, "hi")
            _TurnIdFilter().filter(record)
            self.assertEqual(getattr(record, "turn"), "deadbeef")
        finally:
            reset_turn_id(token)

    def test_filter_falls_back_to_dash(self) -> None:
        record = _make_record("app.core.test", logging.INFO, "hi")
        _TurnIdFilter().filter(record)
        self.assertEqual(getattr(record, "turn"), "-")


class RingBufferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = _RingBufferHandler(capacity=8)
        self.handler.setFormatter(logging.Formatter(LOG_FORMAT))
        self.handler.addFilter(_TurnIdFilter())

    def _emit(self, level: int, name: str, msg: str) -> None:
        record = _make_record(name, level, msg)
        # Filter must run for `record.turn` to be populated.
        _TurnIdFilter().filter(record)
        self.handler.emit(record)

    def test_capacity_drops_oldest(self) -> None:
        for i in range(10):
            self._emit(logging.INFO, "app.x", f"line-{i}")
        snap = self.handler.snapshot()
        # Capacity is 8 → only last 8 retained.
        self.assertEqual(len(snap), 8)
        # Newest entry is line-9.
        self.assertIn("line-9", snap[-1][3])

    def test_thread_safety_under_burst(self) -> None:
        # 6 threads × 200 emits each = 1200 records. With capacity 8 we just
        # care that the handler doesn't crash and ends up with exactly 8 rows.
        def worker(idx: int) -> None:
            for j in range(200):
                self._emit(logging.DEBUG, f"app.t{idx}", f"msg-{idx}-{j}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.handler.snapshot()), 8)


class ConfigureLoggingTests(unittest.TestCase):
    """`configure_logging_full` is global; tests cooperate by re-configuring."""

    def tearDown(self) -> None:
        # Restore something close to the default for downstream tests.
        configure_logging_full(level_name="WARNING", file_enabled=False)
        # Drop everything from the ring so cross-test pollution can't leak.
        if crash_logging._RING_HANDLER is not None:
            crash_logging._RING_HANDLER.clear()

    def test_module_levels_applied(self) -> None:
        configure_logging_full(
            level_name="INFO",
            module_levels={"app.test_module_levels.target": "DEBUG"},
            file_enabled=False,
        )
        target = logging.getLogger("app.test_module_levels.target")
        self.assertEqual(target.level, logging.DEBUG)

    def test_ring_handler_attached_and_filterable(self) -> None:
        configure_logging_full(level_name="DEBUG", file_enabled=False)
        log = logging.getLogger("app.core.test_ring_attached")
        log.info("alpha")
        log.warning("beta")
        log.debug("gamma")

        all_lines = tail(n=50, level="DEBUG")
        self.assertTrue(any("alpha" in line for line in all_lines))
        self.assertTrue(any("beta" in line for line in all_lines))
        self.assertTrue(any("gamma" in line for line in all_lines))

        warnings_only = tail(n=50, level="WARNING")
        self.assertFalse(any("alpha" in line for line in warnings_only))
        self.assertFalse(any("gamma" in line for line in warnings_only))
        self.assertTrue(any("beta" in line for line in warnings_only))

    def test_ring_handler_module_substring_filter(self) -> None:
        configure_logging_full(level_name="DEBUG", file_enabled=False)
        logging.getLogger("app.core.something").info("from-core")
        logging.getLogger("app.web.something_else").info("from-web")
        core = tail(n=50, level="INFO", module_contains="core")
        self.assertTrue(any("from-core" in line for line in core))
        self.assertFalse(any("from-web" in line for line in core))

    def test_format_includes_turn_field(self) -> None:
        configure_logging_full(level_name="DEBUG", file_enabled=False)
        token = set_turn_id("c0ffee01")
        try:
            logging.getLogger("app.core.test_turn_format").info("trace-this")
        finally:
            reset_turn_id(token)
        lines = tail(n=10, level="INFO", module_contains="test_turn_format")
        self.assertTrue(lines, "expected at least one matching log line")
        self.assertIn("turn=c0ffee01", lines[-1])


class FileHandlerTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_logging_full(level_name="WARNING", file_enabled=False)
        if crash_logging._RING_HANDLER is not None:
            crash_logging._RING_HANDLER.clear()

    @staticmethod
    def _release_file_handlers() -> None:
        """Close every file handler so Windows releases the log file lock."""
        for handler in list(logging.getLogger("app").handlers):
            try:
                handler.close()
            except Exception:
                pass
        # Re-configure to a no-file state so future asserts don't see stale
        # handlers.
        configure_logging_full(level_name="WARNING", file_enabled=False)

    def test_rotating_file_writes_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            configure_logging_full(
                level_name="DEBUG",
                file_enabled=True,
                file_path=str(path),
                file_max_bytes=1024,
                file_backup_count=2,
            )
            log = logging.getLogger("app.core.test_file_writes")
            for i in range(3):
                log.info("file-line-%d", i)
            for handler in logging.getLogger("app").handlers:
                handler.flush()

            self.assertTrue(path.exists(), "rotating file was not created")
            text = path.read_text(encoding="utf-8")
            self.assertIn("file-line-0", text)
            self.assertIn("file-line-2", text)
            for line in text.strip().splitlines():
                self.assertIn("turn=", line)

            self._release_file_handlers()

    def test_read_log_file_filters(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            configure_logging_full(
                level_name="DEBUG",
                file_enabled=True,
                file_path=str(path),
                file_max_bytes=4096,
                file_backup_count=1,
            )
            log = logging.getLogger("app.core.test_read_log_file")
            log.info("alpha-marker")
            log.warning("beta-marker")
            log.error("gamma-marker")
            for handler in logging.getLogger("app").handlers:
                handler.flush()

            warns = read_log_file(lines=20, level="WARNING", path=str(path))
            self.assertTrue(
                any("beta-marker" in line for line in warns),
                f"WARNING marker missing from {warns!r}",
            )
            self.assertFalse(any("alpha-marker" in line for line in warns))

            grepped = read_log_file(
                lines=20, level="DEBUG", grep="gamma", path=str(path)
            )
            self.assertEqual(len(grepped), 1)
            self.assertIn("gamma-marker", grepped[0])

            self._release_file_handlers()


class SetModuleLevelTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        target = logging.getLogger("app.tests.set_module_level_target")
        original = target.level
        try:
            resolved = set_module_level(target.name, "DEBUG")
            self.assertEqual(target.level, logging.DEBUG)
            self.assertEqual(resolved, "DEBUG")
            resolved_again = set_module_level(target.name, "WARNING")
            self.assertEqual(target.level, logging.WARNING)
            self.assertEqual(resolved_again, "WARNING")
        finally:
            target.setLevel(original)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
