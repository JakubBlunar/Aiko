"""File-read task handler — first exerciser of ``TaskInputNeeded``.

Chunk 12 of the brain-orchestration refactor. The phase-1 doc spec
calls out this handler as the canonical demonstrator of the
``awaiting_input`` flow: a bare path that matches in more than one
configured root forces the handler to ask Aiko (and therefore the
user) which root they meant, rather than silently picking the first
match. The orchestrator persists ``status='awaiting_input'`` +
``input_request`` JSON; the brain loop parks a
:class:`TaskInputNeededEvent` cue; the next user turn surfaces the
question in the T6 prompt block; the user's free-text answer drives
:meth:`FileReadHandler.on_input` to resume.

Why ``file_read`` and not something flashier (markdown render, code
preview)?

* Reading a text file is the lowest-cognitive-load shape for the
  doc's "what does the awaiting-input loop actually look like" demo.
* It re-uses every safety primitive already in
  :mod:`app.core.tasks.sandbox` — no new escape-rejection logic, no
  new validation surface to audit.
* It surfaces the multi-root ambiguity case naturally because users
  pile files with the same name across roots (``notes/q4.md`` in
  Documents AND in Notes, for example).

Threading model is the same as :class:`FileSearchHandler`: the
handler runs synchronously on a worker thread; both ``start`` and
``on_input`` are bounded (read + decode + truncate, no network
calls, no recursion beyond a single resolve). Cancellation is the
no-op cleanup path — there's nothing long-lived to release.

Safety caps (read live off ``AgentSettings`` via constructor kwargs
so a settings hot-reload + handler re-register picks up new
values):

* ``max_bytes`` — hard byte cap on the read. Larger files are
  truncated mid-stream.
* ``max_lines`` — secondary line cap for the catastrophic case of
  a 256 KiB minified single-line blob.
* ``allowed_extensions`` — case-insensitive extension allow-list.
  Empty tuple means "rely on the magic-byte text check only".
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.tasks.handler_names import HANDLER_FILE_READ
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


log = logging.getLogger("app.tasks.file_read")


DEFAULT_MAX_BYTES = 262144
DEFAULT_MAX_LINES = 2000
# Used by ``_looks_binary`` as a probe. Read the first N bytes; if
# any NUL byte is present OR more than ``_BINARY_PRINTABLE_RATIO`` of
# them are non-printable (outside common text codepoints), treat as
# binary and refuse. Conservative but explicit.
_BINARY_PROBE_BYTES = 8192
_BINARY_PRINTABLE_RATIO = 0.30  # >=30% non-printable -> binary
# A small ASCII printable-ish set used by the binary heuristic. We
# don't decode the bytes to detect this; the per-byte set is enough
# because we only care about the gross "is this a text file?" question.
_TEXT_BYTES: frozenset[int] = frozenset(
    {0x09, 0x0A, 0x0D, 0x0C}  # tab, lf, cr, ff
    | set(range(0x20, 0x7F))  # printable ASCII
    | set(range(0x80, 0x100))  # high-bit, common in utf-8 / latin-1
)


# ── args + state ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ReadArgs:
    """Validated form of the ``args`` dict passed to ``start``."""

    path: str
    max_bytes: int


def _parse_args(args: dict[str, Any], default_max_bytes: int) -> _ReadArgs | str:
    """Validate the ``args`` dict, returning either a parsed args
    object or a short error string for ``TaskFailed.error``.
    """
    path = (args or {}).get("path", "") or ""
    if not isinstance(path, str):
        return "path must be a string"
    path = path.strip()
    if not path:
        return "path is empty"
    try:
        raw_max = int(
            (args or {}).get("max_bytes", default_max_bytes) or default_max_bytes
        )
    except (TypeError, ValueError):
        raw_max = default_max_bytes
    # Floor at 1 KiB; ceiling at the handler's configured cap. Lets
    # an LLM tool call shrink the request (e.g. "just the first 8 KB")
    # without ever letting it exceed the safety cap.
    max_bytes = max(1024, min(int(default_max_bytes), raw_max))
    return _ReadArgs(path=path, max_bytes=max_bytes)


# ── binary heuristic + extension check ──────────────────────────────────


def _looks_binary(sample: bytes) -> bool:
    """Heuristic: does this byte sample look like a binary file?

    Two signals (either fires):

    * Any NUL byte in the probe — binary, full stop. PNG / JPEG /
      PDF / sqlite / etc. all carry NULs in their headers.
    * More than ``_BINARY_PRINTABLE_RATIO`` of bytes outside the
      common text set — likely a binary blob that just happens not
      to start with a NUL.

    Returns False for empty input — a zero-byte file is text by
    convention (nothing to display, but ``cat /dev/null`` is fine).
    """
    if not sample:
        return False
    if 0x00 in sample:
        return True
    non_text = sum(1 for b in sample if b not in _TEXT_BYTES)
    return (non_text / len(sample)) >= _BINARY_PRINTABLE_RATIO


def _extension_allowed(
    abs_path: str, allowed_extensions: tuple[str, ...]
) -> bool:
    """True if ``abs_path``'s extension is in ``allowed_extensions``.

    Empty allow-list = allow everything (the magic-byte check is
    the only filter). Otherwise, case-insensitive suffix match
    against ``Path(abs_path).suffix``; files with no extension are
    rejected when the allow-list is non-empty.
    """
    if not allowed_extensions:
        return True
    suffix = Path(abs_path).suffix.lower()
    if not suffix:
        return False
    return suffix in allowed_extensions


# ── core read ────────────────────────────────────────────────────────────


def _read_file_safely(
    resolved: ResolvedPath,
    *,
    max_bytes: int,
    max_lines: int,
    allowed_extensions: tuple[str, ...],
) -> dict[str, Any] | str:
    """Read ``resolved`` with every safety check.

    Returns either a result dict suitable for :class:`TaskCompleted`
    or a short error string for :class:`TaskFailed`. The dict
    carries:

    * ``label`` + ``relative_path`` — the canonical identity pair.
    * ``content`` — decoded text, possibly truncated.
    * ``size_bytes`` — file size on disk (pre-truncation).
    * ``read_bytes`` — bytes actually read.
    * ``truncated`` — True if either cap fired.
    * ``encoding`` — ``utf-8`` (with errors=replace) for phase 1.
    * ``line_count`` — number of lines in the returned content
      (after any line-cap truncation).
    """
    abs_path = resolved.abs_path
    if not _extension_allowed(abs_path, allowed_extensions):
        return (
            f"file extension not allowed: {Path(abs_path).suffix or '(none)'}"
        )
    try:
        stat = os.stat(abs_path)
    except OSError as exc:
        return f"could not stat file: {exc}"
    if not os.path.isfile(abs_path):
        return "path is not a regular file"
    size_bytes = int(stat.st_size)
    # Read up to ``max_bytes + 1`` so we can flag truncation
    # accurately even when ``size_bytes`` doesn't match (sparse
    # files, special filesystems).
    read_cap = int(max_bytes) + 1
    try:
        with open(abs_path, "rb") as fh:
            raw = fh.read(read_cap)
    except OSError as exc:
        return f"could not read file: {exc}"
    if _looks_binary(raw[:_BINARY_PROBE_BYTES]):
        return "file looks binary (cannot read)"
    byte_truncated = len(raw) > int(max_bytes)
    if byte_truncated:
        raw = raw[: int(max_bytes)]
    # utf-8 with errors=replace so a stray latin-1 byte doesn't blow
    # the whole call up. Phase 2 can add an encoding-detection
    # arg / setting if needed.
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    line_truncated = False
    if len(lines) > int(max_lines):
        lines = lines[: int(max_lines)]
        line_truncated = True
    final_text = "\n".join(lines)
    if byte_truncated and not line_truncated:
        # When the byte cap fired mid-line we may have left a partial
        # last line; trim trailing whitespace so the LLM doesn't see
        # a weirdly-cut artifact.
        final_text = final_text.rstrip()
    return {
        "label": resolved.label,
        "relative_path": resolved.relative_path,
        "content": final_text,
        "size_bytes": size_bytes,
        "read_bytes": min(len(raw), int(max_bytes)),
        "truncated": bool(byte_truncated or line_truncated),
        "encoding": "utf-8",
        "line_count": len(lines),
    }


# ── candidate-list formatting for TaskInputNeeded ───────────────────────


def _format_candidates(
    candidates: tuple[ResolvedPath, ...],
) -> list[str]:
    """Render candidates as ``"<label>:<relative_path>"`` strings.

    These are the strings the TaskStrip "click-to-answer" path sends
    back as the user's answer; the chat path lets the user reply
    free-text (Aiko's persona block teaches her to repeat the label
    back so the response is parseable). The ordering matches
    ``resolve_path``'s candidate list (config order across roots).
    """
    return [f"{c.label}:{c.relative_path}" for c in candidates]


# ── handler class ───────────────────────────────────────────────────────


class FileReadHandler:
    """Phase-1 read-only file content fetch.

    Construction mirrors :class:`FileSearchHandler` so the
    orchestrator can register both from the same factory in
    :meth:`TaskOrchestrationMixin._register_builtin_task_handlers`.

    State persisted between ``start`` and ``on_input`` (the
    awaiting-input case):

    ``{"args": <original args>, "phase": "awaiting_disambiguation",
       "candidates": ["Label:relative/path", ...]}``

    ``on_input`` validates the user's answer against ``candidates``
    so a stale or malicious answer can't trick the handler into
    reading a file outside the originally-resolved set. The path
    re-resolves through :func:`resolve_path` for safety even though
    the candidate strings come from a previous resolve.
    """

    name: str = HANDLER_FILE_READ

    def __init__(
        self,
        *,
        roots: list[FileTaskRoot] | None = None,
        app_root: str | os.PathLike[str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_lines: int = DEFAULT_MAX_LINES,
        allowed_extensions: tuple[str, ...] = (),
    ) -> None:
        self._validated: list[ValidatedRoot] = validate_roots(
            roots or [], app_root=app_root
        )
        self._max_bytes = max(1024, int(max_bytes))
        self._max_lines = max(10, int(max_lines))
        # Normalise extensions here so ad-hoc constructor callers
        # don't have to. Settings-fed callers already pass a
        # normalised tuple (see :func:`_parse_extension_list`).
        self._allowed_extensions: tuple[str, ...] = tuple(
            (ext if ext.startswith(".") else "." + ext).lower()
            for ext in allowed_extensions
            if isinstance(ext, str) and ext.strip()
        )

    # ── lifecycle ────────────────────────────────────────────────────

    def start(
        self, args: dict[str, Any], emit: TaskEmitFn
    ) -> TaskState:
        parsed = _parse_args(args, default_max_bytes=self._max_bytes)
        if isinstance(parsed, str):
            emit(TaskFailed(error=parsed))
            return {"args": args, "phase": "rejected"}
        actives = [vr for vr in self._validated if vr.active]
        if not actives:
            emit(TaskFailed(error="no active file roots configured"))
            return {"args": args, "phase": "rejected"}
        resolved = resolve_path(parsed.path, active_roots=actives)
        if isinstance(resolved, PathResolutionError):
            if resolved.reason == "multiple_matches" and resolved.candidates:
                # Canonical ``TaskInputNeeded`` case — multiple roots
                # contain a bare path with the same relative shape.
                candidate_strings = _format_candidates(resolved.candidates)
                emit(
                    TaskInputNeeded(
                        prompt=(
                            f"The path {parsed.path!r} matches "
                            f"{len(resolved.candidates)} configured roots. "
                            "Which one did you mean? Reply with the "
                            "label-prefixed path (e.g. "
                            f"'{candidate_strings[0]}')."
                        ),
                        options=candidate_strings,
                    )
                )
                log.info(
                    "file_read: awaiting input (multi-root): path=%r "
                    "candidates=%d",
                    parsed.path,
                    len(candidate_strings),
                )
                return {
                    "args": args,
                    "phase": "awaiting_disambiguation",
                    "candidates": candidate_strings,
                }
            emit(
                TaskFailed(
                    error=(
                        f"could not resolve path: {resolved.message} "
                        f"({resolved.reason})"
                    )
                )
            )
            log.info(
                "file_read: failed resolve: path=%r reason=%s",
                parsed.path,
                resolved.reason,
            )
            return {"args": args, "phase": "rejected"}
        # Single-root match — read and complete.
        return self._complete_with_read(args, resolved, parsed.max_bytes, emit)

    def resume(
        self, state: TaskState, emit: TaskEmitFn
    ) -> TaskState:
        # No multi-step long-running state to recover. If a row
        # survives a restart in ``awaiting_input`` we keep the
        # candidate list; ``on_input`` is the only path that resolves
        # the question, so resume just no-ops back to the same state.
        # Tasks left in ``running`` at restart land here only after the
        # boot recovery demotes them to ``interrupted`` — emit a
        # graceful failure so the row reaches a terminal state instead
        # of getting stuck mid-air.
        emit(
            TaskFailed(
                error=(
                    "file_read does not support resume; restart the read"
                )
            )
        )
        return state

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        """Resolve the multi-root disambiguation with the user's answer.

        ``state["candidates"]`` is the canonical "valid answers" list
        — accept anything that matches (case-insensitive) one of
        those strings, OR a label that uniquely matches one. This
        keeps the chat path forgiving without losing the safety
        invariant that the read goes through ``resolve_path`` against
        the same active roots.
        """
        candidates = list(state.get("candidates") or [])
        args = dict(state.get("args") or {})
        if not candidates:
            emit(TaskFailed(error="no candidates remembered; restart the read"))
            return {"args": args, "phase": "rejected"}
        raw = (answer or "").strip()
        if not raw:
            emit(TaskFailed(error="answer is empty"))
            return state
        chosen_path = self._match_answer(raw, candidates)
        if chosen_path is None:
            # Hand it back as another awaiting-input with a tighter
            # prompt so Aiko can retry once without the user having to
            # spawn a fresh task. Cap to a single retry by stashing a
            # counter on state.
            retries = int(state.get("retries", 0)) + 1
            if retries >= 2:
                emit(
                    TaskFailed(
                        error=(
                            f"could not match answer {raw!r} to any of "
                            f"{len(candidates)} candidates"
                        )
                    )
                )
                return {**state, "retries": retries, "phase": "rejected"}
            emit(
                TaskInputNeeded(
                    prompt=(
                        f"I didn't recognise {raw!r}. The candidates were: "
                        + ", ".join(candidates)
                        + ". Reply with one of them exactly."
                    ),
                    options=candidates,
                )
            )
            return {**state, "retries": retries, "phase": "awaiting_disambiguation"}
        # Re-resolve via the sandbox so the read is still gated by the
        # live active-roots list. A label-prefixed string trivially
        # resolves to the named root.
        actives = [vr for vr in self._validated if vr.active]
        resolved = resolve_path(chosen_path, active_roots=actives)
        if isinstance(resolved, PathResolutionError):
            emit(
                TaskFailed(
                    error=(
                        f"resolved candidate failed re-validation: "
                        f"{resolved.message}"
                    )
                )
            )
            return {**state, "phase": "rejected"}
        # Pull max_bytes off the original args so the user-tuned cap
        # survives the awaiting-input hop.
        try:
            max_bytes = int(args.get("max_bytes", self._max_bytes) or self._max_bytes)
        except (TypeError, ValueError):
            max_bytes = self._max_bytes
        max_bytes = max(1024, min(self._max_bytes, max_bytes))
        log.info(
            "file_read: on_input resolved: chosen=%r label=%s rel=%s",
            chosen_path,
            resolved.label,
            resolved.relative_path,
        )
        return self._complete_with_read(args, resolved, max_bytes, emit)

    def cancel(self, state: TaskState) -> None:
        # Nothing to release — the start/on_input invocations are
        # synchronous reads that have already returned by the time the
        # orchestrator marks the row cancelled.
        return None

    # ── helpers ──────────────────────────────────────────────────────

    def _complete_with_read(
        self,
        args: dict[str, Any],
        resolved: ResolvedPath,
        max_bytes: int,
        emit: TaskEmitFn,
    ) -> TaskState:
        """Run the safety-checked read and emit Completed / Failed."""
        result = _read_file_safely(
            resolved,
            max_bytes=max_bytes,
            max_lines=self._max_lines,
            allowed_extensions=self._allowed_extensions,
        )
        if isinstance(result, str):
            emit(TaskFailed(error=result))
            log.info(
                "file_read: failed read: label=%s rel=%s reason=%s",
                resolved.label,
                resolved.relative_path,
                result,
            )
            return {
                "args": args,
                "phase": "rejected",
                "label": resolved.label,
                "relative_path": resolved.relative_path,
            }
        log.info(
            "file_read: completed: label=%s rel=%s bytes=%d truncated=%s",
            resolved.label,
            resolved.relative_path,
            int(result.get("read_bytes", 0)),
            bool(result.get("truncated", False)),
        )
        emit(TaskCompleted(result=result))
        return {
            "args": args,
            "phase": "done",
            "label": resolved.label,
            "relative_path": resolved.relative_path,
        }

    @staticmethod
    def _match_answer(
        answer: str, candidates: list[str]
    ) -> str | None:
        """Try to match ``answer`` against the candidate list.

        Match shapes, in order (first hit wins):

        1. Exact case-insensitive equality (``"Documents:notes/q4.md"``).
        2. Same relative path under exactly one root label (``"Documents"``
           or ``"documents"``).
        3. Bare relative path with no label, matching exactly one
           candidate (rare; mostly for tests).

        Returns the canonical candidate string on a hit, or ``None``
        on no match / ambiguous match.
        """
        if not answer:
            return None
        lower = answer.strip().lower()
        # 1) exact match
        for c in candidates:
            if c.lower() == lower:
                return c
        # 2) label-only ("Documents")
        if ":" not in lower:
            label_hits = [c for c in candidates if c.split(":", 1)[0].lower() == lower]
            if len(label_hits) == 1:
                return label_hits[0]
        # 3) bare path
        if ":" not in lower:
            path_hits = [
                c for c in candidates
                if (":" in c) and c.split(":", 1)[1].lower() == lower
            ]
            if len(path_hits) == 1:
                return path_hits[0]
        return None


__all__ = [
    "FileReadHandler",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
]
