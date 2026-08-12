"""Tests for the native-fault diagnostics.

A ``Windows fatal exception: access violation`` names a Python frame that
is almost always innocent -- a pure-Python statement cannot dereference a
bad pointer, so the frame is wherever the corrupted heap happened to be
touched next. What identifies the culprit is the faulting address's
module, plus whether the process was in a supported native configuration
at all. These tests cover both.
"""
from __future__ import annotations

import sys
import unittest

from app.core.infra import native_crash, native_runtimes


class RuntimeClassificationTests(unittest.TestCase):
    """The hazard logic, fed synthetic module lists."""

    def test_extension_modules_are_not_openmp_runtimes(self) -> None:
        # The trap that makes a naive substring scan useless: several
        # scipy/pyarrow extension modules contain "omp" in their names
        # ("_decomp_lu_cython", "_compute") and would each look like a
        # separate OpenMP runtime.
        paths = [
            r"C:\v\Lib\site-packages\scipy\linalg\_decomp_lu_cython.cp313-win_amd64.pyd",
            r"C:\v\Lib\site-packages\scipy\linalg\_decomp_update.cp313-win_amd64.pyd",
            r"C:\v\Lib\site-packages\pyarrow\_compute.cp313-win_amd64.pyd",
        ]
        found = native_runtimes.classify(paths)
        self.assertEqual(found.openmp, ())
        self.assertFalse(found.hazardous)

    def test_a_single_openmp_runtime_is_fine(self) -> None:
        found = native_runtimes.classify(
            [r"C:\v\Lib\site-packages\torch\lib\libiomp5md.dll"]
        )
        self.assertEqual(found.distinct_openmp, ("libiomp5md.dll",))
        self.assertFalse(found.hazardous)
        self.assertNotIn("DUPLICATE", found.describe())

    def test_two_copies_of_one_runtime_are_flagged(self) -> None:
        # The real configuration this project ships: torch and
        # CTranslate2 each vendor their own Intel OpenMP.
        found = native_runtimes.classify(
            [
                r"C:\v\Lib\site-packages\torch\lib\libiomp5md.dll",
                r"C:\v\Lib\site-packages\ctranslate2\libiomp5md.dll",
            ]
        )
        self.assertTrue(found.hazardous)
        self.assertIn("libiomp5md.dll", found.duplicates)
        self.assertEqual(len(found.duplicates["libiomp5md.dll"]), 2)
        self.assertIn("DUPLICATE", found.describe())

    def test_two_different_runtimes_are_flagged(self) -> None:
        found = native_runtimes.classify(
            [
                r"C:\v\Lib\site-packages\torch\lib\libiomp5md.dll",
                r"C:\v\Lib\site-packages\sklearn\.libs\vcomp140.dll",
            ]
        )
        self.assertTrue(found.hazardous)
        # Different names, so not a "duplicate" -- still more than one
        # OpenMP runtime in the process, which is the actual hazard.
        self.assertEqual(found.duplicates, {})
        self.assertEqual(
            found.distinct_openmp, ("libiomp5md.dll", "vcomp140.dll")
        )

    def test_the_intel_stub_is_not_a_second_runtime(self) -> None:
        # libiompstubs exports the Intel API without a thread pool, so it
        # cannot create the duplicate-state hazard.
        found = native_runtimes.classify(
            [
                r"C:\v\Lib\site-packages\torch\lib\libiomp5md.dll",
                r"C:\v\Lib\site-packages\torch\lib\libiompstubs5md.dll",
            ]
        )
        self.assertEqual(found.distinct_openmp, ("libiomp5md.dll",))
        self.assertFalse(found.hazardous)

    def test_several_blas_builds_are_not_a_hazard(self) -> None:
        # numpy and scipy vendor separate OpenBLAS copies by design.
        found = native_runtimes.classify(
            [
                r"C:\v\Lib\site-packages\numpy.libs\libscipy_openblas64_-abc.dll",
                r"C:\v\Lib\site-packages\scipy.libs\libscipy_openblas-def.dll",
            ]
        )
        self.assertEqual(len(found.blas), 2)
        self.assertFalse(found.hazardous)

    def test_a_process_with_no_math_runtimes_describes_cleanly(self) -> None:
        found = native_runtimes.classify([r"C:\Windows\System32\kernel32.dll"])
        self.assertIn("no OpenMP", found.describe())


class ExceptionCodeTests(unittest.TestCase):
    def test_known_codes_get_names(self) -> None:
        self.assertEqual(native_crash.exception_name(0xC0000005), "access violation")
        self.assertEqual(native_crash.exception_name(0xC00000FD), "stack overflow")
        self.assertTrue(native_crash.is_fatal(0xC0000005))

    def test_unknown_codes_fall_back_to_hex(self) -> None:
        self.assertEqual(native_crash.exception_name(0x12345678), "0x12345678")
        self.assertFalse(native_crash.is_fatal(0x12345678))

    def test_benign_codes_are_not_fatal(self) -> None:
        # The debugger's thread-naming exception and C++ exceptions are
        # routine; writing a minidump for them would be noise.
        self.assertFalse(native_crash.is_fatal(0x406D1388))
        self.assertFalse(native_crash.is_fatal(0xE06D7363))


class ReportTests(unittest.TestCase):
    def test_a_report_carries_what_identifies_the_culprit(self) -> None:
        report = native_crash._build_report(0xC0000005, 0x7FFAB1234567, 4242)
        self.assertEqual(report["type"], "native_crash")
        self.assertEqual(report["exception"], "access violation")
        self.assertEqual(report["exception_code"], "0xC0000005")
        self.assertEqual(report["thread_id"], 4242)
        self.assertIn("7FFAB1234567", str(report["address"]))
        self.assertIn("module", report)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only resolution")
    def test_an_address_resolves_to_the_dll_containing_it(self) -> None:
        # The heart of the feature: given a pointer, name the library.
        # Use a known-good pointer (a kernel32 export) so the assertion
        # does not depend on a crash.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        address = ctypes.cast(kernel32.GetCurrentProcessId, ctypes.c_void_p).value
        resolved = native_crash.module_for_address(int(address or 0))
        self.assertIsNotNone(resolved)
        self.assertIn("kernel32", (resolved or "").lower())

    def test_a_junk_address_resolves_to_nothing(self) -> None:
        self.assertIsNone(native_crash.module_for_address(0))
        if sys.platform == "win32":
            self.assertIsNone(native_crash.module_for_address(0x10))


class ReadBackTests(unittest.TestCase):
    def test_native_records_are_read_back_newest_first(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from app.core.infra import crash_logging

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crashlog.txt"
            lines = [
                # Raw faulthandler output and other record types are
                # interleaved in this file and must be skipped, not
                # treated as corruption.
                "Windows fatal exception: access violation\n",
                '  File "x.py", line 1 in f\n',
                json.dumps({"type": "ui_crash", "message": "unrelated"}) + "\n",
                json.dumps(
                    {"type": "native_crash", "module": "first.dll"}
                ) + "\n",
                json.dumps(
                    {"type": "native_crash", "module": "second.dll"}
                ) + "\n",
            ]
            path.write_text("".join(lines), encoding="utf-8")
            with mock.patch.object(crash_logging, "CRASH_LOG_PATH", path):
                found = crash_logging.read_native_crashes(limit=5)
        self.assertEqual([entry["module"] for entry in found],
                         ["second.dll", "first.dll"])

    def test_a_missing_file_reads_as_empty(self) -> None:
        from pathlib import Path
        from unittest import mock

        from app.core.infra import crash_logging

        with mock.patch.object(
            crash_logging, "CRASH_LOG_PATH", Path("nope") / "missing.txt"
        ):
            self.assertEqual(crash_logging.read_native_crashes(), [])


if __name__ == "__main__":
    unittest.main()
