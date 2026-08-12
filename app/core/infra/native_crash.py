"""Capture the *native* side of a fatal crash on Windows.

``faulthandler`` already writes the Python stack for an access
violation, but a Python frame is only ever where a native fault
*surfaced* -- a pure-Python statement cannot dereference a bad pointer.
The frame at the top of such a dump is usually just the busiest
allocation site in the process, which sends you auditing innocent code.

What actually identifies the culprit is the faulting address, so this
module installs an unhandled-exception filter that records:

* the exception code and faulting address,
* **the DLL that address falls inside** -- normally enough on its own,
* whether the process held duplicate OpenMP runtimes at the time, and
* a minidump, so a real debugger can walk the native stack.

The filter returns ``EXCEPTION_CONTINUE_SEARCH``, so the process still
dies exactly as it did before; this only adds evidence on the way out.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import logging
import sys
import threading


log = logging.getLogger("app.native_crash")

_EXCEPTION_CONTINUE_SEARCH = 0

# Only the codes worth writing a dump for. Anything else (C++ exceptions,
# the debugger's thread-naming exception, ...) is left alone.
_FATAL_CODES: dict[int, str] = {
    0xC0000005: "access violation",
    0xC00000FD: "stack overflow",
    0xC0000094: "integer divide by zero",
    0xC0000091: "float overflow",
    0xC000001D: "illegal instruction",
    0xC0000096: "privileged instruction",
    0xC0000006: "in-page error",
    0xC000008C: "array bounds exceeded",
    0xC0000374: "heap corruption",
}

# MiniDumpNormal | MiniDumpWithUnloadedModules | MiniDumpWithThreadInfo.
# Enough for module attribution and a native backtrace per thread,
# without the multi-gigabyte file a full-memory dump produces once torch
# is loaded.
_DUMP_TYPE = 0x0000 | 0x0020 | 0x1000

_installed = False
_handler_ref = None  # keeps the ctypes callback alive
_in_handler = threading.Lock()


def exception_name(code: int) -> str:
    """Human name for a Windows exception code, hex fallback."""
    return _FATAL_CODES.get(int(code) & 0xFFFFFFFF, "0x%08X" % (int(code) & 0xFFFFFFFF))


def is_fatal(code: int) -> bool:
    return (int(code) & 0xFFFFFFFF) in _FATAL_CODES


def module_for_address(address: int) -> str | None:
    """Return the path of the module containing ``address``.

    This is the single most useful fact about a native fault: it names
    the library holding the bad pointer. ``None`` when the address is
    not inside any loaded module (JIT/heap/stack, or already unloaded).
    """
    if sys.platform != "win32" or not address:
        return None
    import ctypes
    import ctypes.wintypes as wt

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleExW.argtypes = [
            wt.DWORD,
            wt.LPCWSTR,
            ctypes.POINTER(wt.HMODULE),
        ]
        kernel32.GetModuleHandleExW.restype = wt.BOOL
        kernel32.GetModuleFileNameW.argtypes = [wt.HMODULE, wt.LPWSTR, wt.DWORD]
        kernel32.GetModuleFileNameW.restype = wt.DWORD
    except Exception:
        return None

    # FROM_ADDRESS | UNCHANGED_REFCOUNT: look the module up by a pointer
    # into it without taking a reference we would have to release.
    flags = 0x00000004 | 0x00000002
    handle = wt.HMODULE()
    if not kernel32.GetModuleHandleExW(
        flags, ctypes.cast(ctypes.c_void_p(address), wt.LPCWSTR), ctypes.byref(handle)
    ):
        return None
    buffer = ctypes.create_unicode_buffer(2048)
    if not kernel32.GetModuleFileNameW(handle, buffer, 2048):
        return None
    return buffer.value or None


def _write_minidump(dump_path: Path, exception_pointers, thread_id: int) -> str:
    """Write a minidump for the current fault.

    Returns ``""`` on success, else a short reason. A silent failure here
    is worse than useless -- it leaves you believing a dump exists -- so
    the reason travels into the crash record.

    Every signature is declared explicitly: without ``argtypes`` ctypes
    narrows pointer-sized arguments to C ``int``, and the call fails
    while leaving a zero-byte file behind.
    """
    import ctypes
    import ctypes.wintypes as wt

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
    except Exception as exc:
        return "dbghelp unavailable: %s" % type(exc).__name__

    class _MinidumpExceptionInformation(ctypes.Structure):
        _fields_ = [
            ("ThreadId", wt.DWORD),
            ("ExceptionPointers", ctypes.c_void_p),
            ("ClientPointers", wt.BOOL),
        ]

    try:
        kernel32.CreateFileW.argtypes = [
            wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
            wt.DWORD, wt.DWORD, wt.HANDLE,
        ]
        kernel32.CreateFileW.restype = wt.HANDLE
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        kernel32.GetCurrentProcessId.restype = wt.DWORD
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        dbghelp.MiniDumpWriteDump.argtypes = [
            wt.HANDLE,
            wt.DWORD,
            wt.HANDLE,
            wt.DWORD,
            ctypes.POINTER(_MinidumpExceptionInformation),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        dbghelp.MiniDumpWriteDump.restype = wt.BOOL
    except Exception as exc:
        return "signature setup failed: %s" % type(exc).__name__

    generic_write = 0x40000000
    create_always = 2
    file_attribute_normal = 0x80
    invalid_handle = wt.HANDLE(-1).value

    handle = kernel32.CreateFileW(
        str(dump_path), generic_write, 0, None, create_always,
        file_attribute_normal, None,
    )
    if not handle or handle == invalid_handle:
        return "CreateFileW failed err=%d" % ctypes.get_last_error()
    reason = ""
    try:
        info = _MinidumpExceptionInformation(
            ThreadId=wt.DWORD(int(thread_id)),
            ExceptionPointers=ctypes.cast(exception_pointers, ctypes.c_void_p),
            ClientPointers=wt.BOOL(False),
        )

        def _attempt(exception_param) -> int:
            ctypes.set_last_error(0)
            return dbghelp.MiniDumpWriteDump(
                kernel32.GetCurrentProcess(),
                kernel32.GetCurrentProcessId(),
                handle,
                _DUMP_TYPE,
                exception_param,
                None,
                None,
            )

        if not _attempt(ctypes.byref(info)):
            first = ctypes.get_last_error()
            # dbghelp routinely refuses the exception-pointers struct from
            # inside a filter (ERROR_NOACCESS). The dump is still worth
            # having without it: every thread's native stack is present,
            # which is what names the faulting library. The exception code
            # and address are already recorded alongside.
            if not _attempt(None):
                reason = "MiniDumpWriteDump failed err=%d (and err=%d with " \
                    "exception info)" % (ctypes.get_last_error(), first)
    except Exception as exc:
        reason = "MiniDumpWriteDump raised: %s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass
    if reason:
        # Don't leave a zero-byte dump implying we captured something.
        try:
            dump_path.unlink(missing_ok=True)
        except Exception:
            pass
    return reason


def _build_report(code: int, address: int, thread_id: int) -> dict[str, object]:
    """Assemble the record for a fault. Split out so it is testable."""
    module = module_for_address(address)
    report: dict[str, object] = {
        "type": "native_crash",
        "exception": exception_name(code),
        "exception_code": "0x%08X" % (int(code) & 0xFFFFFFFF),
        "address": "0x%016X" % int(address),
        "module": module or "unknown",
        "thread_id": int(thread_id),
    }
    try:
        from app.core.infra import native_runtimes

        found = native_runtimes.inventory()
        report["openmp_runtimes"] = list(found.distinct_openmp)
        report["duplicate_openmp"] = {
            name: list(dirs) for name, dirs in found.duplicates.items()
        }
        report["unsupported_runtime_config"] = found.hazardous
    except Exception:
        pass
    return report


def install(
    *,
    dump_dir: Path,
    record: Callable[[dict[str, object]], None] | None = None,
) -> bool:
    """Install the unhandled-exception filter. No-op off Windows.

    ``record`` receives the structured report (the caller decides where
    it lands); it is invoked inside a crashing process, so it must be
    cheap and must not raise.
    """
    global _installed, _handler_ref
    if _installed or sys.platform != "win32":
        return False

    import ctypes
    import ctypes.wintypes as wt

    class _ExceptionRecord(ctypes.Structure):
        pass

    _ExceptionRecord._fields_ = [
        ("ExceptionCode", wt.DWORD),
        ("ExceptionFlags", wt.DWORD),
        ("ExceptionRecord", ctypes.POINTER(_ExceptionRecord)),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", wt.DWORD),
        ("ExceptionInformation", ctypes.c_size_t * 15),
    ]

    class _ExceptionPointers(ctypes.Structure):
        _fields_ = [
            ("ExceptionRecord", ctypes.POINTER(_ExceptionRecord)),
            ("ContextRecord", ctypes.c_void_p),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return False

    filter_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.POINTER(_ExceptionPointers)
    )

    def _filter(pointers) -> int:
        # Always hand the fault onward so the process dies as it did
        # before; we are only here to leave evidence behind.
        try:
            if not pointers:
                return _EXCEPTION_CONTINUE_SEARCH
            record_ptr = pointers[0].ExceptionRecord
            if not record_ptr:
                return _EXCEPTION_CONTINUE_SEARCH
            code = int(record_ptr[0].ExceptionCode)
            if not is_fatal(code):
                return _EXCEPTION_CONTINUE_SEARCH
            # A fault raised *by this handler* must not recurse.
            if not _in_handler.acquire(blocking=False):
                return _EXCEPTION_CONTINUE_SEARCH
            try:
                address = int(record_ptr[0].ExceptionAddress or 0)
                thread_id = int(kernel32.GetCurrentThreadId())
                report = _build_report(code, address, thread_id)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                dump_path = dump_dir / ("crash-%s-%d.dmp" % (stamp, thread_id))
                try:
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    failure = _write_minidump(dump_path, pointers, thread_id)
                except Exception as exc:
                    failure = "dump attempt raised: %s" % type(exc).__name__
                report["minidump"] = "" if failure else str(dump_path)
                if failure:
                    report["minidump_error"] = failure
                if record is not None:
                    record(report)
            finally:
                _in_handler.release()
        except Exception:
            pass
        return _EXCEPTION_CONTINUE_SEARCH

    callback = filter_type(_filter)
    try:
        kernel32.SetUnhandledExceptionFilter.argtypes = [filter_type]
        kernel32.SetUnhandledExceptionFilter.restype = ctypes.c_void_p
        kernel32.SetUnhandledExceptionFilter(callback)
    except Exception:
        return False

    _handler_ref = callback  # must outlive install()
    _installed = True
    return True
