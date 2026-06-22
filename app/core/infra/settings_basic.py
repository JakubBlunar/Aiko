from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class FileWriteSettings:
    """Per-capability resource config for the ``file_write`` task.

    The reusable pattern (see ``docs/task-approvals.md``): a destructive
    capability owns a small nested settings block grouping its resource
    knobs. The *approval* policy is generic and lives on
    :class:`AgentSettings` (``task_approval_mode`` /
    ``task_approval_overrides``); this block is only the file-write
    resource limits.

    ``enabled`` is the master switch — when off, the ``write_file``
    workflow skill is never offered to the planner and the handler is
    not registered. ``max_bytes`` caps the resulting file size.
    ``allowed_extensions`` is the case-insensitive write allow-list
    (empty = allow everything, same convention as the read handler).
    """

    enabled: bool = False
    max_bytes: int = 262144
    allowed_extensions: tuple[str, ...] = (
        ".txt", ".md", ".rst", ".log",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".csv", ".tsv",
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".html", ".css", ".xml",
        ".sh", ".bat", ".ps1",
        ".sql",
    )


@dataclass(slots=True)
class VisionSettings:
    """Resource config for the local-vision ``describe_image`` task.

    The vision task does NOT introduce a second model: it reuses the
    already-loaded worker Ollama client + worker model, so the only
    requirement is that the worker model is multimodal (e.g.
    ``qwen3.5:27b`` / ``qwen3.6:27b``). That's why there's no
    ``base_url`` / ``keep_alive`` / ``num_ctx`` here — those are
    inherited from the worker client so there is genuinely one model
    config to reason about.

    * ``enabled`` — master switch. Off = the ``describe_image`` workflow
      skill is not offered and the handler is not registered.
    * ``model`` — OPTIONAL override. Empty (the default + recommended)
      reuses the effective worker model. A non-empty value points the
      vision call at a different local model, accepting a load/reload.
    * ``max_bytes`` — hard cap on the image file size that will be
      base64-encoded and sent to Ollama.
    * ``timeout_seconds`` — per-call ceiling (vision inference + a
      possible cold model load can be slow).
    * ``allowed_extensions`` — case-insensitive image extension
      allow-list (empty = allow everything).
    * ``default_prompt`` — instruction sent alongside the image when the
      caller doesn't supply a question.
    """

    enabled: bool = False
    model: str = ""
    max_bytes: int = 8 * 1024 * 1024
    timeout_seconds: int = 180
    allowed_extensions: tuple[str, ...] = (
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    )
    default_prompt: str = (
        "Look at this image and describe what you see in a few natural "
        "sentences. Mention the main subject, setting, notable details, "
        "any visible text, and the overall mood."
    )


