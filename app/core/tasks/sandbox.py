"""Filesystem sandbox for task handlers.

Phase 1 of the brain-orchestration refactor ships two read-only task
handlers (``file_search`` and ``file_read``) that walk the user's
disk on Aiko's behalf. The doc spec calls out *exactly* what makes
filesystem access from an LLM-driven task safe:

* Every path resolves against a labelled :class:`FileTaskRoot` from
  ``agent.task_file_allowed_roots`` — nothing outside the configured
  roots ever opens.
* ``..`` segments, absolute paths that try to break out of a root,
  and symlinks pointing outside their root are all rejected at
  resolve time.
* Each root is validated at boot — non-existent / wrong-type /
  overlapping roots get a WARNING but stay in config (so a
  temporarily-unmounted external drive doesn't auto-disappear).
* Bare paths search across every active root; if a file matches in
  more than one, the handler emits ``TaskInputNeeded`` rather than
  guessing (see ``file_read``).

This module is pure: no I/O on import, no orchestrator references,
no global state. Everything is a function or a small frozen
dataclass. The handler-side code (``file_search.py``) and the
settings parser (``settings.py``) both depend on it. Designed so
the unit tests can hand it a temp-dir tree and exercise every
rejection branch in milliseconds.

The doc spec also reserves ``read_only`` as a per-root flag for
phase 2 write ops. Phase 1 ignores it (everything is read-only by
construction); the flag is plumbed through so a future write handler
can re-use this exact resolver.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


log = logging.getLogger("app.tasks.sandbox")


@dataclass(frozen=True, slots=True)
class FileTaskRoot:
    """One entry in ``agent.task_file_allowed_roots``.

    ``label`` is the human-readable id used in path prefixes
    (``"Documents:notes/q4.md"``) and in the JSON result rows
    so the LLM can see which root a hit came from. Stable + unique
    per-config; case-sensitive.

    ``path`` is the absolute on-disk directory. May be configured
    as a relative path (resolved against the app root at load
    time); by the time it lands on this dataclass it's always
    absolute.

    ``read_only`` is reserved for phase 2 — phase 1 never writes,
    so it's effectively ignored. Plumbed through here so a future
    write handler can re-use the same root list with mixed
    read/write entries.
    """

    label: str
    path: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class ValidatedRoot:
    """Output of :func:`validate_roots`.

    Wraps the source :class:`FileTaskRoot` with the boot-time
    validation verdict so MCP debug + WS surface can show *why* a
    configured root isn't being searched.

    ``active=False`` means the path failed validation (didn't
    exist, wasn't a directory, etc.). The handler skips inactive
    roots entirely; they still appear in ``list_file_roots()`` so
    the user can see what went wrong.

    ``warnings`` is a list of non-fatal observations (e.g.
    "overlaps with another root"). Multiple warnings can land on a
    single root without flipping it inactive.
    """

    root: FileTaskRoot
    active: bool
    abs_path: str
    reason: str = ""
    warnings: tuple[str, ...] = ()


# ── label normalisation ───────────────────────────────────────────────────

# Labels are user-typed identifiers in path prefixes
# (``"Documents:notes/q4.md"``). Keep them simple: letters / digits /
# underscores / hyphens / spaces, no path separators, no colons (the
# separator). Empty labels are rejected at validate time.
_LABEL_FORBIDDEN: frozenset[str] = frozenset(":/\\\n\r\t")


def is_valid_label(label: str) -> bool:
    """Cheap predicate. Used by the settings parser + validate_roots."""
    if not isinstance(label, str):
        return False
    s = label.strip()
    if not s:
        return False
    return not any(ch in _LABEL_FORBIDDEN for ch in s)


# ── root normalisation ────────────────────────────────────────────────────


def normalize_root(
    root: FileTaskRoot,
    *,
    app_root: str | os.PathLike[str] | None = None,
) -> FileTaskRoot:
    """Resolve ``root.path`` to an absolute, normalised path.

    Relative paths are resolved against ``app_root`` (the assistant's
    install / working directory). Absolute paths are kept verbatim.
    The returned dataclass is a fresh frozen instance so callers can
    treat the input as truly immutable.

    Does NOT touch the filesystem — that's :func:`validate_roots`'s
    job. This step is safe to call on every config reload.
    """
    raw = root.path or ""
    p = Path(raw)
    if not p.is_absolute():
        base = Path(app_root) if app_root is not None else Path.cwd()
        p = (base / p).resolve(strict=False)
    else:
        # ``resolve(strict=False)`` collapses ``..`` and normalises
        # casing without requiring the path to exist yet — perfect
        # for a config load that might predate the directory.
        p = p.resolve(strict=False)
    return FileTaskRoot(label=root.label, path=str(p), read_only=root.read_only)


# Heuristic system directories we never want to silently sandbox over.
# Not an outright reject — the user might have a legit reason to read
# from ``C:\Windows\Logs`` — but a WARNING gets logged so an
# accidental misconfiguration is visible.
_SENSITIVE_PREFIXES_POSIX: tuple[str, ...] = (
    "/etc",
    "/sys",
    "/proc",
    "/boot",
    "/dev",
    "/var/run",
    "/var/log",
)
_SENSITIVE_PREFIXES_WINDOWS: tuple[str, ...] = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\System Volume Information",
)


def _is_sensitive(abs_path: str) -> bool:
    """OS-aware sensitive-directory check. Used for WARNING only."""
    if os.name == "nt":
        lower = abs_path.lower()
        for prefix in _SENSITIVE_PREFIXES_WINDOWS:
            if lower.startswith(prefix.lower()):
                return True
        return False
    for prefix in _SENSITIVE_PREFIXES_POSIX:
        if abs_path == prefix or abs_path.startswith(prefix + os.sep):
            return True
    return False


def validate_roots(
    roots: Iterable[FileTaskRoot],
    *,
    app_root: str | os.PathLike[str] | None = None,
) -> list[ValidatedRoot]:
    """Validate every root, log WARNINGs, return per-root verdicts.

    Validation rules (mirrors the design doc):

    * Path does not exist → ``active=False`` reason=``missing``.
    * Path is a file, not a directory → ``active=False`` reason=``not_a_directory``.
    * Two configured roots resolve to the same absolute path →
      both kept; second one flagged ``warning="duplicate_path"``.
    * One root sits inside another → both kept; the inner one
      gets ``warning="nested_inside_<other_label>"``.
    * Label invalid (empty, contains ``:``, ``/``, ``\\``) →
      ``active=False`` reason=``invalid_label``.
    * Path resolves to a sensitive system directory →
      ``warning="sensitive_directory"`` but still active.

    Active state and reasons are stable strings — MCP debug tools
    and the doc cross-reference them.
    """
    output: list[ValidatedRoot] = []
    normalised: list[FileTaskRoot] = []
    seen_paths: dict[str, str] = {}  # path -> first label that claimed it
    for root in roots:
        n = normalize_root(root, app_root=app_root)
        normalised.append(n)
        warnings: list[str] = []
        reason = ""
        active = True
        if not is_valid_label(root.label):
            active = False
            reason = "invalid_label"
            log.warning(
                "file root rejected: label=%r reason=invalid_label",
                root.label,
            )
            output.append(
                ValidatedRoot(
                    root=root,
                    active=False,
                    abs_path=n.path,
                    reason=reason,
                    warnings=(),
                )
            )
            continue
        abs_path = n.path
        try:
            p = Path(abs_path)
            if not p.exists():
                active = False
                reason = "missing"
                log.warning(
                    "file root inactive: label=%s path=%s reason=missing",
                    n.label,
                    abs_path,
                )
            elif not p.is_dir():
                active = False
                reason = "not_a_directory"
                log.warning(
                    "file root inactive: label=%s path=%s reason=not_a_directory",
                    n.label,
                    abs_path,
                )
        except OSError as exc:
            active = False
            reason = "io_error"
            log.warning(
                "file root inactive: label=%s path=%s reason=io_error err=%s",
                n.label,
                abs_path,
                exc,
            )
        # Cross-root overlaps — only meaningful for paths that resolved.
        if abs_path in seen_paths and seen_paths[abs_path] != n.label:
            warnings.append("duplicate_path")
            log.warning(
                "file root warning: label=%s path=%s warning=duplicate_path "
                "first_claimed_by=%s",
                n.label,
                abs_path,
                seen_paths[abs_path],
            )
        else:
            seen_paths[abs_path] = n.label
        # Nested-root warning — does this root sit inside another
        # *previously-seen* root? (Only one direction is enough; the
        # outer root scanning will produce the same WARNING from its
        # own perspective if both directions matter.)
        for prior in normalised[:-1]:
            if prior.path == abs_path:
                continue
            try:
                if Path(abs_path).is_relative_to(prior.path):
                    warnings.append(f"nested_inside_{prior.label}")
                    log.warning(
                        "file root warning: label=%s path=%s "
                        "warning=nested_inside_%s",
                        n.label,
                        abs_path,
                        prior.label,
                    )
            except AttributeError:
                # ``Path.is_relative_to`` is 3.9+; we're on 3.11+ so
                # this branch is unreachable. Keeping for type-checker
                # robustness.
                pass
        if _is_sensitive(abs_path):
            warnings.append("sensitive_directory")
            log.warning(
                "file root warning: label=%s path=%s "
                "warning=sensitive_directory",
                n.label,
                abs_path,
            )
        output.append(
            ValidatedRoot(
                root=n,
                active=active,
                abs_path=abs_path,
                reason=reason,
                warnings=tuple(warnings),
            )
        )
    return output


# ── path resolution ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A path successfully resolved within one root.

    Used by every read operation as the *single* representation of
    "where on disk we're going to read from". Carries the label
    back to the caller so result rows can surface ``label`` +
    ``relative_path`` rather than the (potentially private)
    absolute path.
    """

    label: str
    relative_path: str
    abs_path: str
    root: FileTaskRoot


@dataclass(frozen=True, slots=True)
class PathResolutionError:
    """Structured rejection reason for :func:`resolve_path`.

    ``reason`` is a stable short identifier (``"unknown_label"`` /
    ``"no_match"`` / ``"multiple_matches"`` / ``"escape"`` /
    ``"empty_path"``) so callers can branch on it cheaply. The free-
    form ``message`` is what we surface in error events / cues.
    """

    reason: str
    message: str
    candidates: tuple[ResolvedPath, ...] = field(default_factory=tuple)


def _parse_prefix(path: str) -> tuple[str | None, str]:
    """Split a user-shaped path into ``(label_or_None, tail)``.

    A leading ``"<label>:<tail>"`` is treated as a label-scoped path
    iff the label is a valid label (no path separators). Any colon
    deeper into the path is left untouched — Windows drive letters
    (``"C:\\foo"``) would otherwise be misread as label prefixes,
    so we require the prefix label to pass :func:`is_valid_label`.
    """
    if not path:
        return None, ""
    if ":" not in path:
        return None, path
    head, rest = path.split(":", 1)
    if is_valid_label(head):
        return head.strip(), rest
    return None, path


def _resolve_in_root(
    root: FileTaskRoot,
    relative: str,
) -> ResolvedPath | None:
    """Resolve ``relative`` against ``root.path``; reject escapes.

    Returns ``None`` if the resolved absolute path exits the root
    (via ``..`` or a symlink that points outside). Returns ``None``
    if the relative path itself is empty after stripping. The
    caller is responsible for verifying existence — this function
    is purely syntactic.
    """
    rel = (relative or "").strip().lstrip("/\\")
    if not rel:
        return None
    try:
        candidate = (Path(root.path) / rel).resolve(strict=False)
    except (OSError, ValueError):
        return None
    try:
        rel_to_root = candidate.relative_to(Path(root.path).resolve(strict=False))
    except ValueError:
        # Escape attempt — the resolved path is outside the root.
        return None
    return ResolvedPath(
        label=root.label,
        relative_path=str(rel_to_root).replace(os.sep, "/"),
        abs_path=str(candidate),
        root=root,
    )


def resolve_path(
    path: str,
    *,
    active_roots: Iterable[ValidatedRoot],
    must_exist: bool = True,
) -> ResolvedPath | PathResolutionError:
    """Resolve a user-typed path against the configured roots.

    Two shapes:

    * **Label-prefixed** ``"Documents:notes/q4.md"`` — resolves only
      against the named root; rejects with ``"unknown_label"`` if
      the label doesn't match any active root.
    * **Bare** ``"notes/q4.md"`` — tries each active root in
      configuration order. Returns the unique hit if exactly one
      root contains the path. Multiple hits → ``"multiple_matches"``
      with the candidate list (the caller — typically a
      ``TaskInputNeeded`` emit — picks). Zero hits → ``"no_match"``.

    Escape attempts (``..``, absolute paths outside any root,
    symlinks pointing out of the root) are rejected with
    ``"escape"``. ``must_exist`` controls whether the resolved path
    has to exist on disk before being accepted (handlers that
    *create* paths pass ``False``; phase 1 handlers are all read-
    only so the default is ``True``).
    """
    if not path or not path.strip():
        return PathResolutionError(
            reason="empty_path", message="path is empty"
        )
    actives = [vr for vr in active_roots if vr.active]
    label, tail = _parse_prefix(path)
    if label is not None:
        for vr in actives:
            if vr.root.label == label:
                resolved = _resolve_in_root(vr.root, tail)
                if resolved is None:
                    return PathResolutionError(
                        reason="escape",
                        message=(
                            f"path {path!r} resolves outside root "
                            f"{label!r}"
                        ),
                    )
                if must_exist and not Path(resolved.abs_path).exists():
                    return PathResolutionError(
                        reason="no_match",
                        message=(
                            f"path {path!r} does not exist in root "
                            f"{label!r}"
                        ),
                    )
                return resolved
        return PathResolutionError(
            reason="unknown_label",
            message=f"no active root with label {label!r}",
        )
    # Bare path — try every active root in order.
    candidates: list[ResolvedPath] = []
    for vr in actives:
        resolved = _resolve_in_root(vr.root, tail)
        if resolved is None:
            continue
        if must_exist and not Path(resolved.abs_path).exists():
            continue
        candidates.append(resolved)
    if not candidates:
        return PathResolutionError(
            reason="no_match",
            message=f"path {path!r} did not match any configured root",
        )
    if len(candidates) > 1:
        return PathResolutionError(
            reason="multiple_matches",
            message=(
                f"path {path!r} matches {len(candidates)} roots — "
                "specify <label>:<path> or pick one of the candidates"
            ),
            candidates=tuple(candidates),
        )
    return candidates[0]


__all__ = [
    "FileTaskRoot",
    "ValidatedRoot",
    "ResolvedPath",
    "PathResolutionError",
    "is_valid_label",
    "normalize_root",
    "validate_roots",
    "resolve_path",
]
