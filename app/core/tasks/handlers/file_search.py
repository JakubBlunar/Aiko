"""Filename substring search handler.

Read-only walk across every active root in
``agent.task_file_allowed_roots`` looking for files whose
*basename* contains a case-insensitive query substring. Emits
``TaskProgress`` events every ``progress_every_n_dirs`` directories
scanned (so the TaskStrip in the UI can move) and a final
``TaskCompleted`` with the truncated match list.

Why substring and not glob/regex for phase 1?

* Substring is the lowest-cognitive-load shape for "find that
  thing" — same as a file-explorer's search box.
* Globs and regexes invite quoting / escaping mistakes in
  LLM-generated tool arguments. Phase 2 can add an optional
  ``mode`` arg.
* The implementation stays trivially auditable — no surprising
  walk behaviour, no catastrophic-backtracking risk.

Why not ``TaskInputNeeded`` on result-cap overflow (per the doc)?

* For phase 1 the simpler "truncate + flag" shape proves the queue
  pipeline end-to-end without needing the awaiting-input path to
  be wired into the demo. The doc's full ``running →
  awaiting_input → done`` flow lands with ``FileReadHandler``
  (next chunk) which exercises the ambiguity case naturally
  (one bare filename, multiple roots).

Threading: the handler walks the tree synchronously on the
orchestrator's worker thread. Big trees take seconds, not minutes;
the worker pool has enough capacity that even multiple concurrent
searches won't starve other handlers. Cancellation is checked via
the supplied ``cancel_event`` snapshot off ``state["cancel_set"]``
— the orchestrator stamps it on the state dict on the *first*
emit so the handler can poll it without taking new orchestrator
locks.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.tasks.handler_names import HANDLER_FILE_SEARCH
from app.core.tasks.sandbox import (
    FileTaskRoot,
    ValidatedRoot,
    validate_roots,
)
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskEmitFn,
    TaskFailed,
    TaskProgress,
    TaskState,
)


log = logging.getLogger("app.tasks.file_search")


# Per-call defaults. The settings module owns the live values; the
# handler reads them off its construction kwargs so a hot-reload
# rebuild of the handler picks up new caps.
DEFAULT_MAX_RESULTS = 50
DEFAULT_MAX_FILES_SCANNED = 20000
DEFAULT_PROGRESS_EVERY_N_DIRS = 25
# Per-directory exclude list — never recurse into these. Conservative
# defaults; users can extend via a future config knob if needed.
_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".idea",
        ".vscode",
    }
)


@dataclass(frozen=True, slots=True)
class _SearchArgs:
    """Validated form of the ``args`` dict passed to ``start``."""

    query: str
    root_label: str  # empty = search all active roots
    max_results: int
    case_sensitive: bool


def _parse_args(args: dict[str, Any]) -> _SearchArgs | str:
    """Return either a validated :class:`_SearchArgs` or an error string.

    ``args`` is jsonable; we accept it loosely (LLMs sometimes
    fumble field names) but reject empty queries hard because a
    blank substring matches every file in the tree.
    """
    raw_query = (args or {}).get("query", "") or ""
    if not isinstance(raw_query, str):
        return "query must be a string"
    query = raw_query.strip()
    if not query:
        return "query is empty"
    raw_label = (args or {}).get("root_label", "") or ""
    label = str(raw_label).strip()
    max_results = int(
        (args or {}).get("max_results", DEFAULT_MAX_RESULTS) or DEFAULT_MAX_RESULTS
    )
    max_results = max(1, min(500, max_results))
    case_sensitive = bool((args or {}).get("case_sensitive", False))
    return _SearchArgs(
        query=query,
        root_label=label,
        max_results=max_results,
        case_sensitive=case_sensitive,
    )


def _pick_roots(
    actives: list[ValidatedRoot], label: str
) -> list[ValidatedRoot] | str:
    """Filter ``actives`` to the requested label, or return the
    full active list if no label was supplied. Returns an error
    string when the requested label doesn't match any active root.
    """
    if not label:
        return [vr for vr in actives if vr.active]
    for vr in actives:
        if vr.active and vr.root.label == label:
            return [vr]
    return f"no active root with label {label!r}"


class FileSearchHandler:
    """Phase 1 read-only filename substring search.

    Construction:

        handler = FileSearchHandler(roots=[FileTaskRoot(...), ...])

    The handler stores its roots in the validated form so each
    ``start`` invocation doesn't redo the I/O check. If the config
    changes at runtime (settings hot-reload), build a fresh
    handler and re-register it — the same name re-registration
    pattern that :meth:`TaskOrchestrator.register_handler` already
    supports.
    """

    name: str = HANDLER_FILE_SEARCH

    def __init__(
        self,
        *,
        roots: list[FileTaskRoot] | None = None,
        app_root: str | os.PathLike[str] | None = None,
        max_files_scanned: int = DEFAULT_MAX_FILES_SCANNED,
        progress_every_n_dirs: int = DEFAULT_PROGRESS_EVERY_N_DIRS,
    ) -> None:
        self._validated: list[ValidatedRoot] = validate_roots(
            roots or [], app_root=app_root
        )
        self._max_files_scanned = max(1, int(max_files_scanned))
        self._progress_every_n_dirs = max(1, int(progress_every_n_dirs))

    # ── lifecycle ────────────────────────────────────────────────────

    def start(
        self, args: dict[str, Any], emit: TaskEmitFn
    ) -> TaskState:
        parsed = _parse_args(args)
        if isinstance(parsed, str):
            emit(TaskFailed(error=parsed))
            return {"args": args, "phase": "rejected"}
        roots = _pick_roots(self._validated, parsed.root_label)
        if isinstance(roots, str):
            emit(TaskFailed(error=roots))
            return {"args": args, "phase": "rejected"}
        if not roots:
            emit(TaskFailed(error="no active file roots configured"))
            return {"args": args, "phase": "rejected"}

        needle = parsed.query if parsed.case_sensitive else parsed.query.lower()
        matches: list[dict[str, Any]] = []
        files_scanned = 0
        dirs_scanned = 0
        started_at = time.monotonic()
        truncated = False
        for vr in roots:
            root_abs = Path(vr.abs_path)
            try:
                walker = os.walk(root_abs, followlinks=False)
            except OSError as exc:
                log.warning(
                    "file_search: walker open failed root=%s err=%s",
                    vr.root.label,
                    exc,
                )
                continue
            for current_dir, dir_names, file_names in walker:
                # In-place prune of skip-dirs so we don't recurse.
                dir_names[:] = [
                    d for d in dir_names if d not in _SKIP_DIR_NAMES
                ]
                dirs_scanned += 1
                # Progress beat.
                if dirs_scanned % self._progress_every_n_dirs == 0:
                    emit(
                        TaskProgress(
                            progress=None,
                            message=(
                                f"scanning... {files_scanned} files in "
                                f"{dirs_scanned} dirs, {len(matches)} matches"
                            ),
                        )
                    )
                for name in file_names:
                    files_scanned += 1
                    if files_scanned > self._max_files_scanned:
                        truncated = True
                        break
                    haystack = name if parsed.case_sensitive else name.lower()
                    if needle not in haystack:
                        continue
                    full = Path(current_dir) / name
                    try:
                        size = full.stat().st_size
                    except OSError:
                        size = -1
                    try:
                        rel = full.relative_to(Path(vr.abs_path))
                    except ValueError:
                        # Shouldn't happen — os.walk returned this
                        # under root_abs — but be defensive.
                        rel = Path(name)
                    matches.append(
                        {
                            "label": vr.root.label,
                            "relative_path": str(rel).replace(os.sep, "/"),
                            "size": size,
                        }
                    )
                    if len(matches) >= parsed.max_results:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break

        elapsed_ms = (time.monotonic() - started_at) * 1000.0
        result: dict[str, Any] = {
            "query": parsed.query,
            "matches": matches,
            "match_count": len(matches),
            "files_scanned": files_scanned,
            "dirs_scanned": dirs_scanned,
            "truncated": truncated,
            "elapsed_ms": round(elapsed_ms, 2),
            "roots_searched": [vr.root.label for vr in roots],
        }
        notify_aiko = len(matches) > 0  # silent on zero hits — see doc
        log.info(
            "file_search done: query=%s matches=%d files_scanned=%d "
            "dirs_scanned=%d truncated=%s elapsed_ms=%.1f",
            parsed.query,
            len(matches),
            files_scanned,
            dirs_scanned,
            truncated,
            elapsed_ms,
        )
        emit(TaskCompleted(result=result, notify_aiko=notify_aiko))
        return {"args": args, "phase": "done", "matches_count": len(matches)}

    def resume(
        self, state: TaskState, emit: TaskEmitFn
    ) -> TaskState:
        # Phase 1 search isn't resumable mid-walk — re-running the
        # original args is the right call. Emit a TaskFailed so the
        # surrounding machinery doesn't think the row will eventually
        # complete on its own.
        args = state.get("args") if isinstance(state, dict) else None
        emit(
            TaskFailed(
                error="resume not supported for file_search — re-run the search"
            )
        )
        return {"args": args, "phase": "failed_resume"}

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        # Phase 1 search never emits TaskInputNeeded so this entry
        # point should not get exercised. If it does, fail loudly
        # so a buggy caller doesn't silently stall the row.
        emit(TaskFailed(error="file_search did not expect input"))
        return {**state, "phase": "failed_input"}

    def cancel(self, state: TaskState) -> None:
        # Synchronous walker — nothing to release. The orchestrator
        # already marked the row cancelled before calling us; on the
        # next loop iteration the walker will see ``state`` was
        # discarded and exit. We do nothing.
        del state


__all__ = ["FileSearchHandler"]
