"""Inventory of the native threading / math runtimes loaded in-process.

Two copies of one OpenMP runtime -- or two different OpenMP runtimes --
inside a single Windows process is undefined behaviour: each copy keeps
its own global state and thread pool, and both Intel and PyTorch
document the result as crashes, hangs, or silently wrong numbers. The
failure mode is a native access violation at an arbitrary allocation
site after hours of uptime, which looks exactly like failing hardware
and is invisible to CPU/RAM stress tests.

We import torch (via RealtimeSTT) and pyarrow/lancedb in the same
process, so the risk is real and worth stating in the log at boot:
when a fatal native fault does land, the first question is always "was
this process in a supported configuration at all?".

Classification is deliberately name-based against a curated list rather
than a substring scan. Extension modules like ``_decomp_lu_cython.pyd``
and ``_compute.pyd`` contain "omp" and produce a convincing pile of
false positives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sys


# Real OpenMP runtimes. ``libiompstubs`` is deliberately absent: it is a
# stub library exporting the Intel API without a second thread pool, so
# it does not create the duplicate-runtime hazard.
_OPENMP_EXACT = frozenset(
    {
        "libiomp5md.dll",
        "libiomp5.dll",
        "libomp.dll",
        "omp.dll",
        "iomp5md.dll",
    }
)
_OPENMP_PREFIXES = ("vcomp", "libgomp")

# BLAS/LAPACK implementations. Several of these coexisting is normal and
# safe (numpy and scipy vendor their own OpenBLAS on purpose), so these
# are reported for context but never flagged as a hazard.
_BLAS_PREFIXES = (
    "libopenblas",
    "libscipy_openblas",
    "openblas",
    "mkl_",
    "libblas",
    "liblapack",
    "libflexiblas",
    "blis",
)


def _basename(path: str) -> str:
    return path.replace("/", "\\").rsplit("\\", 1)[-1].lower()


def _directory(path: str) -> str:
    normalised = path.replace("/", "\\")
    return normalised.rsplit("\\", 1)[0].lower() if "\\" in normalised else ""


def is_openmp_runtime(path: str) -> bool:
    """True when ``path`` is a real OpenMP runtime (not a stub, not a
    Python extension module that merely contains "omp" in its name)."""
    name = _basename(path)
    if not name.endswith(".dll"):
        return False
    if name in _OPENMP_EXACT:
        return True
    if name.startswith("libiompstubs"):
        return False
    return name.startswith(_OPENMP_PREFIXES)


def is_blas_runtime(path: str) -> bool:
    name = _basename(path)
    if not name.endswith(".dll"):
        return False
    return name.startswith(_BLAS_PREFIXES)


@dataclass(frozen=True)
class RuntimeInventory:
    """What is loaded, and whether the combination is supported.

    ``duplicates`` maps a runtime filename to the two-or-more directories
    it was loaded from -- the genuinely dangerous case, because Windows
    keys loaded modules by path and gives each copy its own state.
    """

    openmp: tuple[str, ...] = ()
    blas: tuple[str, ...] = ()
    duplicates: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def distinct_openmp(self) -> tuple[str, ...]:
        return tuple(sorted({_basename(p) for p in self.openmp}))

    @property
    def hazardous(self) -> bool:
        """True when the process holds more than one OpenMP runtime, by
        either measure: two copies of one runtime, or two different ones."""
        return bool(self.duplicates) or len(self.distinct_openmp) > 1

    def describe(self) -> str:
        """One-line summary for the boot log."""
        omp = self.distinct_openmp
        if not omp:
            return "native runtimes: no OpenMP runtime loaded"
        parts = ["native runtimes: openmp=%s" % ",".join(omp)]
        if self.blas:
            parts.append("blas=%s" % ",".join(sorted({_basename(p) for p in self.blas})))
        if self.duplicates:
            dupes = "; ".join(
                "%s loaded from %d paths" % (name, len(dirs))
                for name, dirs in sorted(self.duplicates.items())
            )
            parts.append("DUPLICATE: %s" % dupes)
        return " ".join(parts)


def classify(paths: list[str]) -> RuntimeInventory:
    """Split loaded module paths into the runtimes we care about.

    Pure: takes the module list rather than reading the live process, so
    the hazard logic is testable without a torch import.
    """
    openmp = tuple(p for p in paths if is_openmp_runtime(p))
    blas = tuple(p for p in paths if is_blas_runtime(p))
    by_name: dict[str, set[str]] = {}
    for path in openmp:
        by_name.setdefault(_basename(path), set()).add(_directory(path))
    duplicates = {
        name: tuple(sorted(dirs)) for name, dirs in by_name.items() if len(dirs) > 1
    }
    return RuntimeInventory(openmp=openmp, blas=blas, duplicates=duplicates)


def loaded_modules() -> list[str]:
    """Every module mapped into this process. ``[]`` off Windows."""
    if sys.platform != "win32":
        return []
    import ctypes
    import ctypes.wintypes as wt

    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        psapi.EnumProcessModulesEx.argtypes = [
            wt.HANDLE,
            ctypes.POINTER(wt.HMODULE),
            wt.DWORD,
            wt.LPDWORD,
            wt.DWORD,
        ]
        psapi.EnumProcessModulesEx.restype = wt.BOOL
        psapi.GetModuleFileNameExW.argtypes = [
            wt.HANDLE,
            wt.HMODULE,
            wt.LPWSTR,
            wt.DWORD,
        ]
        psapi.GetModuleFileNameExW.restype = wt.DWORD
    except Exception:
        return []

    handle = kernel32.GetCurrentProcess()
    slots = 8192
    array = (wt.HMODULE * slots)()
    needed = wt.DWORD()
    if not psapi.EnumProcessModulesEx(
        handle, array, ctypes.sizeof(array), ctypes.byref(needed), 0x03
    ):
        return []
    count = min(slots, needed.value // ctypes.sizeof(wt.HMODULE))
    buffer = ctypes.create_unicode_buffer(2048)
    out: list[str] = []
    for index in range(count):
        if psapi.GetModuleFileNameExW(handle, array[index], buffer, 2048):
            out.append(buffer.value)
    return out


def inventory() -> RuntimeInventory:
    """Classify the live process's loaded modules."""
    return classify(loaded_modules())
