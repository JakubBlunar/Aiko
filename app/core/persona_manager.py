"""Live2D persona management.

Owns ``data/personas/active/`` -- the single currently-loaded Live2D model.
Handles zip-upload validation, extraction, manifest writing, and reaction
mapping persistence.

The renderer side (React + ``pixi-live2d-display``) consumes the manifest
returned by :meth:`PersonaManager.current` and fetches the actual model
files via the FastAPI static mount ``/personas/active/``.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


log = logging.getLogger("app.persona_manager")


# ── Constants ───────────────────────────────────────────────────────────

# Reactions Aiko can emit (must stay in sync with the persona prompt).
REACTIONS: tuple[str, ...] = (
    "neutral",
    "cheerful",
    "excited",
    "surprised",
    "sad",
    "angry",
    "calm",
    "serious",
    "friendly",
    "gentle",
    "enthusiastic",
)

# Synonyms the default-mapping fuzzy match looks for in expression filenames.
_REACTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "neutral": ("normal", "default", "neutral", "idle"),
    "cheerful": ("smile", "happy", "joy", "cheer", "cheerful", "grin"),
    "excited": ("excite", "wow", "yay", "shine", "sparkle"),
    "surprised": ("surprise", "shock", "wow", "gasp"),
    "sad": ("sad", "cry", "tear", "unhappy", "sob"),
    "angry": ("angry", "anger", "mad", "rage", "pout"),
    "calm": ("calm", "relax", "peace", "soft"),
    "serious": ("serious", "stern", "thinking", "frown"),
    "friendly": ("friendly", "smile", "wink", "warm"),
    "gentle": ("gentle", "soft", "kind", "warm"),
    "enthusiastic": ("excite", "shine", "sparkle", "yay", "fun"),
}

# Hard caps to defang malicious zips.
_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024   # 500 MB
_MAX_MEMBER_COUNT = 5000
_MAX_ENTRY_DEPTH = 6  # `BanG Dream/asneeded/live2d/chara/001/.../*.model.json`
_MANIFEST_FILENAME = "_persona.json"
_MAPPING_FILENAME = "_mapping.json"


# ── Data shapes ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class ExpressionRef:
    name: str
    file: str  # relative to active root


@dataclass(slots=True)
class MotionRef:
    name: str
    file: str


@dataclass(slots=True)
class PersonaManifest:
    id: str
    display_name: str
    cubism_version: int  # 2 or 3
    entry_filename: str  # relative to active root, e.g. "senko.model3.json"
    expressions: list[ExpressionRef] = field(default_factory=list)
    motions: dict[str, list[MotionRef]] = field(default_factory=dict)
    reaction_mapping: dict[str, str] = field(default_factory=dict)
    idle_motion_group: str | None = None
    talk_motion_group: str | None = None
    uploaded_at: str = ""

    def to_dict(self) -> dict:
        out = asdict(self)
        # asdict serializes nested dataclasses already; stable JSON ordering
        # isn't required but keeps diffs readable.
        return out


class PersonaError(Exception):
    """Raised when an upload is rejected. Message is safe to surface to the UI."""


# ── Manager ─────────────────────────────────────────────────────────────


class PersonaManager:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._active_dir = self._root / "active"
        self._root.mkdir(parents=True, exist_ok=True)

    # ── public API ────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    @property
    def active_dir(self) -> Path:
        return self._active_dir

    def current(self) -> PersonaManifest | None:
        manifest_path = self._active_dir / _MANIFEST_FILENAME
        if not manifest_path.is_file():
            return None
        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            log.warning("active persona manifest is unreadable; treating as none")
            return None
        return _manifest_from_dict(data)

    def install_from_zip(
        self,
        zip_stream: IO[bytes],
        *,
        display_name: str = "",
    ) -> PersonaManifest:
        """Validate, extract, and register a Live2D model zip.

        Raises :class:`PersonaError` with a user-friendly message on rejection.
        On success, replaces any previously-active persona.
        """
        with zipfile.ZipFile(zip_stream) as zf:
            members = _validate_zip(zf)
            entry_member = _pick_entry_member(members)

            # Tentative extraction into a sibling staging dir, then atomic swap.
            staging = self._root / f"_staging_{uuid.uuid4().hex[:8]}"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            try:
                _extract_safely(zf, members, staging)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise

        # Build the manifest by parsing the entry JSON inside the staged copy.
        entry_rel = entry_member.filename
        try:
            manifest = _build_manifest(
                staging,
                entry_rel,
                display_name=display_name or _derive_display_name(entry_rel),
            )
        except PersonaError:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # Atomic-ish swap: remove old active, rename staging -> active.
        if self._active_dir.exists():
            shutil.rmtree(self._active_dir)
        staging.rename(self._active_dir)

        self._write_manifest(manifest)
        log.info(
            "installed persona %s (cubism v%d, entry=%s, %d expressions)",
            manifest.display_name,
            manifest.cubism_version,
            manifest.entry_filename,
            len(manifest.expressions),
        )
        return manifest

    def delete(self) -> bool:
        if not self._active_dir.exists():
            return False
        shutil.rmtree(self._active_dir, ignore_errors=True)
        log.info("removed active persona")
        return True

    def update_mapping(
        self,
        *,
        reaction_mapping: dict[str, str] | None = None,
        idle_motion_group: str | None = None,
        talk_motion_group: str | None = None,
    ) -> PersonaManifest | None:
        manifest = self.current()
        if manifest is None:
            return None
        if reaction_mapping is not None:
            allowed_expressions = {e.name for e in manifest.expressions}
            cleaned: dict[str, str] = {}
            for reaction, expr_name in reaction_mapping.items():
                key = str(reaction or "").strip().lower()
                if key not in REACTIONS:
                    continue
                value = str(expr_name or "").strip()
                if not value:
                    continue
                if value not in allowed_expressions:
                    continue
                cleaned[key] = value
            manifest.reaction_mapping = cleaned
        if idle_motion_group is not None:
            manifest.idle_motion_group = (
                idle_motion_group if idle_motion_group in manifest.motions else None
            )
        if talk_motion_group is not None:
            manifest.talk_motion_group = (
                talk_motion_group if talk_motion_group in manifest.motions else None
            )
        self._write_manifest(manifest)
        return manifest

    # ── internals ─────────────────────────────────────────────────────

    def _write_manifest(self, manifest: PersonaManifest) -> None:
        path = self._active_dir / _MANIFEST_FILENAME
        path.write_text(
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_zip(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Sanity-check the zip and return its members.

    Rejects: too many members, zip-bomb sized, absolute / .. paths, symlinks,
    no model entry. Returns the validated member list.
    """
    members = zf.infolist()
    if len(members) == 0:
        raise PersonaError("Zip is empty.")
    if len(members) > _MAX_MEMBER_COUNT:
        raise PersonaError(
            f"Zip has {len(members)} files; maximum {_MAX_MEMBER_COUNT} allowed."
        )
    total = 0
    for m in members:
        # Reject zip-slip (..) and absolute paths.
        normalized = m.filename.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise PersonaError(f"Zip contains unsafe path: {m.filename!r}")
        # Reject symlinks (high-bit mode 0xA000).
        external = (m.external_attr >> 16) & 0xFFFF
        if (external & 0xF000) == 0xA000:
            raise PersonaError(f"Zip contains a symlink: {m.filename!r}")
        total += int(m.file_size)
        if total > _MAX_UNCOMPRESSED_BYTES:
            raise PersonaError(
                "Zip is too large when uncompressed (over 500 MB)."
            )
    if not _has_entry_member(members):
        raise PersonaError(
            "Zip does not contain a Live2D model entry "
            "(expected a *.model3.json or *.model.json file)."
        )
    return members


def _has_entry_member(members: list[zipfile.ZipInfo]) -> bool:
    for m in members:
        if _is_entry_filename(m.filename):
            return True
    return False


def _is_entry_filename(name: str) -> bool:
    lowered = name.lower().replace("\\", "/")
    if lowered.endswith("/"):
        return False
    base = lowered.rsplit("/", 1)[-1]
    return base.endswith(".model3.json") or base.endswith(".model.json")


def _pick_entry_member(members: list[zipfile.ZipInfo]) -> zipfile.ZipInfo:
    """Pick the shallowest entry; alphabetical tiebreak. Prefer model3 over model."""
    candidates = [m for m in members if _is_entry_filename(m.filename)]
    if not candidates:
        raise PersonaError("No Live2D model entry found in zip.")

    def sort_key(m: zipfile.ZipInfo) -> tuple[int, int, str]:
        normalized = m.filename.replace("\\", "/")
        depth = normalized.count("/")
        # Cubism 3+ first (we get more features).
        is_cubism2 = 0 if normalized.lower().endswith(".model3.json") else 1
        return (depth, is_cubism2, normalized.lower())

    chosen = sorted(candidates, key=sort_key)[0]
    normalized_depth = chosen.filename.replace("\\", "/").count("/")
    if normalized_depth > _MAX_ENTRY_DEPTH:
        raise PersonaError(
            f"Model entry is too deeply nested ({normalized_depth} levels). "
            "Re-zip the model folder so the .model.json is closer to the root."
        )
    return chosen


def _extract_safely(
    zf: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    target: Path,
) -> None:
    target_resolved = target.resolve()
    for m in members:
        # Directory entries are recreated implicitly when needed.
        if m.filename.endswith("/"):
            continue
        member_path = (target / m.filename).resolve()
        # Final defensive check: extraction stays within target.
        try:
            member_path.relative_to(target_resolved)
        except ValueError as exc:
            raise PersonaError(
                f"Refusing to extract path outside target: {m.filename!r}"
            ) from exc
        member_path.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, open(member_path, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _derive_display_name(entry_rel: str) -> str:
    base = Path(entry_rel.replace("\\", "/")).stem
    base = base.removesuffix(".model3").removesuffix(".model")
    cleaned = re.sub(r"[_\-]+", " ", base).strip()
    return cleaned or "Persona"


def _build_manifest(
    root: Path,
    entry_rel: str,
    *,
    display_name: str,
) -> PersonaManifest:
    entry_path = (root / entry_rel).resolve()
    if not entry_path.is_file():
        raise PersonaError(f"Entry file vanished after extraction: {entry_rel!r}")
    try:
        with entry_path.open("r", encoding="utf-8") as f:
            entry_data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise PersonaError(f"Could not parse model entry JSON: {exc}") from exc

    cubism_version = 3 if entry_rel.lower().endswith(".model3.json") else 2

    if cubism_version == 3:
        expressions, motions = _parse_cubism3(entry_data)
    else:
        expressions, motions = _parse_cubism2(entry_data)

    reaction_mapping = _default_reaction_mapping(expressions)

    idle_group = _pick_motion_group(motions, ("idle", "tick", "loop"))
    talk_group = _pick_motion_group(motions, ("tap", "talk", "anim", "story"))

    return PersonaManifest(
        id=uuid.uuid4().hex[:12],
        display_name=display_name,
        cubism_version=cubism_version,
        entry_filename=entry_rel.replace("\\", "/"),
        expressions=expressions,
        motions=motions,
        reaction_mapping=reaction_mapping,
        idle_motion_group=idle_group,
        talk_motion_group=talk_group,
        uploaded_at=_now_iso(),
    )


def _parse_cubism3(
    data: dict,
) -> tuple[list[ExpressionRef], dict[str, list[MotionRef]]]:
    refs = data.get("FileReferences") or {}
    expressions: list[ExpressionRef] = []
    for entry in refs.get("Expressions") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("Name") or "").strip()
        file_rel = str(entry.get("File") or "").strip()
        if not name and file_rel:
            name = Path(file_rel).stem.removesuffix(".exp3")
        if not file_rel or not name:
            continue
        expressions.append(ExpressionRef(name=name, file=file_rel.replace("\\", "/")))

    motions: dict[str, list[MotionRef]] = {}
    raw_motions = refs.get("Motions") or {}
    if isinstance(raw_motions, dict):
        for group, entries in raw_motions.items():
            if not isinstance(entries, list):
                continue
            # Real-world models often store every motion under the empty-string
            # group key. Preserve those by renaming to ``default`` so the UI
            # has a stable, clickable label.
            group_name = str(group).strip() or "default"
            collected: list[MotionRef] = []
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                file_rel = str(entry.get("File") or "").strip()
                if not file_rel:
                    continue
                name = str(entry.get("Name") or "").strip() or Path(file_rel).stem
                collected.append(
                    MotionRef(name=name, file=file_rel.replace("\\", "/"))
                )
            if collected:
                motions[group_name] = collected
    return expressions, motions


def _parse_cubism2(
    data: dict,
) -> tuple[list[ExpressionRef], dict[str, list[MotionRef]]]:
    expressions: list[ExpressionRef] = []
    for entry in data.get("expressions") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        file_rel = str(entry.get("file") or "").strip()
        if not name and file_rel:
            name = Path(file_rel).stem
        if not file_rel or not name:
            continue
        expressions.append(ExpressionRef(name=name, file=file_rel.replace("\\", "/")))

    motions: dict[str, list[MotionRef]] = {}
    raw_motions = data.get("motions") or {}
    if isinstance(raw_motions, dict):
        for group, entries in raw_motions.items():
            if not isinstance(entries, list):
                continue
            group_name = str(group).strip() or "default"
            collected: list[MotionRef] = []
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                file_rel = str(entry.get("file") or "").strip()
                if not file_rel:
                    continue
                name = str(entry.get("name") or "").strip() or Path(file_rel).stem
                collected.append(
                    MotionRef(name=name, file=file_rel.replace("\\", "/"))
                )
            if collected:
                motions[group_name] = collected
    return expressions, motions


def _default_reaction_mapping(
    expressions: list[ExpressionRef],
) -> dict[str, str]:
    if not expressions:
        return {}
    mapping: dict[str, str] = {}
    expression_keys = [(e.name, e.name.lower(), e.file.lower()) for e in expressions]
    for reaction in REACTIONS:
        synonyms = _REACTION_SYNONYMS.get(reaction, (reaction,))
        chosen: str | None = None
        for syn in synonyms:
            for original_name, lower_name, lower_file in expression_keys:
                if syn in lower_name or syn in lower_file:
                    chosen = original_name
                    break
            if chosen is not None:
                break
        if chosen is not None:
            mapping[reaction] = chosen
    return mapping


def _pick_motion_group(
    motions: dict[str, list[MotionRef]],
    keywords: tuple[str, ...],
) -> str | None:
    if not motions:
        return None
    lowered = {name: name.lower() for name in motions.keys()}
    for keyword in keywords:
        for original, low in lowered.items():
            if keyword in low:
                return original
    # Fall back to the first group so idle has *something* to play.
    return next(iter(motions.keys()))


def _manifest_from_dict(data: dict) -> PersonaManifest | None:
    try:
        expressions = [
            ExpressionRef(
                name=str(e.get("name") or ""),
                file=str(e.get("file") or ""),
            )
            for e in (data.get("expressions") or [])
            if isinstance(e, dict)
        ]
        motions: dict[str, list[MotionRef]] = {}
        for group, entries in (data.get("motions") or {}).items():
            if not isinstance(entries, list):
                continue
            motions[str(group)] = [
                MotionRef(
                    name=str(e.get("name") or ""),
                    file=str(e.get("file") or ""),
                )
                for e in entries
                if isinstance(e, dict)
            ]
        return PersonaManifest(
            id=str(data.get("id") or ""),
            display_name=str(data.get("display_name") or "Persona"),
            cubism_version=int(data.get("cubism_version") or 3),
            entry_filename=str(data.get("entry_filename") or ""),
            expressions=expressions,
            motions=motions,
            reaction_mapping={
                str(k): str(v)
                for k, v in (data.get("reaction_mapping") or {}).items()
            },
            idle_motion_group=data.get("idle_motion_group"),
            talk_motion_group=data.get("talk_motion_group"),
            uploaded_at=str(data.get("uploaded_at") or ""),
        )
    except (TypeError, ValueError):
        return None
