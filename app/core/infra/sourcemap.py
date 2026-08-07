"""Resolve minified browser stack frames back to real source locations.

A production build is minified, so an unaided UI crash report reads::

    TypeError: Cannot read properties of undefined
        at Ln (http://localhost:6275/assets/index-D4x9k2.js:48:1203)

which names neither the file nor the function that actually broke. The
Vite build emits ``.map`` files next to each bundle (``build.sourcemap``
in ``web/vite.config.ts``), and this module reads them off disk to
rewrite those frames into::

    TypeError: Cannot read properties of undefined
        at Ln (src/live2d/channels/ExpressionChannel.ts:520:14)

Doing it **server-side** is the point. The crashes worth catching happen
on a phone, where nobody has DevTools attached to do the mapping
interactively — this way the readable stack is already in
``data/crashlog.txt`` by the time anyone looks.

Design notes
------------
* **Best-effort, always.** Every entry point swallows its own errors and
  falls back to returning the stack unchanged. A crash reporter that
  crashes is worse than no crash reporter.
* **Correctness is guaranteed by Vite's content hashing**, not by us.
  ``index-D4x9k2.js`` only ever pairs with ``index-D4x9k2.js.map``, so a
  stale ``dist/`` simply fails to resolve rather than silently producing
  a plausible-but-wrong location. There is no version check to get wrong.
* **Parsed maps are cached** keyed by path + mtime + size, because
  decoding a multi-megabyte ``mappings`` string on every crash report
  would be silly. A rebuild changes mtime and invalidates the entry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable
import bisect
import json
import re
import threading

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ASSETS_DIR = PROJECT_ROOT / "web" / "dist" / "assets"

# Refuse absurd map files rather than reading them into memory. Real maps
# for this bundle are a few MB; anything past this is not ours.
MAX_MAP_BYTES = 64 * 1024 * 1024
# Bound on how many frames we rewrite in one stack, so a pathological
# report can't turn into a long lookup loop.
MAX_FRAMES = 80

_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_INDEX = {ch: i for i, ch in enumerate(_B64_ALPHABET)}

# ``…/assets/index-D4x9k2.js:48:1203`` — the location tail of a stack
# frame. Deliberately matches the *location* rather than the whole frame
# so it works for all three formats without parsing each one:
# Chrome's ``at fn (URL:l:c)``, Firefox/Safari's ``fn@URL:l:c``, and the
# bare ``at URL:l:c`` that anonymous frames produce.
_FRAME_LOCATION = re.compile(
    r"(?P<url>(?:[A-Za-z][A-Za-z0-9+.-]*://[^\s()]+?|/[^\s()]*?)"
    r"/(?P<file>[^/\s()?#]+\.[cm]?js))"
    r"(?:\?[^\s():]*)?"
    r":(?P<line>\d+):(?P<col>\d+)"
)


class SourceMap:
    """One parsed ``.map`` file, queryable by generated line/column."""

    __slots__ = ("sources", "names", "_rows")

    def __init__(self, sources: list[str], names: list[str], mappings: str) -> None:
        self.sources = sources
        self.names = names
        self._rows = _parse_mappings(mappings)

    def lookup(self, line: int, column: int) -> tuple[str, int, int, str] | None:
        """Map a **1-based** generated position to ``(source, line, col, name)``.

        Returns source line/column 1-based as well, so the result reads
        like a normal editor location. ``None`` when the position falls
        outside the map or lands on a segment with no source (generated
        code with no original, e.g. injected helpers).
        """
        row_index = line - 1
        if row_index < 0 or row_index >= len(self._rows):
            return None
        columns, segments = self._rows[row_index]
        if not columns:
            return None
        # The segment that *starts at or before* this column owns it.
        slot = bisect.bisect_right(columns, column - 1) - 1
        if slot < 0:
            return None
        _gen_col, src_idx, src_line, src_col, name_idx = segments[slot]
        if src_idx < 0 or src_idx >= len(self.sources):
            return None
        name = ""
        if 0 <= name_idx < len(self.names):
            name = self.names[name_idx]
        return self.sources[src_idx], src_line + 1, src_col + 1, name


def _decode_vlq(segment: str) -> list[int]:
    """Decode one base64-VLQ segment into its signed integer fields."""
    values: list[int] = []
    result = 0
    shift = 0
    for ch in segment:
        digit = _B64_INDEX.get(ch)
        if digit is None:
            raise ValueError(f"bad base64 char {ch!r}")
        result += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        negative = result & 1
        result >>= 1
        values.append(-result if negative else result)
        result = 0
        shift = 0
    return values


def _parse_mappings(
    mappings: str,
) -> list[tuple[list[int], list[tuple[int, int, int, int, int]]]]:
    """Decode the ``mappings`` string into one sorted row per generated line.

    Each row is ``(generated_columns, segments)`` — the columns are split
    out into their own list purely so :meth:`SourceMap.lookup` can hand
    them straight to :mod:`bisect`.

    The source index / line / column / name fields are deltas that carry
    **across** line boundaries; only the generated column resets on each
    ``;``. Getting that backwards yields a map that decodes without error
    and points at nonsense, which is why it is called out here.
    """
    rows: list[tuple[list[int], list[tuple[int, int, int, int, int]]]] = []
    src_idx = 0
    src_line = 0
    src_col = 0
    name_idx = 0

    for group in mappings.split(";"):
        columns: list[int] = []
        segments: list[tuple[int, int, int, int, int]] = []
        gen_col = 0
        if group:
            for raw in group.split(","):
                if not raw:
                    continue
                fields = _decode_vlq(raw)
                if not fields:
                    continue
                gen_col += fields[0]
                if len(fields) >= 4:
                    src_idx += fields[1]
                    src_line += fields[2]
                    src_col += fields[3]
                    if len(fields) >= 5:
                        name_idx += fields[4]
                        entry = (gen_col, src_idx, src_line, src_col, name_idx)
                    else:
                        entry = (gen_col, src_idx, src_line, src_col, -1)
                else:
                    # A one-field segment marks generated code with no
                    # original counterpart.
                    entry = (gen_col, -1, -1, -1, -1)
                columns.append(gen_col)
                segments.append(entry)
        # Segments are emitted in ascending generated-column order, but a
        # hand-rolled or concatenated map need not honour that, and an
        # unsorted list would break the bisect.
        if columns != sorted(columns):
            order = sorted(range(len(columns)), key=columns.__getitem__)
            columns = [columns[i] for i in order]
            segments = [segments[i] for i in order]
        rows.append((columns, segments))
    return rows


def _tidy_source(path: str, source_root: str = "") -> str:
    """Normalise a map's source path into something worth reading.

    Vite records sources relative to the bundle (``../../src/App.tsx``)
    and marks synthesised modules with ``\\0`` or a ``plugin:`` prefix.
    Trim the walk-up noise and anchor on the first meaningful segment so
    the result looks like a repo-relative path.
    """
    text = str(path or "").replace("\\", "/")
    if source_root:
        root = str(source_root).replace("\\", "/").rstrip("/")
        if root and not text.startswith(("/", "http://", "https://")):
            text = f"{root}/{text}"
    text = text.lstrip("\0")
    for marker in ("/@fs/", "/@id/"):
        if text.startswith(marker):
            text = text[len(marker) :]
    while text.startswith("../"):
        text = text[3:]
    if text.startswith("./"):
        text = text[2:]
    # Anchor on the frontend root when it's in there somewhere, so
    # ``.../web/src/App.tsx`` reads as ``web/src/App.tsx``.
    for anchor in ("/web/src/", "/src/", "/node_modules/"):
        idx = text.find(anchor)
        if idx > 0:
            return text[idx + 1 :]
    return text


class _MapCache:
    """Path → parsed map, invalidated by mtime + size."""

    def __init__(self, capacity: int = 12) -> None:
        self._entries: dict[Path, tuple[float, int, SourceMap | None]] = {}
        self._capacity = max(1, capacity)
        self._lock = threading.Lock()

    def get(self, path: Path) -> SourceMap | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > MAX_MAP_BYTES:
            return None
        key = (stat.st_mtime, stat.st_size)

        with self._lock:
            cached = self._entries.get(path)
            if cached is not None and (cached[0], cached[1]) == key:
                return cached[2]

        parsed = _load_map(path)

        with self._lock:
            if len(self._entries) >= self._capacity:
                # Cheap eviction: drop the oldest insertion. The working
                # set is one or two bundles, so this effectively never
                # fires.
                self._entries.pop(next(iter(self._entries)), None)
            self._entries[path] = (key[0], key[1], parsed)
        return parsed

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _load_map(path: Path) -> SourceMap | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    mappings = raw.get("mappings")
    if not isinstance(mappings, str) or not mappings:
        return None
    sources_raw = raw.get("sources")
    sources = [
        _tidy_source(str(s), str(raw.get("sourceRoot") or ""))
        for s in (sources_raw if isinstance(sources_raw, list) else [])
    ]
    names_raw = raw.get("names")
    names = [str(n) for n in (names_raw if isinstance(names_raw, list) else [])]
    try:
        return SourceMap(sources, names, mappings)
    except Exception:
        return None


_CACHE = _MapCache()


def clear_cache() -> None:
    """Drop every parsed map. Used by tests and after a rebuild."""
    _CACHE.clear()


def _map_path_for(asset: str, assets_dir: Path) -> Path | None:
    """Locate the ``.map`` for a bundle filename, refusing path escapes."""
    name = Path(str(asset)).name
    if not name or name in (".", ".."):
        return None
    candidate = assets_dir / f"{name}.map"
    try:
        resolved = candidate.resolve()
        root = assets_dir.resolve()
    except OSError:
        return None
    if root not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


def symbolicate_stack(
    stack: str,
    *,
    assets_dir: Path | None = None,
) -> str:
    """Rewrite every resolvable frame location in ``stack``.

    Frames whose map is missing, stale, or unparseable are left exactly
    as they were, so the result is never *worse* than the input. Returns
    the input unchanged (and never raises) on any failure.
    """
    if not stack or not isinstance(stack, str):
        return stack
    directory = assets_dir if assets_dir is not None else DEFAULT_ASSETS_DIR
    try:
        if not directory.is_dir():
            return stack
    except OSError:
        return stack

    remaining = MAX_FRAMES

    def replace(match: re.Match[str]) -> str:
        nonlocal remaining
        if remaining <= 0:
            return match.group(0)
        try:
            map_path = _map_path_for(match.group("file"), directory)
            if map_path is None:
                return match.group(0)
            source_map = _CACHE.get(map_path)
            if source_map is None:
                return match.group(0)
            hit = source_map.lookup(int(match.group("line")), int(match.group("col")))
            if hit is None:
                return match.group(0)
            remaining -= 1
            source, line, column, name = hit
            location = f"{source}:{line}:{column}"
            # The map's ``name`` is the pre-minification identifier. Keep
            # it only when it adds something the frame doesn't already
            # say, since Chrome frames carry their own (minified) name.
            return f"{location} ({name})" if name else location
        except Exception:
            return match.group(0)

    try:
        return _FRAME_LOCATION.sub(replace, stack)
    except Exception:
        return stack


def stack_is_symbolicated(original: str, mapped: str) -> bool:
    """Whether :func:`symbolicate_stack` actually changed anything."""
    return bool(mapped) and mapped != original


def available_bundles(assets_dir: Path | None = None) -> Iterable[str]:
    """Names of bundles that currently have a map on disk (diagnostics)."""
    directory = assets_dir if assets_dir is not None else DEFAULT_ASSETS_DIR
    try:
        return sorted(p.name[: -len(".map")] for p in directory.glob("*.js.map"))
    except OSError:
        return []
