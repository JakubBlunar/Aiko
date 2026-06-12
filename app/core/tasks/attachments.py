"""Managed attachments root for in-chat file attachments (D2 Part B).

When the user attaches an image or text file to a chat message, the
file is uploaded to a fixed managed directory (``data/attachments/``)
that is auto-registered as a **read-only** sandbox root labelled
``Attachments``. This lets the existing file handlers (``describe_image``
for images, ``read_file`` for text) resolve ``Attachments:<file>`` with
zero new path plumbing — the attachment path is just another root.

The directory is gitignored and size-capped on write. Filenames are
UUID-based (the original name is preserved only as display metadata) so
an upload can never traverse out of the root or collide.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.tasks.sandbox import FileTaskRoot


# Repo-root/data/attachments — mirrors the data-dir resolution used by
# SessionController (``Path(__file__).resolve().parents[3] / "data"``).
ATTACHMENTS_DIR: Path = (
    Path(__file__).resolve().parents[3] / "data" / "attachments"
)
ATTACHMENTS_LABEL = "Attachments"

# Default classification sets. The image set mirrors
# ``VisionSettings.allowed_extensions``; the text set mirrors the
# read-handler-friendly text formats. Callers may pass narrower sets
# (e.g. the live ``agent.vision.allowed_extensions``).
DEFAULT_IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
)
DEFAULT_TEXT_EXTENSIONS: tuple[str, ...] = (
    ".txt", ".md", ".rst", ".log",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".tsv",
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".css", ".xml",
    ".sh", ".bat", ".ps1",
    ".sql",
)

# Hard cap on a single attachment upload (8 MiB) — matches the vision
# byte cap so an image that uploads can also be described.
DEFAULT_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SavedAttachment:
    """One uploaded attachment, as returned to the client + persisted."""

    id: str
    filename: str
    kind: str  # "image" | "text"
    rel_path: str  # "Attachments:<uuid><ext>"
    bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "filename": self.filename,
            "kind": self.kind,
            "rel_path": self.rel_path,
            "bytes": self.bytes,
        }


def ensure_attachments_dir() -> Path:
    """Create the managed attachments dir if missing; return its path."""
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    return ATTACHMENTS_DIR


def attachments_root() -> FileTaskRoot:
    """The managed ``Attachments`` root (read-only) for the sandbox."""
    return FileTaskRoot(
        label=ATTACHMENTS_LABEL,
        path=str(ATTACHMENTS_DIR),
        read_only=True,
    )


def _normalise_exts(exts: tuple[str, ...] | None) -> tuple[str, ...]:
    if not exts:
        return ()
    return tuple(
        (e if e.startswith(".") else "." + e).lower()
        for e in exts
        if isinstance(e, str) and e.strip()
    )


def classify_extension(
    ext: str,
    *,
    image_extensions: tuple[str, ...] | None = None,
    text_extensions: tuple[str, ...] | None = None,
) -> str | None:
    """Return ``"image"`` / ``"text"`` for a file extension, else ``None``."""
    suffix = ext.lower()
    if not suffix.startswith("."):
        suffix = "." + suffix
    images = _normalise_exts(image_extensions) or DEFAULT_IMAGE_EXTENSIONS
    texts = _normalise_exts(text_extensions) or DEFAULT_TEXT_EXTENSIONS
    if suffix in images:
        return "image"
    if suffix in texts:
        return "text"
    return None


def save_attachment(
    *,
    data: bytes,
    filename: str,
    image_extensions: tuple[str, ...] | None = None,
    text_extensions: tuple[str, ...] | None = None,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
) -> SavedAttachment:
    """Validate + write one attachment into the managed root.

    Raises :class:`ValueError` on an empty body, an unsupported
    extension, or an oversize file. On success the bytes are written to
    ``data/attachments/<uuid><ext>`` and a :class:`SavedAttachment` is
    returned (the ``rel_path`` is ``Attachments:<uuid><ext>``).
    """
    name = (filename or "").strip()
    if not name:
        raise ValueError("missing filename")
    if not data:
        raise ValueError("attachment is empty")
    if len(data) > max_bytes:
        raise ValueError(
            f"attachment too large (limit {max_bytes // (1024 * 1024)} MB)"
        )
    suffix = Path(name).suffix.lower()
    kind = classify_extension(
        suffix,
        image_extensions=image_extensions,
        text_extensions=text_extensions,
    )
    if kind is None:
        raise ValueError(f"unsupported file type: {suffix or '(none)'}")
    ensure_attachments_dir()
    stored_id = uuid.uuid4().hex
    stored_name = f"{stored_id}{suffix}"
    dest = ATTACHMENTS_DIR / stored_name
    dest.write_bytes(data)
    return SavedAttachment(
        id=stored_id,
        filename=name,
        kind=kind,
        rel_path=f"{ATTACHMENTS_LABEL}:{stored_name}",
        bytes=len(data),
    )


def delete_attachment(stored_name: str) -> bool:
    """Delete one stored attachment file by its stored name (``<uuid><ext>``).

    Guards against traversal (the name must resolve back inside the
    managed dir). Returns True if a file was removed.
    """
    raw = (stored_name or "").strip()
    if not raw:
        return False
    # Accept either the bare stored name or a ``Attachments:<name>`` ref.
    if raw.startswith(ATTACHMENTS_LABEL + ":"):
        raw = raw.split(":", 1)[1]
    candidate = (ATTACHMENTS_DIR / raw).resolve()
    try:
        candidate.relative_to(ATTACHMENTS_DIR.resolve())
    except ValueError:
        return False
    if candidate.is_file():
        candidate.unlink()
        return True
    return False


__all__ = [
    "ATTACHMENTS_DIR",
    "ATTACHMENTS_LABEL",
    "DEFAULT_IMAGE_EXTENSIONS",
    "DEFAULT_TEXT_EXTENSIONS",
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "SavedAttachment",
    "ensure_attachments_dir",
    "attachments_root",
    "classify_extension",
    "save_attachment",
    "delete_attachment",
]
