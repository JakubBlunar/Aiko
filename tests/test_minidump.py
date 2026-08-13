"""Tests for the minidump reader.

The reader exists because a native fault left a 900 KB ``.dmp`` on disk
that nothing on the machine could open: no debugger installed, and no
symbols for the 147 MB Rust extension that faulted. What it has to get
right is narrow -- resolve an address to a module, name the faulting
thread, and never raise on a file a dying process truncated -- so the
dumps here are synthetic and built field by field against
``minidumpapiset.h`` rather than checked in as binaries.
"""
from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from app.core.infra import native_crash
from app.core.infra.minidump import Minidump, MinidumpError, summarize


# Header (32 bytes) plus room for the stream directory, reserved up front
# so that every RVA handed out is already final. Shifting them afterwards
# would mean rewriting the RVAs buried inside module and thread-name
# records too, and forgetting one produces a dump that parses into
# convincing garbage.
_PREFIX = 256
_MAX_STREAMS = (_PREFIX - 32) // 12


class _Builder:
    """Assemble a minimal but structurally valid minidump."""

    def __init__(self) -> None:
        self._blob = bytearray(_PREFIX)
        self._streams: list[tuple[int, int, int]] = []  # (type, size, rva)

    def _append(self, payload: bytes) -> int:
        # 4-align so RVAs look like the real thing.
        while len(self._blob) % 4:
            self._blob.append(0)
        rva = len(self._blob)
        self._blob += payload
        return rva

    def string(self, text: str) -> int:
        raw = text.encode("utf-16-le")
        return self._append(struct.pack("<I", len(raw)) + raw + b"\x00\x00")

    def modules(self, entries: list[tuple[int, int, str]]) -> "_Builder":
        name_rvas = [self.string(path) for _b, _s, path in entries]
        payload = bytearray(struct.pack("<I", len(entries)))
        for (base, size, _path), name_rva in zip(entries, name_rvas):
            record = bytearray(108)
            struct.pack_into("<QI", record, 0, base, size)
            struct.pack_into("<I", record, 20, name_rva)
            payload += record
        self._streams.append((4, len(payload), self._append(bytes(payload))))
        return self

    def threads(self, entries: list[tuple[int, bytes]]) -> "_Builder":
        stack_rvas = [self._append(stack) if stack else 0 for _t, stack in entries]
        payload = bytearray(struct.pack("<I", len(entries)))
        for (tid, stack), stack_rva in zip(entries, stack_rvas):
            record = bytearray(48)
            struct.pack_into("<I", record, 0, tid)
            struct.pack_into("<QII", record, 24, 0x1000, len(stack), stack_rva)
            payload += record
        self._streams.append((3, len(payload), self._append(bytes(payload))))
        return self

    def thread_names(self, names: dict[int, str]) -> "_Builder":
        # ThreadId (4) + RVA64 (8) with no padding: these structures are
        # declared under #pragma pack(4), so the stride is 12.
        rvas = {tid: self.string(name) for tid, name in names.items()}
        payload = bytearray(struct.pack("<I", len(names)))
        for tid, rva in rvas.items():
            payload += struct.pack("<IQ", tid, rva)
        self._streams.append((24, len(payload), self._append(bytes(payload))))
        return self

    def exception(self, tid: int, code: int, address: int) -> "_Builder":
        payload = struct.pack("<II", tid, 0) + struct.pack(
            "<IIQQ", code, 0, 0, address
        )
        self._streams.append((6, len(payload), self._append(payload)))
        return self

    def system(self, processors: int) -> "_Builder":
        payload = struct.pack("<HHHBB", 9, 6, 1, processors, 1) + bytes(48)
        self._streams.append((7, len(payload), self._append(payload)))
        return self

    def build(self) -> bytes:
        assert len(self._streams) <= _MAX_STREAMS, "reserve a larger prefix"
        blob = bytearray(self._blob)
        struct.pack_into(
            "<4sIII", blob, 0, b"MDMP", 42899, len(self._streams), 32
        )
        for i, (stype, size, rva) in enumerate(self._streams):
            struct.pack_into("<III", blob, 32 + i * 12, stype, size, rva)
        return bytes(blob)

    def write(self, directory: Path, name: str = "crash.dmp") -> Path:
        path = directory / name
        path.write_bytes(self.build())
        return path


def _dump(builder: _Builder) -> Minidump:
    return Minidump(builder.build())


class AddressResolutionTests(unittest.TestCase):
    """Naming the library that held the bad pointer."""

    def _two_modules(self) -> _Builder:
        return _Builder().modules([
            (0x7FF860000000, 0x8CB3000, r"F:\v\Lib\site-packages\lancedb\_lancedb.pyd"),
            (0x7FF8BF090000, 0x655000, r"C:\Windows\python313.dll"),
        ])

    def test_an_address_inside_a_module_names_it_with_an_offset(self) -> None:
        dump = _dump(self._two_modules())
        self.assertEqual(
            dump.describe(0x7FF866F60528), "_lancedb.pyd+0x6F60528"
        )

    def test_the_first_byte_of_a_module_is_inside_it(self) -> None:
        dump = _dump(self._two_modules())
        self.assertEqual(dump.describe(0x7FF860000000), "_lancedb.pyd+0x0")

    def test_the_byte_past_a_module_is_not_inside_it(self) -> None:
        # Half-open range: an off-by-one here would attribute a fault to
        # whichever module happens to sit below it in memory.
        dump = _dump(self._two_modules())
        self.assertNotIn("_lancedb", dump.describe(0x7FF860000000 + 0x8CB3000))

    def test_an_unowned_address_falls_back_to_hex(self) -> None:
        dump = _dump(self._two_modules())
        self.assertEqual(dump.describe(0x1234), "0x0000000000001234")
        self.assertIsNone(dump.module_for(0x1234))

    def test_a_dump_with_no_module_list_still_answers(self) -> None:
        dump = _dump(_Builder().threads([(1, b"")]))
        self.assertEqual(dump.modules, [])
        self.assertEqual(dump.describe(0x40), "0x0000000000000040")


class ThreadNameTests(unittest.TestCase):
    """Whose thread faulted -- the fact that reorients an investigation."""

    def test_every_named_thread_gets_its_own_name(self) -> None:
        # A stride error still recovers the first entry, so more than one
        # thread is the assertion that matters here.
        builder = (
            _Builder()
            .threads([(0x9124, b""), (0x9125, b""), (0x9126, b"")])
            .thread_names({
                0x9124: "tokio-rt-worker",
                0x9125: "MessageIndexer",
                0x9126: "rag-search_0",
            })
        )
        dump = _dump(builder)
        found = {t.thread_id: t.name for t in dump.threads}
        self.assertEqual(found[0x9124], "tokio-rt-worker")
        self.assertEqual(found[0x9125], "MessageIndexer")
        self.assertEqual(found[0x9126], "rag-search_0")

    def test_a_name_for_an_unlisted_thread_is_ignored(self) -> None:
        # A dump truncated mid-write can carry a name stream that mentions
        # threads the thread list does not.
        builder = (
            _Builder()
            .threads([(0x9124, b"")])
            .thread_names({0x9124: "tokio-rt-worker", 0x4242: "ghost"})
        )
        census = _dump(builder).thread_census()
        self.assertEqual(census, {"tokio-rt-worker": 1})

    def test_threads_without_a_name_stream_are_simply_unnamed(self) -> None:
        dump = _dump(_Builder().threads([(7, b"")]))
        self.assertEqual(dump.threads[0].name, "")
        self.assertEqual(dump.thread_census(), {})

    def test_the_census_counts_threads_by_name(self) -> None:
        builder = (
            _Builder()
            .threads([(1, b""), (2, b""), (3, b"")])
            .thread_names({1: "tokio-rt-worker", 2: "tokio-rt-worker", 3: "MainThread"})
        )
        census = _dump(builder).thread_census()
        self.assertEqual(census["tokio-rt-worker"], 2)
        self.assertEqual(census["MainThread"], 1)
        # Busiest first, so a runaway pool is the first thing read.
        self.assertEqual(list(census)[0], "tokio-rt-worker")


class StackScanTests(unittest.TestCase):
    """The coarse "what had this thread touched" signal."""

    def test_module_pointers_on_the_stack_are_counted(self) -> None:
        stack = struct.pack(
            "<QQQQ",
            0x7FF860001000,  # _lancedb
            0x7FF860002000,  # _lancedb
            0x7FF8BF091000,  # python313
            0x40,            # too small to be a code pointer
        )
        builder = (
            _Builder()
            .modules([
                (0x7FF860000000, 0x8CB3000, r"F:\v\lancedb\_lancedb.pyd"),
                (0x7FF8BF090000, 0x655000, r"C:\Windows\python313.dll"),
            ])
            .threads([(0x11, stack)])
        )
        found = _dump(builder).stack_modules(0x11)
        self.assertEqual(found, {"_lancedb.pyd": 2, "python313.dll": 1})

    def test_a_thread_with_no_saved_stack_reports_nothing(self) -> None:
        builder = _Builder().modules([(0x1000, 0x100, "a.dll")]).threads([(5, b"")])
        self.assertEqual(_dump(builder).stack_modules(5), {})

    def test_an_unknown_thread_reports_nothing(self) -> None:
        builder = _Builder().threads([(5, b"")])
        self.assertEqual(_dump(builder).stack_modules(999), {})


class SummaryTests(unittest.TestCase):
    def _dump_without_exception(self) -> _Builder:
        stack = struct.pack("<QQ", 0x7FF860001000, 0x7FF8B0001000)
        return (
            _Builder()
            .modules([
                (0x7FF860000000, 0x8CB3000, r"F:\v\lancedb\_lancedb.pyd"),
                (0x7FF8B0000000, 0x10000, r"C:\Windows\dbgcore.dll"),
            ])
            .threads([(0x91C4, stack)])
            .thread_names({0x91C4: "tokio-rt-worker"})
            .system(32)
        )

    def test_a_dump_without_an_exception_record_uses_the_crash_record(self) -> None:
        # The common case: dbghelp refuses the exception-pointers struct
        # from inside a filter, so the dump cannot say where it faulted and
        # the caller supplies the address the handler captured.
        summary = _dump(self._dump_without_exception()).summary(
            fault_address=0x7FF866F60528, fault_thread_id=0x91C4
        )
        self.assertFalse(summary["has_exception_stream"])
        self.assertEqual(summary["fault_source"], "crash record")
        self.assertEqual(summary["fault_module"], "_lancedb.pyd+0x6F60528")
        self.assertEqual(summary["fault_thread_name"], "tokio-rt-worker")
        self.assertEqual(summary["processors"], 32)

    def test_the_dumps_own_exception_record_wins_when_present(self) -> None:
        builder = self._dump_without_exception().exception(
            0x91C4, 0xC0000005, 0x7FF860ABCDE
        )
        summary = _dump(builder).summary(fault_address=0xDEAD, fault_thread_id=1)
        self.assertTrue(summary["has_exception_stream"])
        self.assertEqual(summary["fault_source"], "dump exception stream")
        self.assertEqual(summary["exception_code"], "0xC0000005")
        self.assertEqual(summary["fault_thread_id"], 0x91C4)

    def test_our_own_handler_frames_are_reported_separately(self) -> None:
        # dbgcore is on the stack because our filter called
        # MiniDumpWriteDump on the faulting thread. Reading it as evidence
        # would send someone auditing the crash handler.
        summary = _dump(self._dump_without_exception()).summary(
            fault_thread_id=0x91C4
        )
        self.assertIn("dbgcore.dll", summary["fault_stack_handler_frames"])
        self.assertIn("_lancedb.pyd", summary["fault_stack_modules"])

    def test_the_faulting_threads_peer_count_is_reported(self) -> None:
        # 102 threads sharing the faulting thread's name against 32
        # processors is the kind of thing worth seeing next to the fault.
        builder = (
            _Builder()
            .threads([(1, b""), (2, b""), (3, b"")])
            .thread_names({1: "tokio-rt-worker", 2: "tokio-rt-worker", 3: "other"})
            .system(2)
        )
        summary = _dump(builder).summary(fault_thread_id=1)
        self.assertEqual(summary["fault_thread_peers"], 2)
        self.assertEqual(summary["processors"], 2)

    def test_a_thread_id_absent_from_the_dump_is_named_plainly(self) -> None:
        summary = _dump(self._dump_without_exception()).summary(fault_thread_id=4242)
        self.assertEqual(summary["fault_thread_name"], "(unnamed)")


class RobustnessTests(unittest.TestCase):
    """A dying process writes truncated files; none of it may raise."""

    def test_a_non_minidump_is_rejected_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-a-dump.dmp"
            path.write_bytes(b"this is not a minidump")
            self.assertIn("error", summarize(path))
            with self.assertRaises(MinidumpError):
                Minidump.load(path)

    def test_a_missing_file_is_reported_not_raised(self) -> None:
        found = summarize(Path("nowhere") / "absent.dmp")
        self.assertIn("error", found)

    def test_an_empty_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.dmp"
            path.write_bytes(b"")
            self.assertIn("error", summarize(path))

    def test_a_dump_truncated_mid_stream_still_summarizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builder = (
                _Builder()
                .modules([(0x1000, 0x100, "a.dll"), (0x2000, 0x100, "b.dll")])
                .threads([(1, b"")])
            )
            blob = builder.build()
            path = Path(tmp) / "cut.dmp"
            path.write_bytes(blob[: len(blob) // 2])
            found = summarize(path, fault_address=0x1010, fault_thread_id=1)
            self.assertNotIn("Traceback", str(found))

    def test_a_good_dump_round_trips_through_the_file_path_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _Builder().modules(
                [(0x7FF860000000, 0x1000, r"F:\v\_lancedb.pyd")]
            ).threads([(9, b"")]).thread_names({9: "tokio-rt-worker"}).write(Path(tmp))
            found = summarize(path, fault_address=0x7FF860000528, fault_thread_id=9)
            self.assertEqual(found["fault_module"], "_lancedb.pyd+0x528")
            self.assertEqual(found["fault_thread_name"], "tokio-rt-worker")
            self.assertEqual(found["path"], str(path))


class ReportFieldTests(unittest.TestCase):
    """The handler's record must carry the thread name."""

    def test_a_report_names_the_faulting_thread(self) -> None:
        report = native_crash._build_report(
            0xC0000005, 0x7FFAB1234567, 4242, "tokio-rt-worker"
        )
        self.assertEqual(report["thread_name"], "tokio-rt-worker")

    def test_a_report_without_a_name_still_carries_the_field(self) -> None:
        # Consumers read this unconditionally; a missing key would make
        # every crash line special-case its own format.
        report = native_crash._build_report(0xC0000005, 0x1000, 1)
        self.assertEqual(report["thread_name"], "")

    def test_the_current_thread_name_is_a_string(self) -> None:
        # Runs on whatever thread the test runner uses, so assert the
        # contract rather than a value: never raises, always a str.
        self.assertIsInstance(native_crash.current_thread_name(), str)

    def test_a_named_python_thread_reports_its_name(self) -> None:
        import threading

        seen: list[str] = []

        def _run() -> None:
            seen.append(native_crash.current_thread_name())

        thread = threading.Thread(target=_run, name="rag-search_7")
        thread.start()
        thread.join()
        self.assertEqual(seen, ["rag-search_7"])


if __name__ == "__main__":
    unittest.main()
