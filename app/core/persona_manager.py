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
import os
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

# Characters that have URL syntactic meaning (fragment / query delimiters)
# and that browsers do NOT auto-percent-encode when they appear inside a
# path component. Live2D zips occasionally contain texture filenames like
# ``texture_00 #969.png`` (the ``#969`` is a deduplication tag from the
# original packaging tool); served as-is, the browser truncates the
# request at the ``#`` and the texture 404s. Sanitize to ``_`` on
# install. Spaces and other characters are left alone — browsers
# percent-encode them transparently.
_URL_UNSAFE_CHARS = re.compile(r"[#?]")


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
    # Cubism 3 ``Groups`` parameter IDs. ``lip_sync_ids`` drives mouth
    # animation from TTS amplitude — the renderer falls back to the
    # generic ``ParamMouthOpenY`` (or ``PARAM_MOUTH_OPEN_Y`` for legacy
    # Cubism-2-ported models) when this list is empty. ``eye_blink_ids``
    # is exposed for future use.
    lip_sync_ids: list[str] = field(default_factory=list)
    eye_blink_ids: list[str] = field(default_factory=list)
    # Display-time scale multiplier on top of the bounds-fit scale.
    # 1.0 = "fit the bounding box in the panel with a 0.92 margin". Many
    # Cubism rigs include large invisible mesh extents (raised arms,
    # particle hulls) that make the auto-fit feel zoomed out, so users
    # need a per-persona zoom knob.
    scale_multiplier: float = 1.0
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

        # Sanitize URL-unsafe characters in extracted filenames (``#``,
        # ``?``) and patch every JSON reference inside the staged tree so
        # the renderer can fetch them via the static mount. See
        # :data:`_URL_UNSAFE_CHARS` for why this is necessary.
        try:
            rename_map = _sanitize_extracted_paths(staging)
            if rename_map:
                _rewrite_json_references(staging, rename_map)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # Build the manifest by parsing the entry JSON inside the staged copy.
        # If the entry file itself was renamed during sanitization, follow
        # the rename so we read the right JSON below.
        entry_rel = entry_member.filename.replace("\\", "/")
        entry_rel = rename_map.get(entry_rel, entry_rel)

        # Many Live2D zips ship a minimal ``model3.json`` (one Idle motion,
        # no expressions) while the ``motions/`` and ``expressions/``
        # folders contain dozens of unreferenced files. Discover those
        # orphans and append them to the entry JSON so both the renderer
        # and our manifest parser see them.
        try:
            _augment_with_orphan_refs(staging, entry_rel)
        except Exception:
            log.warning("orphan-ref augmentation failed; continuing", exc_info=True)
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
        scale_multiplier: float | None = None,
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
        if scale_multiplier is not None:
            # Clamp to a sane range so a typo can't make the avatar
            # explode off-screen or shrink to a single pixel.
            try:
                value = float(scale_multiplier)
            except (TypeError, ValueError):
                value = 1.0
            if value != value:  # NaN
                value = 1.0
            manifest.scale_multiplier = max(0.3, min(4.0, value))
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


def _augment_with_orphan_refs(staging: Path, entry_rel: str) -> None:
    """Append on-disk motions / expressions that the entry JSON omits.

    Live2D zips frequently ship a stub ``model3.json`` that declares one
    Idle motion while the ``motions/`` folder holds dozens of unused
    ``.motion3.json`` files. Without this step the renderer never learns
    about them and the user sees a single-entry "Idle" group with no
    "Talk" option.

    Also normalizes the special empty-string motion group key (``""``)
    that real-world ripped models use to ``"default"`` so the JSON sent
    to ``pixi-live2d-display`` matches the manifest the UI builds — a
    mismatch here would silently break ``model.motion("default", ...)``
    even when the file references are correct.

    Mutates the entry JSON in place. Idempotent: only orphans (files
    referenced by neither group) are added. Failures are swallowed by
    the caller — we'd rather install with the original JSON than fail
    the upload over a best-effort enrichment.
    """
    entry_path = staging / entry_rel
    try:
        with entry_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("entry JSON unreadable for augmentation: %s", exc)
        return

    # All references inside the entry JSON are relative to the *entry
    # file's directory*, NOT to the staging root. Real-world zips often
    # have a top-level subdir (e.g. ``character_name/``) so the two
    # roots differ; comparing apples to apples here is essential or
    # every declared motion is mistaken for an orphan.
    entry_dir = entry_path.parent

    is_cubism3 = entry_rel.lower().endswith(".model3.json")
    if is_cubism3:
        _augment_cubism3(entry_dir, data)
    else:
        _augment_cubism2(entry_dir, data)

    try:
        entry_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("could not write augmented entry JSON %s: %s", entry_path, exc)


def _augment_cubism3(entry_dir: Path, data: dict) -> None:
    refs = data.setdefault("FileReferences", {})

    # ── Motions ─────────────────────────────────────────────────────
    motions_section = refs.get("Motions")
    if not isinstance(motions_section, dict):
        motions_section = {}

    # Real-world Cubism 3 zips often declare every motion under the
    # empty-string group key. ``pixi-live2d-display`` is happy to play
    # them by passing ``""`` to ``model.motion()``, but our UI dropdown
    # would render an unselectable blank label. Rename to ``default``
    # in the JSON so the manifest, the UI, and the renderer all agree.
    if "" in motions_section:
        existing_default = motions_section.pop("")
        if isinstance(existing_default, list):
            current_default = motions_section.get("default")
            if isinstance(current_default, list):
                current_default.extend(existing_default)
            else:
                motions_section["default"] = existing_default

    declared_motions: set[str] = set()
    for entries in motions_section.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                rel = str(entry.get("File") or "").strip().replace("\\", "/")
                if rel:
                    declared_motions.add(rel)

    orphans: list[str] = []
    for p in entry_dir.rglob("*.motion3.json"):
        rel = str(p.relative_to(entry_dir)).replace("\\", "/")
        if rel not in declared_motions:
            orphans.append(rel)
    orphans.sort()

    if orphans:
        # Group by parent directory name when it's something other than
        # the bare ``motions`` root. ``motions/idle/x.motion3.json`` ->
        # group "Idle"; ``motions/x.motion3.json`` -> group "Extra".
        grouped: dict[str, list[dict]] = {}
        for rel in orphans:
            parts = rel.split("/")
            if len(parts) >= 3 and parts[0].lower() == "motions":
                group = parts[1] or "Extra"
            else:
                group = "Extra"
            display = group if group != group.lower() else group.capitalize()
            grouped.setdefault(display, []).append({"File": rel})
        for group_name, entries in grouped.items():
            existing = motions_section.get(group_name)
            if isinstance(existing, list):
                existing.extend(entries)
            else:
                motions_section[group_name] = entries

    refs["Motions"] = motions_section

    # ── Expressions ─────────────────────────────────────────────────
    declared_expressions: set[str] = set()
    expressions_list = refs.get("Expressions")
    if isinstance(expressions_list, list):
        for entry in expressions_list:
            if isinstance(entry, dict):
                rel = str(entry.get("File") or "").strip().replace("\\", "/")
                if rel:
                    declared_expressions.add(rel)

    extras: list[dict] = []
    for path in sorted(entry_dir.rglob("*.exp3.json")):
        rel = str(path.relative_to(entry_dir)).replace("\\", "/")
        if rel in declared_expressions:
            continue
        stem = path.stem
        if stem.endswith(".exp3"):
            stem = stem[: -len(".exp3")]
        extras.append({"Name": stem or path.stem, "File": rel})
    if extras:
        merged = list(expressions_list) if isinstance(expressions_list, list) else []
        merged.extend(extras)
        refs["Expressions"] = merged


def _augment_cubism2(entry_dir: Path, data: dict) -> None:
    declared_motions: set[str] = set()
    motions_section = data.get("motions")
    if not isinstance(motions_section, dict):
        motions_section = {}
    if "" in motions_section:
        existing_default = motions_section.pop("")
        if isinstance(existing_default, list):
            current_default = motions_section.get("default")
            if isinstance(current_default, list):
                current_default.extend(existing_default)
            else:
                motions_section["default"] = existing_default
    for entries in motions_section.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                rel = str(entry.get("file") or "").strip().replace("\\", "/")
                if rel:
                    declared_motions.add(rel)

    orphans: list[str] = []
    for p in entry_dir.rglob("*.mtn"):
        rel = str(p.relative_to(entry_dir)).replace("\\", "/")
        if rel not in declared_motions:
            orphans.append(rel)
    orphans.sort()

    if orphans:
        grouped: dict[str, list[dict]] = {}
        for rel in orphans:
            parts = rel.split("/")
            if len(parts) >= 3 and parts[0].lower() == "motions":
                group = parts[1] or "extra"
            else:
                group = "extra"
            grouped.setdefault(group, []).append({"file": rel})
        for group_name, entries in grouped.items():
            existing = motions_section.get(group_name)
            if isinstance(existing, list):
                existing.extend(entries)
            else:
                motions_section[group_name] = entries
        data["motions"] = motions_section
    elif motions_section:
        data["motions"] = motions_section


def _sanitize_basename(name: str) -> str:
    """Replace URL-unsafe characters in a single path segment.

    Only touches characters that have URL syntactic meaning (``#``, ``?``)
    — see :data:`_URL_UNSAFE_CHARS`. Whitespace and unicode are preserved.
    """
    return _URL_UNSAFE_CHARS.sub("_", name)


def _sanitize_extracted_paths(root: Path) -> dict[str, str]:
    """Rename any extracted file/dir whose name contains URL-unsafe chars.

    Returns a ``{old_relative_path: new_relative_path}`` map so callers can
    patch JSON references after renaming. Both keys and values use forward
    slashes regardless of OS.

    Walks bottom-up so files are renamed before their parent directories,
    which avoids invalidating paths mid-walk. Collisions (a sanitized name
    already exists at the same level) are resolved with a numeric suffix.
    """
    rename_map: dict[str, str] = {}
    # Deepest paths first so we rename children before parents.
    entries = sorted(root.rglob("*"), key=lambda p: -len(p.parts))
    for entry in entries:
        if not entry.exists():
            continue
        sanitized = _sanitize_basename(entry.name)
        if sanitized == entry.name:
            continue
        new_path = entry.with_name(sanitized)
        # Resolve collisions with a numeric suffix on the stem.
        if new_path.exists():
            stem, ext = os.path.splitext(sanitized)
            counter = 1
            while True:
                candidate = entry.with_name(f"{stem}_{counter}{ext}")
                if not candidate.exists():
                    new_path = candidate
                    break
                counter += 1
        entry.rename(new_path)
        old_rel = str(entry.relative_to(root)).replace("\\", "/")
        new_rel = str(new_path.relative_to(root)).replace("\\", "/")
        rename_map[old_rel] = new_rel
    return rename_map


def _rewrite_json_references(root: Path, rename_map: dict[str, str]) -> None:
    """Patch every ``.json`` file under ``root`` to use sanitized paths.

    Naive string-replace works because Live2D's JSON formats reference
    files as bare path strings (no escaping for our characters). We
    process the longest paths first so a shorter rename can't accidentally
    substring-match inside a longer one.
    """
    if not rename_map:
        return
    pairs = sorted(rename_map.items(), key=lambda kv: -len(kv[0]))
    # Also rewrite by basename to catch references that omit the
    # directory (some exp3.json / motion3.json files use just the
    # filename even when nested).
    base_pairs: list[tuple[str, str]] = []
    seen_base: set[str] = set()
    for old, new in pairs:
        old_base = old.rsplit("/", 1)[-1]
        new_base = new.rsplit("/", 1)[-1]
        if old_base != new_base and old_base not in seen_base:
            seen_base.add(old_base)
            base_pairs.append((old_base, new_base))
    for json_path in root.rglob("*.json"):
        try:
            text = json_path.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text = text
        for old, new in pairs:
            new_text = new_text.replace(old, new)
        for old, new in base_pairs:
            new_text = new_text.replace(old, new)
        if new_text != text:
            try:
                json_path.write_text(new_text, encoding="utf-8")
            except OSError:
                log.warning("could not rewrite json refs in %s", json_path)


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
        lip_sync_ids, eye_blink_ids = _parse_cubism3_groups(entry_data)
    else:
        expressions, motions = _parse_cubism2(entry_data)
        lip_sync_ids, eye_blink_ids = [], []

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
        lip_sync_ids=lip_sync_ids,
        eye_blink_ids=eye_blink_ids,
        uploaded_at=_now_iso(),
    )


def _parse_cubism3_groups(data: dict) -> tuple[list[str], list[str]]:
    """Extract ``LipSync`` and ``EyeBlink`` parameter IDs from ``Groups``.

    Cubism 3+ models declare lip-sync and eye-blink targets here:

        "Groups": [
          {"Target": "Parameter", "Name": "LipSync",  "Ids": [...]},
          {"Target": "Parameter", "Name": "EyeBlink", "Ids": [...]}
        ]

    The IDs themselves vary across models — modern Cubism 4 uses
    ``ParamMouthOpenY``, while many Cubism-3-ported-from-Cubism-2 models
    keep the legacy ``PARAM_MOUTH_OPEN_Y``. Parsing them out of the
    manifest lets the renderer drive the right parameters without us
    guessing.
    """
    lip_sync: list[str] = []
    eye_blink: list[str] = []
    groups = data.get("Groups")
    if not isinstance(groups, list):
        return lip_sync, eye_blink
    for group in groups:
        if not isinstance(group, dict):
            continue
        if str(group.get("Target") or "").strip() != "Parameter":
            continue
        name = str(group.get("Name") or "").strip()
        ids = group.get("Ids") or []
        if not isinstance(ids, list):
            continue
        cleaned = [str(i).strip() for i in ids if str(i or "").strip()]
        if name == "LipSync":
            lip_sync = cleaned
        elif name == "EyeBlink":
            eye_blink = cleaned
    return lip_sync, eye_blink


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
        lip_sync_ids = [
            str(i) for i in (data.get("lip_sync_ids") or []) if str(i or "")
        ]
        eye_blink_ids = [
            str(i) for i in (data.get("eye_blink_ids") or []) if str(i or "")
        ]
        try:
            scale_multiplier = float(data.get("scale_multiplier") or 1.0)
        except (TypeError, ValueError):
            scale_multiplier = 1.0
        if scale_multiplier != scale_multiplier:  # NaN
            scale_multiplier = 1.0
        scale_multiplier = max(0.3, min(4.0, scale_multiplier))
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
            lip_sync_ids=lip_sync_ids,
            eye_blink_ids=eye_blink_ids,
            scale_multiplier=scale_multiplier,
            uploaded_at=str(data.get("uploaded_at") or ""),
        )
    except (TypeError, ValueError):
        return None
