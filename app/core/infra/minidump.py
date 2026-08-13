"""Read the parts of a Windows minidump that identify a native fault.

:mod:`app.core.infra.native_crash` writes a dump when the process dies on
an access violation, but reading one has until now needed a debugger
(``cdb`` / WinDbg) that is not installed on the dev box, and symbols for a
147 MB Rust extension that nobody ships. So the dump sat on disk as
evidence nobody could open.

This module extracts, without symbols and without dbghelp, the three
facts that in practice decide where to look:

* **which module the faulting address falls inside** -- the library
  holding the bad pointer, and normally the whole answer;
* **the faulting thread's name** -- this is the one that reorients an
  investigation. A fault on ``tokio-rt-worker`` is inside a dependency's
  own async runtime and no amount of locking on our side can guard it; the
  same fault on ``MessageIndexer`` or ``rag-search`` would be ours;
* **the thread census** -- how many threads of each name existed, which
  says whether a native runtime was sized as expected or had grown.

It also walks the faulting thread's saved stack and reports which modules
appear on it. Without symbols that is a *coarse* signal and deliberately
presented as one: the scan reports any 8-byte value that happens to land
inside a loaded module, so it picks up dead frames and stale pointers
alongside live return addresses. It is enough to tell "Python called into
this" apart from "this ran entirely inside the native runtime", which is
the distinction that matters, and not enough to claim a call order.

Format reference: ``MINIDUMP_HEADER`` / ``_DIRECTORY`` / ``_MODULE`` /
``_THREAD`` / ``_THREAD_NAME`` in ``minidumpapiset.h``. Only the streams
listed in :class:`_Stream` are read; anything else is skipped.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
import struct


class MinidumpError(Exception):
    """Raised when a file is not a readable minidump."""


class _Stream(IntEnum):
    THREAD_LIST = 3
    MODULE_LIST = 4
    EXCEPTION = 6
    SYSTEM_INFO = 7
    THREAD_NAMES = 24


_SIGNATURE = b"MDMP"

# Fixed record sizes from minidumpapiset.h. Spelled out rather than built
# with struct.calcsize so the layout is auditable against the header.
_MODULE_SIZE = 108
_THREAD_SIZE = 48
# 4, not 8-aligned to 16: these structures live under #pragma pack(4).
_THREAD_NAME_SIZE = 12

# Modules that are on the faulting thread's stack because *we* put them
# there: the unhandled-exception filter is a ctypes callback, so the
# interpreter, libffi and dbghelp's dump writer all appear above the frames
# that actually faulted. Reported separately instead of silently dropped.
HANDLER_MODULES = frozenset({
    "dbgcore.dll",
    "dbghelp.dll",
    "libffi-8.dll",
    "_ctypes.pyd",
})

# OS modules that appear on every stack and carry no attribution value.
_SYSTEM_MODULES = frozenset({
    "ntdll.dll",
    "kernelbase.dll",
    "kernel32.dll",
    "ucrtbase.dll",
    "msvcrt.dll",
    "user32.dll",
    "sechost.dll",
    "rpcrt4.dll",
})


@dataclass(frozen=True, slots=True)
class Module:
    """One loaded module and the address range it occupies."""

    base: int
    size: int
    path: str

    @property
    def name(self) -> str:
        return Path(self.path).name

    def contains(self, address: int) -> bool:
        return self.base <= address < self.base + self.size

    def offset_of(self, address: int) -> int:
        return address - self.base


@dataclass(frozen=True, slots=True)
class Thread:
    """One thread, its name when the dump carries one, and its saved stack."""

    thread_id: int
    stack_start: int
    stack_size: int
    stack_rva: int
    name: str = ""


@dataclass(frozen=True, slots=True)
class Exception_:
    """The dump's own exception record, when it has one.

    Frequently absent: ``MiniDumpWriteDump`` refuses the
    ``MINIDUMP_EXCEPTION_INFORMATION`` struct from inside an exception
    filter often enough that :mod:`app.core.infra.native_crash` retries
    without it, and the resulting dump cannot say where it faulted. The
    crash *record* carries the code and address in that case -- see
    :meth:`Minidump.summary`, which takes them as a fallback.
    """

    thread_id: int
    code: int
    address: int


class Minidump:
    """A parsed minidump. Construct via :meth:`load`."""

    __slots__ = ("_blob", "_streams", "exception", "modules", "processors", "threads")

    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self._streams = self._read_directory()
        self.modules: list[Module] = self._read_modules()
        self.threads: list[Thread] = self._read_threads()
        self.exception: Exception_ | None = self._read_exception()
        self.processors: int = self._read_processors()

    @classmethod
    def load(cls, path: str | Path) -> "Minidump":
        try:
            blob = Path(path).read_bytes()
        except OSError as exc:
            raise MinidumpError(f"cannot read {path}: {exc}") from exc
        return cls(blob)

    # ── parsing ─────────────────────────────────────────────────────────

    def _read_directory(self) -> dict[int, tuple[int, int]]:
        blob = self._blob
        if len(blob) < 32 or blob[:4] != _SIGNATURE:
            raise MinidumpError("not a minidump (bad signature)")
        _sig, _ver, count, dir_rva = struct.unpack_from("<4sIII", blob, 0)
        streams: dict[int, tuple[int, int]] = {}
        for i in range(count):
            off = dir_rva + i * 12
            if off + 12 > len(blob):
                break
            stype, size, rva = struct.unpack_from("<III", blob, off)
            # Type 0 is padding and repeats; a real stream never does.
            if stype and stype not in streams:
                streams[stype] = (size, rva)
        return streams

    def _string(self, rva: int) -> str:
        """Read a ``MINIDUMP_STRING``: byte length, then UTF-16LE."""
        (nbytes,) = struct.unpack_from("<I", self._blob, rva)
        raw = self._blob[rva + 4:rva + 4 + max(0, nbytes)]
        return raw.decode("utf-16-le", "replace")

    def _read_modules(self) -> list[Module]:
        entry = self._streams.get(_Stream.MODULE_LIST)
        if entry is None:
            return []
        _size, rva = entry
        (count,) = struct.unpack_from("<I", self._blob, rva)
        out: list[Module] = []
        for i in range(count):
            off = rva + 4 + i * _MODULE_SIZE
            if off + _MODULE_SIZE > len(self._blob):
                break
            base, size = struct.unpack_from("<QI", self._blob, off)
            (name_rva,) = struct.unpack_from("<I", self._blob, off + 20)
            try:
                path = self._string(name_rva)
            except Exception:
                path = ""
            out.append(Module(base=base, size=size, path=path))
        out.sort(key=lambda m: m.base)
        return out

    def _read_threads(self) -> list[Thread]:
        entry = self._streams.get(_Stream.THREAD_LIST)
        if entry is None:
            return []
        _size, rva = entry
        (count,) = struct.unpack_from("<I", self._blob, rva)
        out: list[Thread] = []
        for i in range(count):
            off = rva + 4 + i * _THREAD_SIZE
            if off + _THREAD_SIZE > len(self._blob):
                break
            (tid,) = struct.unpack_from("<I", self._blob, off)
            start, dsize, drva = struct.unpack_from("<QII", self._blob, off + 24)
            out.append(
                Thread(thread_id=tid, stack_start=start, stack_size=dsize,
                       stack_rva=drva)
            )
        names = self._read_thread_names({t.thread_id for t in out})
        if names:
            out = [
                Thread(
                    thread_id=t.thread_id,
                    stack_start=t.stack_start,
                    stack_size=t.stack_size,
                    stack_rva=t.stack_rva,
                    name=names.get(t.thread_id, ""),
                )
                for t in out
            ]
        return out

    def _read_thread_names(self, known_ids: set[int]) -> dict[int, str]:
        """Parse ``ThreadNamesStream``: ``{thread id: name}``.

        ``MINIDUMP_THREAD_NAME`` is a 32-bit thread id followed by a 64-bit
        ``RVA64``. Natural alignment would pad that to 16 bytes, but
        ``minidumpapiset.h`` declares these structures under
        ``#pragma pack(4)``, so the real stride is 12 with the RVA at
        offset 4 -- confirmed against a dump written by the in-box dbghelp.
        Reading the RVA as 32-bit also appears to work, because the high
        half is zero for any dump below 4 GB, but it is the wrong field
        width and is not what this does.

        ``known_ids`` is not needed to choose a layout, only to skip names
        for threads the thread list does not contain (a dump truncated
        mid-write can carry one without the other).
        """
        entry = self._streams.get(_Stream.THREAD_NAMES)
        if entry is None:
            return {}
        _size, rva = entry
        try:
            (count,) = struct.unpack_from("<I", self._blob, rva)
        except Exception:
            return {}
        found: dict[int, str] = {}
        for i in range(count):
            off = rva + 4 + i * _THREAD_NAME_SIZE
            if off + _THREAD_NAME_SIZE > len(self._blob):
                break
            (tid, name_rva) = struct.unpack_from("<IQ", self._blob, off)
            if not 0 < name_rva < len(self._blob):
                continue
            if known_ids and tid not in known_ids:
                continue
            try:
                name = self._string(int(name_rva))
            except Exception:
                continue
            # A misparse yields control characters rather than a name; keep
            # it out so a bad read looks empty instead of authoritative.
            if not name or any(ch < " " for ch in name):
                continue
            found[tid] = name
        return found

    def _read_exception(self) -> Exception_ | None:
        entry = self._streams.get(_Stream.EXCEPTION)
        if entry is None:
            return None
        _size, rva = entry
        try:
            (tid,) = struct.unpack_from("<I", self._blob, rva)
            code, _flags, _nested, address = struct.unpack_from(
                "<IIQQ", self._blob, rva + 8
            )
        except Exception:
            return None
        return Exception_(thread_id=tid, code=code, address=address)

    def _read_processors(self) -> int:
        entry = self._streams.get(_Stream.SYSTEM_INFO)
        if entry is None:
            return 0
        _size, rva = entry
        try:
            _arch, _lvl, _rev, ncpu, _type = struct.unpack_from("<HHHBB", self._blob, rva)
        except Exception:
            return 0
        return int(ncpu)

    # ── queries ─────────────────────────────────────────────────────────

    def module_for(self, address: int) -> Module | None:
        for module in self.modules:
            if module.contains(address):
                return module
        return None

    def describe(self, address: int) -> str:
        """``"_lancedb.pyd+0x6EC0528"``, or a bare hex address if unowned."""
        module = self.module_for(address)
        if module is None:
            return "0x%016X" % int(address)
        return "%s+0x%X" % (module.name, module.offset_of(address))

    def thread(self, thread_id: int) -> Thread | None:
        for candidate in self.threads:
            if candidate.thread_id == thread_id:
                return candidate
        return None

    def thread_census(self) -> dict[str, int]:
        """``{thread name: count}``, busiest first. Unnamed threads omitted.

        A count well above the core count for a dependency's runtime pool
        is worth noticing: it distinguishes "sized itself as designed" from
        "has been accumulating threads for hours".
        """
        census = Counter(t.name for t in self.threads if t.name)
        return dict(census.most_common())

    def stack_modules(self, thread_id: int) -> dict[str, int]:
        """Modules appearing as pointers on ``thread_id``'s saved stack.

        Counts, busiest first. See the module docstring on why this is a
        coarse signal: it is a scan for values that land inside a loaded
        module, not a stack walk, so treat it as "what this thread had
        touched" rather than a call chain.
        """
        thread = self.thread(thread_id)
        if thread is None or not thread.stack_size:
            return {}
        stack = self._blob[thread.stack_rva:thread.stack_rva + thread.stack_size]
        counts: Counter[str] = Counter()
        # -7, not -8: the last whole 8-byte slot starts at len-8 and must be
        # read, or every stack silently loses its outermost pointer.
        for off in range(0, max(0, len(stack) - 7), 8):
            (value,) = struct.unpack_from("<Q", stack, off)
            if value < 0x10000:
                continue
            module = self.module_for(value)
            if module is not None:
                counts[module.name] += 1
        return dict(counts.most_common())

    def summary(
        self,
        *,
        fault_address: int | None = None,
        fault_thread_id: int | None = None,
    ) -> dict[str, object]:
        """Triage view of the dump.

        ``fault_address`` / ``fault_thread_id`` are used only when the dump
        has no exception stream, which is the common case for dumps written
        from inside an exception filter. Pass the values from the crash
        record and the summary is as informative as if the stream existed.
        """
        address = fault_address
        thread_id = fault_thread_id
        source = "crash record"
        if self.exception is not None:
            address = self.exception.address
            thread_id = self.exception.thread_id
            source = "dump exception stream"

        out: dict[str, object] = {
            "modules": len(self.modules),
            "threads": len(self.threads),
            "processors": self.processors,
            "fault_source": source,
            "has_exception_stream": self.exception is not None,
        }
        if self.exception is not None:
            out["exception_code"] = "0x%08X" % (self.exception.code & 0xFFFFFFFF)
        if address:
            out["fault_address"] = "0x%016X" % int(address)
            out["fault_module"] = self.describe(int(address))
        if thread_id:
            thread = self.thread(int(thread_id))
            out["fault_thread_id"] = int(thread_id)
            out["fault_thread_name"] = (thread.name if thread else "") or "(unnamed)"
            stack = self.stack_modules(int(thread_id))
            out["fault_stack_modules"] = {
                name: n for name, n in stack.items()
                if name.lower() not in _SYSTEM_MODULES
            }
            out["fault_stack_handler_frames"] = {
                name: n for name, n in stack.items() if name in HANDLER_MODULES
            }
        census = self.thread_census()
        out["thread_census"] = census
        if thread_id:
            name = out.get("fault_thread_name")
            if isinstance(name, str) and name in census:
                out["fault_thread_peers"] = census[name]
        return out


def summarize(
    path: str | Path,
    *,
    fault_address: int | None = None,
    fault_thread_id: int | None = None,
) -> dict[str, object]:
    """Load ``path`` and return :meth:`Minidump.summary`.

    Returns ``{"error": ...}`` rather than raising: the callers are a
    debug MCP tool and a log line, and neither should fail because a dump
    was truncated by a process that died mid-write.
    """
    try:
        dump = Minidump.load(path)
    except MinidumpError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        summary = dump.summary(
            fault_address=fault_address, fault_thread_id=fault_thread_id
        )
    except Exception as exc:
        return {"error": "parse failed: %s: %s" % (type(exc).__name__, exc)}
    summary["path"] = str(path)
    return summary


__all__ = [
    "Exception_",
    "HANDLER_MODULES",
    "Minidump",
    "MinidumpError",
    "Module",
    "Thread",
    "summarize",
]
