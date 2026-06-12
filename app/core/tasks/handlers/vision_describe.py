"""Vision-describe task handler — local multimodal image understanding.

Lets Aiko look at an image inside a configured file root (or the managed
``Attachments`` root) and describe it, using the **single local Ollama
model already loaded for workers / workflow** — there is no second model
and no dedicated vision client. The handler is handed:

* a ``client_provider`` that returns the live worker
  :class:`~app.llm.ollama_client.OllamaClient` (the one whose model is
  already resident in VRAM), and
* a ``model_provider`` that returns either the ``agent.vision.model``
  override or the effective worker model.

The only requirement is that this one worker model is multimodal (e.g.
``qwen3.5:27b`` / ``qwen3.6:27b``). Ollama's ``/api/chat`` accepts a
base64 ``images`` list per message, so the call is just a normal
``chat`` with the encoded image attached.

Reachable only as the ``describe_image`` :class:`WorkflowSkill` child of
a goal workflow — never a fast brain tool (a vision pass is seconds, not
milliseconds). The operation is read-only, so it does NOT touch the
approval framework.

Threading + safety mirror :class:`FileReadHandler`: the handler runs
synchronously on a worker thread; ``start`` / ``on_input`` are bounded
(resolve + read + one network call). The multi-root ambiguity case is
handled identically (bare path matching several roots → ``TaskInputNeeded``).

Safety caps (read live off ``agent.vision`` via constructor kwargs):

* ``max_bytes`` — hard cap on the image file size that gets base64'd
  and sent to Ollama. Larger files are refused (not truncated — a
  truncated image is garbage to a vision model).
* ``allowed_extensions`` — case-insensitive image extension allow-list.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core.tasks.handler_names import HANDLER_VISION_DESCRIBE
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


log = logging.getLogger("app.tasks.vision_describe")


DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_PROMPT = (
    "Look at this image and describe what you see in a few natural "
    "sentences."
)
# Cap on the description preview lifted into the terse task cue.
_PREVIEW_CHARS = 280
# Provider type alias for the worker chat client (kept loose to avoid a
# hard import cycle on the protocol).
ClientProvider = Callable[[], Any]
ModelProvider = Callable[[], str]


# ── args ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _DescribeArgs:
    path: str
    question: str


def _parse_args(args: dict[str, Any]) -> _DescribeArgs | str:
    """Validate the ``args`` dict → parsed args or a short error string."""
    raw = args or {}
    path = raw.get("path", "") or ""
    if not isinstance(path, str):
        return "path must be a string"
    path = path.strip()
    if not path:
        return "path is empty"
    question = raw.get("question", "") or raw.get("prompt", "") or ""
    if not isinstance(question, str):
        question = ""
    return _DescribeArgs(path=path, question=question.strip())


def _extension_allowed(
    abs_path: str, allowed_extensions: tuple[str, ...]
) -> bool:
    """True if ``abs_path``'s extension is in ``allowed_extensions``.

    Empty allow-list = allow everything. Otherwise case-insensitive
    suffix match; extension-less files are rejected when the allow-list
    is non-empty (an image with no extension is suspicious).
    """
    if not allowed_extensions:
        return True
    suffix = Path(abs_path).suffix.lower()
    if not suffix:
        return False
    return suffix in allowed_extensions


def _preview(text: str) -> str:
    """One-liner preview of the description for the terse task cue."""
    stripped = (text or "").strip()
    if not stripped:
        return "(no description)"
    flattened = " ".join(stripped.split())
    if len(flattened) > _PREVIEW_CHARS:
        return flattened[:_PREVIEW_CHARS].rstrip() + "\u2026"
    return flattened


def _format_candidates(candidates: tuple[ResolvedPath, ...]) -> list[str]:
    """Render candidates as ``"<label>:<relative_path>"`` strings."""
    return [f"{c.label}:{c.relative_path}" for c in candidates]


# ── handler ─────────────────────────────────────────────────────────────────


class VisionDescribeHandler:
    """Describe an image with the local multimodal worker model.

    Construction mirrors :class:`FileReadHandler` plus two providers so a
    settings hot-reload / worker-client rebuild is picked up without
    re-registering: ``client_provider`` returns the worker
    :class:`OllamaClient`, ``model_provider`` returns the model name to
    send (override or effective worker model).
    """

    name: str = HANDLER_VISION_DESCRIBE

    def __init__(
        self,
        *,
        roots: list[FileTaskRoot] | None = None,
        app_root: str | os.PathLike[str] | None = None,
        client_provider: ClientProvider | None = None,
        model_provider: ModelProvider | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allowed_extensions: tuple[str, ...] = (),
        default_prompt: str = DEFAULT_PROMPT,
    ) -> None:
        self._validated: list[ValidatedRoot] = validate_roots(
            roots or [], app_root=app_root
        )
        self._client_provider = client_provider
        self._model_provider = model_provider
        self._max_bytes = max(1024, int(max_bytes))
        self._allowed_extensions: tuple[str, ...] = tuple(
            (ext if ext.startswith(".") else "." + ext).lower()
            for ext in allowed_extensions
            if isinstance(ext, str) and ext.strip()
        )
        self._default_prompt = (default_prompt or "").strip() or DEFAULT_PROMPT

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, args: dict[str, Any], emit: TaskEmitFn) -> TaskState:
        parsed = _parse_args(args)
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
                candidate_strings = _format_candidates(resolved.candidates)
                emit(
                    TaskInputNeeded(
                        prompt=(
                            f"The image path {parsed.path!r} matches "
                            f"{len(resolved.candidates)} configured roots. "
                            "Which one did you mean? Reply with the "
                            "label-prefixed path (e.g. "
                            f"'{candidate_strings[0]}')."
                        ),
                        options=candidate_strings,
                    )
                )
                log.info(
                    "vision_describe: awaiting input (multi-root): path=%r "
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
                "vision_describe: failed resolve: path=%r reason=%s",
                parsed.path,
                resolved.reason,
            )
            return {"args": args, "phase": "rejected"}
        return self._complete_with_describe(args, resolved, parsed.question, emit)

    def resume(self, state: TaskState, emit: TaskEmitFn) -> TaskState:
        emit(
            TaskFailed(
                error="vision_describe does not support resume; restart it"
            )
        )
        return state

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        candidates = list(state.get("candidates") or [])
        args = dict(state.get("args") or {})
        if not candidates:
            emit(TaskFailed(error="no candidates remembered; restart it"))
            return {"args": args, "phase": "rejected"}
        raw = (answer or "").strip()
        if not raw:
            emit(TaskFailed(error="answer is empty"))
            return state
        chosen_path = self._match_answer(raw, candidates)
        if chosen_path is None:
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
            return {
                **state,
                "retries": retries,
                "phase": "awaiting_disambiguation",
            }
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
        question = str(args.get("question", "") or args.get("prompt", "") or "")
        log.info(
            "vision_describe: on_input resolved: chosen=%r label=%s rel=%s",
            chosen_path,
            resolved.label,
            resolved.relative_path,
        )
        return self._complete_with_describe(args, resolved, question, emit)

    def cancel(self, state: TaskState) -> None:
        return None

    # ── helpers ──────────────────────────────────────────────────────

    def _complete_with_describe(
        self,
        args: dict[str, Any],
        resolved: ResolvedPath,
        question: str,
        emit: TaskEmitFn,
    ) -> TaskState:
        """Read the image, run the vision call, emit Completed / Failed."""
        # Client + model resolution. A missing provider / non-Ollama
        # client / empty model are all "vision is not actually available"
        # — fail with a clear, user-actionable reason.
        client = self._client_provider() if self._client_provider else None
        if client is None:
            emit(TaskFailed(error="vision is unavailable (no worker model)"))
            return {"args": args, "phase": "rejected"}
        # Ollama-shaped image passthrough only. A remote OpenAI-compatible
        # worker client would need a different image envelope; rather than
        # silently send nothing, fail clearly.
        try:
            from app.llm.ollama_client import OllamaClient
        except Exception:  # pragma: no cover - import guard
            OllamaClient = None  # type: ignore[assignment]
        if OllamaClient is not None and not isinstance(client, OllamaClient):
            emit(
                TaskFailed(
                    error=(
                        "the current worker client can't accept images; set "
                        "a local multimodal Ollama worker model (e.g. "
                        "qwen3.5:27b) and keep workers on local Ollama"
                    )
                )
            )
            return {"args": args, "phase": "rejected"}
        model = ""
        if self._model_provider:
            try:
                model = (self._model_provider() or "").strip()
            except Exception:
                model = ""

        # Image read + caps.
        read = self._read_image(resolved)
        if isinstance(read, str):
            emit(TaskFailed(error=read))
            log.info(
                "vision_describe: failed read: label=%s rel=%s reason=%s",
                resolved.label,
                resolved.relative_path,
                read,
            )
            return {
                "args": args,
                "phase": "rejected",
                "label": resolved.label,
                "relative_path": resolved.relative_path,
            }
        b64, size_bytes = read
        prompt = (question or "").strip() or self._default_prompt
        messages = [
            {"role": "user", "content": prompt, "images": [b64]}
        ]
        try:
            description = client.chat(
                messages, model=model or None, think=False,
                surface="vision_describe",
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a friendly reason
            reason = self._friendly_call_error(exc, model)
            emit(TaskFailed(error=reason))
            log.warning(
                "vision_describe: call failed: label=%s rel=%s model=%s exc=%r",
                resolved.label,
                resolved.relative_path,
                model or "(worker default)",
                exc,
            )
            return {
                "args": args,
                "phase": "rejected",
                "label": resolved.label,
                "relative_path": resolved.relative_path,
            }
        description = (description or "").strip()
        if not description:
            emit(
                TaskFailed(
                    error=(
                        "vision model returned an empty description (is the "
                        "worker model multimodal?)"
                    )
                )
            )
            return {
                "args": args,
                "phase": "rejected",
                "label": resolved.label,
                "relative_path": resolved.relative_path,
            }
        result = {
            "label": resolved.label,
            "relative_path": resolved.relative_path,
            "description": description,
            # ``content`` mirrors file_read so the reply-on-complete turn
            # renders the full text; ``summary`` is the terse cue the
            # workflow blackboard + passive cue path lift.
            "content": description,
            "summary": _preview(description),
            "size_bytes": size_bytes,
            "model": model or "(worker default)",
        }
        log.info(
            "vision_describe: completed: label=%s rel=%s bytes=%d model=%s "
            "desc_chars=%d",
            resolved.label,
            resolved.relative_path,
            size_bytes,
            model or "(worker default)",
            len(description),
        )
        emit(TaskCompleted(result=result))
        return {
            "args": args,
            "phase": "done",
            "label": resolved.label,
            "relative_path": resolved.relative_path,
        }

    def _read_image(self, resolved: ResolvedPath) -> tuple[str, int] | str:
        """Read + base64-encode the image. Returns ``(b64, size)`` or error."""
        abs_path = resolved.abs_path
        if not _extension_allowed(abs_path, self._allowed_extensions):
            return (
                f"image extension not allowed: "
                f"{Path(abs_path).suffix or '(none)'}"
            )
        try:
            stat = os.stat(abs_path)
        except OSError as exc:
            return f"could not stat image: {exc}"
        if not os.path.isfile(abs_path):
            return "path is not a regular file"
        size_bytes = int(stat.st_size)
        if size_bytes <= 0:
            return "image file is empty"
        if size_bytes > self._max_bytes:
            return (
                f"image too large: {size_bytes} bytes "
                f"(limit {self._max_bytes})"
            )
        try:
            with open(abs_path, "rb") as fh:
                raw = fh.read(self._max_bytes + 1)
        except OSError as exc:
            return f"could not read image: {exc}"
        if len(raw) > self._max_bytes:
            return (
                f"image too large: >{self._max_bytes} bytes "
                "(refusing to truncate)"
            )
        return base64.b64encode(raw).decode("ascii"), len(raw)

    @staticmethod
    def _friendly_call_error(exc: Exception, model: str) -> str:
        """Map a raw chat exception to a short, user-actionable reason."""
        text = str(exc).lower()
        named = model or "the worker model"
        if "not found" in text or "404" in text:
            return (
                f"vision model {named!r} is not available — pull it first "
                f"(ollama pull {model or '<model>'})"
            )
        if "image" in text or "vision" in text or "multimodal" in text:
            return (
                f"{named!r} can't process images — set a multimodal worker "
                "model (e.g. qwen3.5:27b)"
            )
        return f"vision call failed: {exc}"

    @staticmethod
    def _match_answer(answer: str, candidates: list[str]) -> str | None:
        """Match ``answer`` against the candidate list (see file_read)."""
        if not answer:
            return None
        lower = answer.strip().lower()
        for c in candidates:
            if c.lower() == lower:
                return c
        if ":" not in lower:
            label_hits = [
                c for c in candidates if c.split(":", 1)[0].lower() == lower
            ]
            if len(label_hits) == 1:
                return label_hits[0]
        if ":" not in lower:
            path_hits = [
                c for c in candidates
                if (":" in c) and c.split(":", 1)[1].lower() == lower
            ]
            if len(path_hits) == 1:
                return path_hits[0]
        return None


__all__ = [
    "VisionDescribeHandler",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_PROMPT",
]
