"""Explicit source roots, conservative exclusions and bounded file snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from functools import lru_cache
from functools import cached_property
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Any


class SourceError(ValueError):
    """A safe, user-facing source access failure."""


EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
        ".rst",
        ".txt",
        ".css",
        ".scss",
        ".html",
        ".vue",
        ".svelte",
        ".rs",
        ".go",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".java",
        ".cs",
        ".rb",
        ".sh",
        ".ps1",
        ".sql",
    }
)
DENIED_PARTS = frozenset(
    {
        "data",
        "logs",
        "log",
        "attachments",
        "voices",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "coverage",
        "__pycache__",
        "venv",
        "env",
        "target",
        "credentials",
        "secrets",
    }
)
DENIED_NAMES = (
    "user.json",
    "*.env",
    ".env*",
    "*secret*",
    "*credential*",
    "*password*",
    "*token*.json",
    "*.pem",
    "*.key",
    "package-lock.json",
    "pnpm-lock.yaml",
)


def relative_parts(value: str, *, allow_dot: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SourceError("Provide a bounded repository-relative path.")
    value = value.replace("\\", "/")
    if value == "." and allow_dot:
        return ()
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or any(
        any(ord(c) < 32 or c in ':<>"|?*' for c in part)
        or part.endswith((" ", "."))
        or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0])
        for part in parts
    ):
        raise SourceError("Paths must be repository-relative without traversal or drive names.")
    return tuple(parts)


def matches(path: str, pattern: str) -> bool:
    """Glob segments, including recursive **, without walking through glob()."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
        raise SourceError("Provide a file glob of at most 200 characters.")
    pattern = pattern.replace("\\", "/")
    if pattern.startswith("/") or ":" in pattern or ".." in pattern.split("/"):
        raise SourceError("File patterns must stay inside the source scope.")
    pieces, wanted = path.split("/"), pattern.split("/")
    if len(wanted) > 32:
        raise SourceError("The file pattern is too deeply nested.")
    if "/" not in pattern:
        return fnmatch.fnmatchcase(pieces[-1], pattern)

    @lru_cache(maxsize=None)
    def visit(i, j):
        if i == len(wanted):
            return j == len(pieces)
        if wanted[i] == "**":
            return visit(i + 1, j) or (j < len(pieces) and visit(i, j + 1))
        return j < len(pieces) and fnmatch.fnmatchcase(pieces[j], wanted[i]) and visit(i + 1, j + 1)

    return visit(0, 0)


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


@dataclass(frozen=True)
class Source:
    path: str
    text: str
    sha256: str
    mtime_ns: int
    size: int

    @cached_property
    def lines(self) -> list[str]:
        return self.text.split("\n") if self.text else []

    def identity(self) -> dict:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "mtime_ns": self.mtime_ns,
            "source_kind": "checkout_file",
        }


@dataclass
class Scan:
    files: list[str]
    reasons: set[str]
    deadline: float


class SourceScope:
    def __init__(self, config: dict[str, Any]):
        root = config.get("root")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise SourceError("Configure an explicit absolute source root.")
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise SourceError("The configured source root must be a directory.")
        included = config.get("include_paths")
        if not isinstance(included, list) or not 1 <= len(included) <= 32:
            raise SourceError(
                "Configure 1 to 32 source include_paths; no implicit working directory."
            )
        self.includes = tuple(relative_parts(item, allow_dot=True) for item in included)
        excluded = config.get("exclude_patterns", [])
        if (
            not isinstance(excluded, list)
            or len(excluded) > 32
            or any(not isinstance(item, str) for item in excluded)
        ):
            raise SourceError("exclude_patterns must be a bounded string list.")
        for item in excluded:
            matches("validate.py", item)
        self.excluded = tuple(excluded)

        def bound(name, default, low, high):
            value = config.get(name, default)
            if type(value) is not int or not low <= value <= high:
                raise SourceError(f"{name} must be an integer between {low} and {high}.")
            return value

        self.max_file_bytes = bound("max_file_bytes", 524288, 1024, 1048576)
        self.max_scan_bytes = bound("max_scan_bytes", 8388608, 1024, 16777216)
        self.max_scan_files = bound("max_scan_files", 400, 1, 1000)
        self.max_walk_entries = bound("max_walk_entries", 12000, 1, 20000)
        self.max_query_seconds = bound("max_query_seconds", 3, 1, 10)

    def permitted(self, parts: tuple[str, ...], *, directory=False) -> bool:
        folded = tuple(part.lower() for part in parts)
        if any(part.startswith(".") or part in DENIED_PARTS for part in folded):
            return False
        if any(fnmatch.fnmatchcase(part, pat) for part in folded for pat in DENIED_NAMES):
            return False
        relative = "/".join(parts)
        if any(matches(relative.casefold(), pat.casefold()) for pat in self.excluded):
            return False
        if directory:
            return True
        return PurePosixPath(relative).suffix.lower() in EXTENSIONS or (
            parts and parts[-1] in {"Dockerfile", "Makefile", "LICENSE"}
        )

    def resolve(self, relative: str, *, directory=False) -> Path:
        parts = relative_parts(relative, allow_dot=directory)
        compare = tuple(os.path.normcase(part) for part in parts)
        if not any(
            compare[: len(inc)] == tuple(os.path.normcase(p) for p in inc) for inc in self.includes
        ):
            raise SourceError("This path is outside the configured source include_paths.")
        if not self.permitted(parts, directory=directory):
            raise SourceError("This path is excluded from source inspection.")
        candidate = self.root
        for part in parts:
            candidate /= part
            if _linked(candidate):
                raise SourceError("Source inspection does not follow symlinks or junctions.")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise SourceError("The resolved source path is outside the configured root.")
        actual = resolved.relative_to(self.root).parts
        if not self.permitted(actual, directory=directory):
            raise SourceError("The resolved path is excluded from source inspection.")
        info = resolved.stat()
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise SourceError("The requested source directory is not a directory.")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise SourceError("Only regular, unlinked source files can be inspected.")
        return resolved

    def read(
        self, path: str, expected_sha256: str | None = None, *, max_bytes: int | None = None
    ) -> Source:
        if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise SourceError("expected_sha256 must be the hash from an earlier source result.")
        resolved = self.resolve(path)
        cap = min(self.max_file_bytes, max_bytes) if max_bytes is not None else self.max_file_bytes
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(resolved, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
                raise SourceError("Only regular, unlinked source files can be inspected.")
            if before.st_size > cap:
                raise SourceError("This file exceeds the configured source file-size limit.")
            raw = stream.read(cap + 1)
            after = os.fstat(stream.fileno())
        # Check again after reading; this detects ordinary editor replacements
        # and link changes. This application policy is not an OS sandbox against
        # a hostile process racing filesystem mutations.
        final = self.resolve(path).stat()

        def identity(st):
            return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size

        if identity(before) != identity(after) or identity(after) != identity(final):
            raise SourceError("The source changed while reading; retry with a fresh snapshot.")
        if len(raw) > cap or b"\x00" in raw:
            raise SourceError("Only bounded text source files can be inspected.")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise SourceError("The source revision changed; inspect it again before citing lines.")
        try:
            text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeError as exc:
            raise SourceError("This source is not UTF-8 text.") from exc
        return Source(
            resolved.relative_to(self.root).as_posix(), text, digest, after.st_mtime_ns, len(raw)
        )

    def scan(self, pattern: str) -> Scan:
        matches("validate.py", pattern)
        result = Scan([], set(), time.monotonic() + self.max_query_seconds)
        pending = ["/".join(parts) or "." for parts in reversed(self.includes)]
        seen: set[str] = set()
        visited = 0
        while pending:
            if time.monotonic() >= result.deadline:
                result.reasons.add("time_limit")
                break
            relative = pending.pop()
            if relative in seen:
                continue
            seen.add(relative)
            try:
                path = self.root.joinpath(*relative_parts(relative, allow_dot=True))
                directory = path.is_dir()
                resolved = self.resolve(relative, directory=directory)
                if not directory:
                    if matches(relative, pattern):
                        result.files.append(relative)
                    if len(result.files) >= self.max_scan_files:
                        result.reasons.add("file_limit")
                        break
                    continue
                entries = []
                with os.scandir(resolved) as iterator:
                    for entry in iterator:
                        visited += 1
                        if visited > self.max_walk_entries:
                            result.reasons.add("entry_limit")
                            break
                        child = f"{relative}/{entry.name}" if relative != "." else entry.name
                        parts = tuple(child.split("/"))
                        if (
                            not self.permitted(parts, directory=True)
                            or entry.is_symlink()
                            or _linked(Path(entry.path))
                        ):
                            continue
                        entries.append(child)
                pending.extend(sorted(entries, reverse=True))
                if "entry_limit" in result.reasons:
                    break
            except OSError:
                result.reasons.add("unreadable_paths")
            except SourceError:
                # Excluded paths are intentionally absent from listings. A
                # denied explicit read raises instead of returning private names.
                continue
        result.files.sort()
        return result
