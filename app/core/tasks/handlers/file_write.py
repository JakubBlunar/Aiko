"""File-write task handler — the first *destructive* capability.

Reachable only as a goal-workflow child (the ``write_file`` skill),
never as a fast brain tool. It creates / overwrites / appends /
find-replaces text files inside a **writable** root (a
:class:`FileTaskRoot` with ``read_only=false``) and reuses every
safety primitive from :mod:`app.core.tasks.sandbox` plus the reusable
approval layer (:mod:`app.core.tasks.approval`).

Three ops (the ``op`` arg):

* ``write`` — create a new file or overwrite an existing one with
  ``content``.
* ``append`` — append ``content`` to a file (creating it if missing).
* ``replace`` — replace every occurrence of ``find`` with ``replace``
  in an existing file.

Safety gates, all enforced before anything touches disk:

* **Writable root** — the resolved path must live in a root marked
  ``read_only=false``; read-only roots reject with a clear error.
* **Extension allow-list** — same shape as ``file_read``.
* **Byte cap** — the resulting file content must fit ``max_bytes``.
* **Atomic write** — content is written to a temp file in the same
  directory and ``os.replace``-d into place, so a crash mid-write
  never leaves a half-written file.

Approval flow (the reusable pattern): an action is *destructive* when
it overwrites / appends-to / edits an EXISTING file. Creating a brand
new file is non-destructive (still gated by root / extension / bytes).
When destructive AND the approval policy says ``ask``, ``start`` emits
the standard approval :class:`TaskInputNeeded` and parks in
``awaiting_approval``; ``on_input`` reads the decision and either
performs the write (``approve`` / ``approve all``) or finishes without
acting (``deny``). ``approve all`` also flips the session approve-all
flag via the injected callback so the rest of the session stops
asking.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core.tasks.approval import (
    APPROVE_ALL,
    DENY,
    MODE_ASK,
    build_request,
    parse_decision,
)
from app.core.tasks.capabilities import (
    CAPABILITY_FILE_WRITE,
    get_capability,
)
from app.core.tasks.handler_names import HANDLER_FILE_WRITE
from app.core.tasks.sandbox import (
    FileTaskRoot,
    PathResolutionError,
    ResolvedPath,
    ValidatedRoot,
    resolve_path,
    validate_roots,
)
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskEmitFn,
    TaskFailed,
    TaskInputNeeded,
    TaskState,
)


log = logging.getLogger("app.tasks.file_write")


DEFAULT_MAX_BYTES = 262144

OP_WRITE = "write"
OP_APPEND = "append"
OP_REPLACE = "replace"
VALID_OPS = frozenset((OP_WRITE, OP_APPEND, OP_REPLACE))


# ── args ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _WriteArgs:
    path: str
    op: str
    content: str
    find: str
    replace: str


def _parse_args(args: dict[str, Any]) -> _WriteArgs | str:
    """Validate the ``args`` dict; return parsed args or an error string."""
    raw = args or {}
    path = raw.get("path", "")
    if not isinstance(path, str) or not path.strip():
        return "path is empty"
    op = str(raw.get("op", OP_WRITE) or OP_WRITE).strip().lower()
    if op not in VALID_OPS:
        return f"unknown op {op!r} (use write / append / replace)"
    content = raw.get("content", "")
    content = content if isinstance(content, str) else str(content)
    find = raw.get("find", "")
    find = find if isinstance(find, str) else str(find)
    replace = raw.get("replace", "")
    replace = replace if isinstance(replace, str) else str(replace)
    if op == OP_REPLACE and not find:
        return "replace op requires a non-empty 'find' string"
    return _WriteArgs(
        path=path.strip(), op=op, content=content, find=find, replace=replace
    )


# ── resolution verdict ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Resolved:
    resolved: ResolvedPath
    exists: bool
    destructive: bool


# ── handler ──────────────────────────────────────────────────────────


class FileWriteHandler:
    """Create / overwrite / append / find-replace a text file (gated).

    Construction mirrors :class:`FileReadHandler` plus two approval
    hooks injected by the host:

    * ``resolve_approval`` — ``(capability_id) -> "auto" | "ask"``. The
      handler asks for sign-off only when this returns ``"ask"`` for a
      destructive action. Default: always ``"ask"`` (fail safe).
    * ``mark_session_approved`` — ``(capability_id) -> None``, called on
      an ``approve all`` decision so the session stops asking. Default:
      no-op.
    """

    name: str = HANDLER_FILE_WRITE

    def __init__(
        self,
        *,
        roots: list[FileTaskRoot] | None = None,
        app_root: str | os.PathLike[str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allowed_extensions: tuple[str, ...] = (),
        resolve_approval: Callable[[str], str] | None = None,
        mark_session_approved: Callable[[str], None] | None = None,
    ) -> None:
        self._validated: list[ValidatedRoot] = validate_roots(
            roots or [], app_root=app_root
        )
        self._max_bytes = max(1, int(max_bytes))
        self._allowed_extensions: tuple[str, ...] = tuple(
            (ext if ext.startswith(".") else "." + ext).lower()
            for ext in allowed_extensions
            if isinstance(ext, str) and ext.strip()
        )
        self._resolve_approval = resolve_approval or (lambda _cap: MODE_ASK)
        self._mark_session_approved = mark_session_approved or (
            lambda _cap: None
        )

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, args: dict[str, Any], emit: TaskEmitFn) -> TaskState:
        parsed = _parse_args(args)
        if isinstance(parsed, str):
            emit(TaskFailed(error=parsed))
            return {"args": args, "phase": "rejected"}
        resolved = self._resolve(parsed)
        if isinstance(resolved, str):
            emit(TaskFailed(error=resolved))
            return {"args": args, "phase": "rejected"}
        # Destructive + policy says ask -> gate before touching disk.
        if resolved.destructive and self._resolve_approval(
            CAPABILITY_FILE_WRITE
        ) == "ask":
            cap = get_capability(CAPABILITY_FILE_WRITE)
            summary = _action_summary(parsed, resolved)
            if cap is not None:
                emit(build_request(cap, summary))
            else:  # pragma: no cover - capability always registered
                emit(
                    TaskInputNeeded(
                        prompt=f"Approve this file write: {summary}?",
                        options=["approve", "approve all", "deny"],
                    )
                )
            log.info(
                "file_write: awaiting approval: op=%s label=%s rel=%s",
                parsed.op,
                resolved.resolved.label,
                resolved.resolved.relative_path,
            )
            return {"args": args, "phase": "awaiting_approval"}
        return self._perform_and_complete(parsed, resolved, emit)

    def resume(self, state: TaskState, emit: TaskEmitFn) -> TaskState:
        # No long-running state to recover. A row surviving a restart in
        # ``awaiting_input`` keeps its args and waits for ``on_input``;
        # a ``running`` row demoted to ``interrupted`` lands here and is
        # failed gracefully (never silently re-run a destructive write).
        emit(
            TaskFailed(
                error="file_write does not support resume; start it again"
            )
        )
        return state

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        args = dict(state.get("args") or {})
        decision = parse_decision(answer)
        if decision == DENY:
            log.info("file_write: write declined by user")
            emit(
                TaskCompleted(
                    result={
                        "written": False,
                        "declined": True,
                        "summary": "Skipped the write — you didn't approve it.",
                    }
                )
            )
            return {"args": args, "phase": "declined"}
        if decision == APPROVE_ALL:
            try:
                self._mark_session_approved(CAPABILITY_FILE_WRITE)
            except Exception:
                log.debug("mark_session_approved failed", exc_info=True)
            log.info("file_write: approve-all set for this session")
        parsed = _parse_args(args)
        if isinstance(parsed, str):
            emit(TaskFailed(error=parsed))
            return {"args": args, "phase": "rejected"}
        resolved = self._resolve(parsed)
        if isinstance(resolved, str):
            emit(TaskFailed(error=resolved))
            return {"args": args, "phase": "rejected"}
        return self._perform_and_complete(parsed, resolved, emit)

    def cancel(self, state: TaskState) -> None:
        # The write itself is a single synchronous op that has already
        # returned by cancel time; nothing to release.
        return None

    # ── helpers ──────────────────────────────────────────────────────

    def _writable_actives(self) -> list[ValidatedRoot]:
        """Active roots that allow writes (``read_only=False``)."""
        return [
            vr
            for vr in self._validated
            if vr.active and not vr.root.read_only
        ]

    def _resolve(self, parsed: _WriteArgs) -> _Resolved | str:
        """Resolve + validate the target path; classify destructiveness.

        Returns a :class:`_Resolved` or a short error string. Only
        writable roots are considered, so a path that only exists under
        a read-only root reports "no writable root" rather than silently
        resolving somewhere unwritable.
        """
        writable = self._writable_actives()
        if not writable:
            return (
                "no writable file roots configured (a root must have "
                "read_only=false)"
            )
        resolved = resolve_path(
            parsed.path, active_roots=writable, must_exist=False
        )
        if isinstance(resolved, PathResolutionError):
            if resolved.reason == "multiple_matches":
                return (
                    f"path {parsed.path!r} is ambiguous across writable "
                    "roots — prefix it with a root label, e.g. "
                    "'Notes:todo.md'"
                )
            if resolved.reason == "unknown_label":
                return (
                    f"{resolved.message} (the label may be read-only or "
                    "not configured)"
                )
            return f"could not resolve path: {resolved.message}"
        if not _extension_allowed(
            resolved.abs_path, self._allowed_extensions
        ):
            return (
                "file extension not allowed: "
                f"{Path(resolved.abs_path).suffix or '(none)'}"
            )
        exists = os.path.isfile(resolved.abs_path)
        if parsed.op == OP_REPLACE and not exists:
            return f"cannot replace in a file that does not exist: {parsed.path!r}"
        # Destructive = modifies an existing file. A brand-new file is
        # non-destructive (still byte/ext/root gated, just no approval).
        destructive = exists
        return _Resolved(
            resolved=resolved, exists=exists, destructive=destructive
        )

    def _perform_and_complete(
        self, parsed: _WriteArgs, resolved: _Resolved, emit: TaskEmitFn
    ) -> TaskState:
        """Run the write op + emit Completed / Failed."""
        result = self._do_write(parsed, resolved)
        if isinstance(result, str):
            emit(TaskFailed(error=result))
            log.info(
                "file_write: failed: op=%s label=%s rel=%s reason=%s",
                parsed.op,
                resolved.resolved.label,
                resolved.resolved.relative_path,
                result,
            )
            return {"args": _args_dict(parsed), "phase": "rejected"}
        log.info(
            "file_write: completed: op=%s label=%s rel=%s bytes=%d created=%s",
            parsed.op,
            resolved.resolved.label,
            resolved.resolved.relative_path,
            int(result.get("bytes_written", 0)),
            not resolved.exists,
        )
        emit(TaskCompleted(result=result))
        return {
            "args": _args_dict(parsed),
            "phase": "done",
            "label": resolved.resolved.label,
            "relative_path": resolved.resolved.relative_path,
        }

    def _do_write(
        self, parsed: _WriteArgs, resolved: _Resolved
    ) -> dict[str, Any] | str:
        """Compute the final content + write it atomically.

        Returns a result dict or a short error string.
        """
        abs_path = resolved.resolved.abs_path
        if parsed.op == OP_WRITE:
            new_text = parsed.content
        elif parsed.op == OP_APPEND:
            existing = ""
            if resolved.exists:
                read = _read_text(abs_path)
                if isinstance(read, str) and read.startswith("\x00ERR:"):
                    return read[5:]
                existing = read  # type: ignore[assignment]
            new_text = existing + parsed.content
        else:  # OP_REPLACE
            read = _read_text(abs_path)
            if isinstance(read, str) and read.startswith("\x00ERR:"):
                return read[5:]
            existing = read  # type: ignore[assignment]
            if parsed.find not in existing:
                return f"'find' text not found in {resolved.resolved.relative_path}"
            new_text = existing.replace(parsed.find, parsed.replace)
        encoded = new_text.encode("utf-8")
        if len(encoded) > self._max_bytes:
            return (
                f"resulting file is too large: {len(encoded)} bytes "
                f"(max {self._max_bytes})"
            )
        err = _atomic_write(abs_path, encoded)
        if err is not None:
            return err
        return {
            "written": True,
            "op": parsed.op,
            "label": resolved.resolved.label,
            "relative_path": resolved.resolved.relative_path,
            "created": not resolved.exists,
            "bytes_written": len(encoded),
            "summary": _result_summary(parsed, resolved, len(encoded)),
        }


# ── module helpers ───────────────────────────────────────────────────


def _args_dict(parsed: _WriteArgs) -> dict[str, Any]:
    return {
        "path": parsed.path,
        "op": parsed.op,
        "content": parsed.content,
        "find": parsed.find,
        "replace": parsed.replace,
    }


def _extension_allowed(
    abs_path: str, allowed_extensions: tuple[str, ...]
) -> bool:
    if not allowed_extensions:
        return True
    suffix = Path(abs_path).suffix.lower()
    if not suffix:
        return False
    return suffix in allowed_extensions


def _read_text(abs_path: str) -> str:
    """Read a file as utf-8 text. Returns content or a ``\\x00ERR:`` string."""
    try:
        with open(abs_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return f"\x00ERR:could not read existing file: {exc}"
    return raw.decode("utf-8", errors="replace")


def _atomic_write(abs_path: str, data: bytes) -> str | None:
    """Write ``data`` to ``abs_path`` atomically. Returns error or None."""
    directory = os.path.dirname(abs_path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return f"could not create parent directory: {exc}"
    tmp_fd = None
    tmp_path = ""
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".aiko_write_", suffix=".tmp"
        )
        with os.fdopen(tmp_fd, "wb") as fh:
            tmp_fd = None  # fdopen took ownership
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, abs_path)
        return None
    except OSError as exc:
        # Clean up the temp file on failure.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return f"could not write file: {exc}"


def _action_summary(parsed: _WriteArgs, resolved: _Resolved) -> str:
    """Human phrase for the approval prompt."""
    target = f"{resolved.resolved.label}:{resolved.resolved.relative_path}"
    if parsed.op == OP_WRITE:
        return f"overwrite {target}"
    if parsed.op == OP_APPEND:
        return f"append to {target}"
    return f"edit (find/replace in) {target}"


def _result_summary(
    parsed: _WriteArgs, resolved: _Resolved, byte_count: int
) -> str:
    """Short summary folded into the task cue / workflow observation."""
    target = f"{resolved.resolved.label}:{resolved.resolved.relative_path}"
    if not resolved.exists:
        return f"created {target} ({byte_count} bytes)"
    if parsed.op == OP_APPEND:
        return f"appended to {target} ({byte_count} bytes total)"
    if parsed.op == OP_REPLACE:
        return f"edited {target} ({byte_count} bytes)"
    return f"overwrote {target} ({byte_count} bytes)"


__all__ = [
    "FileWriteHandler",
    "DEFAULT_MAX_BYTES",
    "OP_WRITE",
    "OP_APPEND",
    "OP_REPLACE",
]
