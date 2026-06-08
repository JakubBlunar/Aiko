"""Per-root file snapshot store for "what's new since last time".

The ``only_new`` mode on :class:`~app.core.tasks.handlers.file_search.FileSearchHandler`
needs a memory of which files it has already seen in a root so a later
scan can report only the *new* or *modified* ones. That memory lives
here: one JSON blob per root label in the ``kv_meta`` table
(``tasks.file_snapshot.<label>``), mapping ``relative_path -> {mtime, size}``.

Design notes:

* **Write-locked.** ``diff_and_update`` does a read-modify-write of the
  per-root blob. A single process-wide :class:`threading.Lock` serialises
  it so two concurrent ``only_new`` scans of the same root (a real case
  once multiple workflows run) can't lose each other's updates. The lock
  is coarse (one lock for all labels) on purpose -- snapshot writes are
  rare and cheap, so contention is a non-issue and the code stays simple.
* **First-run baseline.** The very first scan of a root has no prior
  snapshot. Returning the entire tree as "new" would be useless noise
  ("find new files" dumping every file). Instead the first run records
  the current set as a baseline and reports zero new
  (``baseline_established=True``); only genuinely-new files surface on
  subsequent scans.
* **Best-effort.** A malformed / missing blob is treated as "no prior
  snapshot" (baseline). Persistence failures are swallowed and logged --
  a snapshot write that fails should never fail the search itself.

The store is pure plumbing over the injected ``db`` (anything exposing
``kv_get`` / ``kv_set``); it holds no other state. Designed for
millisecond unit tests with a fake kv-backed db.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


log = logging.getLogger("app.tasks.file_snapshot")


_KEY_PREFIX = "tasks.file_snapshot."


class _KvLike(Protocol):
    """Minimal surface the store needs from :class:`ChatDatabase`."""

    def kv_get(self, key: str) -> str | None: ...

    def kv_set(self, key: str, value: str) -> None: ...


# A single file's recorded fingerprint. ``mtime`` is a float epoch
# second; ``size`` is bytes. Both come straight off ``os.stat``.
FileFingerprint = Mapping[str, float]
# label-scoped current set: ``relative_path -> {"mtime": ..., "size": ...}``
SnapshotMap = dict[str, dict[str, float]]


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    """Result of diffing a current scan against the stored snapshot.

    ``new`` / ``modified`` are sorted lists of relative paths.
    ``baseline_established`` is True only on the first-ever scan of a
    root (no prior snapshot existed) -- callers should treat that as
    "nothing new yet, baseline recorded" rather than "everything is new".
    """

    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    baseline_established: bool = False

    @property
    def changed(self) -> set[str]:
        """Union of new + modified relative paths."""
        return set(self.new) | set(self.modified)

    @property
    def is_empty(self) -> bool:
        return not self.new and not self.modified


class FileSnapshotStore:
    """kv-backed, write-locked per-root seen-file index."""

    def __init__(self, db: _KvLike) -> None:
        self._db = db
        self._lock = threading.Lock()

    # ── key helpers ──────────────────────────────────────────────────

    @staticmethod
    def _key(label: str) -> str:
        return f"{_KEY_PREFIX}{label}"

    # ── load / persist ───────────────────────────────────────────────

    def load(self, label: str) -> SnapshotMap | None:
        """Return the stored snapshot for ``label``, or ``None`` if none.

        ``None`` means "no baseline yet". A malformed blob is treated
        the same way (and logged) so a corrupt row self-heals on the
        next ``update``.
        """
        try:
            raw = self._db.kv_get(self._key(label))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("file_snapshot load failed: label=%s err=%s", label, exc)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("file_snapshot corrupt blob: label=%s (treating as empty)", label)
            return None
        if not isinstance(data, dict):
            return None
        out: SnapshotMap = {}
        for rel, fp in data.items():
            if not isinstance(rel, str) or not isinstance(fp, dict):
                continue
            try:
                out[rel] = {
                    "mtime": float(fp.get("mtime", 0.0) or 0.0),
                    "size": float(fp.get("size", -1) if fp.get("size") is not None else -1),
                }
            except (TypeError, ValueError):
                continue
        return out

    def _persist(self, label: str, current: SnapshotMap) -> None:
        try:
            self._db.kv_set(self._key(label), json.dumps(current, separators=(",", ":")))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("file_snapshot persist failed: label=%s err=%s", label, exc)

    # ── diff (pure) ──────────────────────────────────────────────────

    @staticmethod
    def diff(prior: SnapshotMap | None, current: Mapping[str, FileFingerprint]) -> SnapshotDiff:
        """Compute new/modified between ``prior`` and ``current``.

        Pure function -- no I/O, no lock. ``prior is None`` means no
        baseline existed: returns ``baseline_established=True`` with
        empty new/modified.
        """
        if prior is None:
            return SnapshotDiff(new=[], modified=[], baseline_established=True)
        new: list[str] = []
        modified: list[str] = []
        for rel, fp in current.items():
            before = prior.get(rel)
            if before is None:
                new.append(rel)
                continue
            cur_mtime = float(fp.get("mtime", 0.0) or 0.0)
            cur_size = float(fp.get("size", -1) if fp.get("size") is not None else -1)
            if cur_mtime != float(before.get("mtime", 0.0) or 0.0) or cur_size != float(
                before.get("size", -1) if before.get("size") is not None else -1
            ):
                modified.append(rel)
        return SnapshotDiff(
            new=sorted(new), modified=sorted(modified), baseline_established=False
        )

    # ── public atomic op ─────────────────────────────────────────────

    def diff_and_update(
        self, label: str, current: Mapping[str, FileFingerprint]
    ) -> SnapshotDiff:
        """Atomically diff ``current`` against the stored snapshot, then
        persist ``current`` as the new snapshot.

        Serialised by the store lock so concurrent scans of the same
        root are safe. On the first scan (no prior) records the baseline
        and reports zero new.
        """
        normalised: SnapshotMap = {
            str(rel): {
                "mtime": float(fp.get("mtime", 0.0) or 0.0),
                "size": float(fp.get("size", -1) if fp.get("size") is not None else -1),
            }
            for rel, fp in current.items()
        }
        with self._lock:
            prior = self.load(label)
            result = self.diff(prior, normalised)
            self._persist(label, normalised)
        return result

    def update(self, label: str, current: Mapping[str, FileFingerprint]) -> None:
        """Persist ``current`` as the snapshot without diffing (testing)."""
        normalised: SnapshotMap = {
            str(rel): {
                "mtime": float(fp.get("mtime", 0.0) or 0.0),
                "size": float(fp.get("size", -1) if fp.get("size") is not None else -1),
            }
            for rel, fp in current.items()
        }
        with self._lock:
            self._persist(label, normalised)

    def reset(self, label: str) -> None:
        """Drop the snapshot for ``label`` (next scan re-baselines)."""
        with self._lock:
            try:
                self._db.kv_set(self._key(label), "")
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("file_snapshot reset failed: label=%s err=%s", label, exc)


__all__ = ["FileSnapshotStore", "SnapshotDiff", "SnapshotMap", "FileFingerprint"]
