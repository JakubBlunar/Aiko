"""L45 gate tuning persistence -- the two files and the apply decision.

Learned thresholds live under ``data/`` with the rest of the derived state,
never in ``config/``. That follows the existing separation: ``config/`` is
what the *user* meant, ``data/`` is what the app worked out. Two files, with
different jobs:

- ``data/tuning/concept_gates.json`` -- one entry per gate: the solved value,
  the statistics that justify it, which rail clamped it, and a short history.
  This is the file to open when a threshold looks wrong.
- ``data/tuning/concept_population.jsonl`` -- one line per tuner run
  describing the concept graph itself. No gate proposes anything from it; it
  exists because every retune so far had to start by measuring the graph from
  scratch, and a rolling record turns that into a trend read. Later phases
  (the cosine bars, the per-kind floors) are designed against this file.

**Resolution order is default -> tuned -> user.** ``config/user.json`` always
wins. There is no provenance on a parsed ``AppSettings`` -- once
:func:`~app.core.infra.settings.load_settings` has merged and clamped
everything, a value carries no memory of where it came from -- so
:func:`apply_gates` reads ``user.json`` directly through
:func:`~app.core.infra.settings.read_user_overrides` and skips any key it
finds there.

**Nothing in the background ever writes ``config/user.json``.** The tuner may
propose that a hand-set value be handed over, and records the drift so the
proposal is legible, but the handoff itself is :func:`adopt_gate` -- an
explicit, human-invoked command. A worker quietly rewriting the user's config
would be the kind of surprise that costs trust in the whole feature, and the
payoff does not come close to justifying it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.core.concepts.gate_tuning import (
    GATE_SPECS,
    GateSolution,
    GateSpec,
    MODE_APPLY,
    kind_floor_defaults,
)
from app.core.infra.settings import (
    USER_CONFIG_PATH,
    persist_user_overrides,
    read_user_overrides,
)

log = logging.getLogger("app.gate_tuning")

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

#: Schema version of ``concept_gates.json``. Bump when an entry's shape
#: changes; a mismatched file is discarded rather than migrated, since every
#: value in it is re-derivable on the next run.
GATES_VERSION = 1

#: Per-gate history depth. Deep enough to see a walk, shallow enough to read
#: at a glance -- and oscillation is the failure mode this feature can
#: introduce, so it needs to be the easiest thing to spot.
HISTORY_CAP = 12

#: Snapshot lines retained. At roughly one line a day this is about a year,
#: which is longer than any trend we would reason about.
POPULATION_CAP = 400

_lock = threading.Lock()


def tuning_dir() -> Path:
    """Where the two files live. ``AIKO_TUNING_DIR`` relocates them.

    Mirrors ``AIKO_USER_CONFIG``: the container points this into the data
    volume so learned thresholds survive recreating the image, rather than
    being discarded with the writable layer.
    """
    override = (os.environ.get("AIKO_TUNING_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_DATA_DIR / "tuning"


def gates_path() -> Path:
    return tuning_dir() / "concept_gates.json"


def population_path() -> Path:
    return tuning_dir() / "concept_population.jsonl"


# ── reading ───────────────────────────────────────────────────────────


def empty_document() -> dict[str, Any]:
    return {"version": GATES_VERSION, "updated_at": None, "gates": {}}


def load_gates(*, path: Path | None = None) -> dict[str, Any]:
    """Read ``concept_gates.json``; a missing or unusable file reads empty.

    Never raises. Every value in here is re-derived on the next tuner run, so
    a corrupt file is worth exactly one warning and no recovery machinery.
    """
    target = path or gates_path()
    try:
        if not target.is_file():
            return empty_document()
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        log.warning("gate tuning file unreadable; ignoring it", exc_info=True)
        return empty_document()
    if not isinstance(raw, dict) or int(raw.get("version", 0)) != GATES_VERSION:
        return empty_document()
    gates = raw.get("gates")
    if not isinstance(gates, dict):
        raw["gates"] = {}
    return raw


def tuned_values(document: Mapping[str, Any]) -> dict[str, float]:
    """The applicable values from a loaded document, gate name -> value.

    Only gates the document itself marked ``applied`` are returned, so a
    value recorded while its gate was in observe mode never leaks into
    settings just because a later boot read the file.
    """
    out: dict[str, float] = {}
    for name, entry in (document.get("gates") or {}).items():
        if not isinstance(entry, dict) or not entry.get("applied"):
            continue
        try:
            out[str(name)] = float(entry["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ── the user-override seam ────────────────────────────────────────────


def user_memory_overrides(*, config_path: Path | None = None) -> dict[str, Any]:
    """The ``memory`` block of ``user.json``, or empty.

    This is the only way to tell an explicit user choice from a code default:
    the parsed dataclass has no provenance, so presence in this dict *is* the
    provenance.
    """
    overrides = read_user_overrides(path=config_path)
    block = overrides.get("memory") if isinstance(overrides, dict) else None
    return dict(block) if isinstance(block, dict) else {}


# ── writing ───────────────────────────────────────────────────────────


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


def build_document(
    solutions: Mapping[str, GateSolution],
    *,
    now: datetime,
    specs: Iterable[GateSpec] = GATE_SPECS,
    previous: Mapping[str, Any] | None = None,
    user_overrides: Mapping[str, Any] | None = None,
    population: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold this run's solutions into a new document.

    ``applied`` is deliberately separate from ``mode``, because a gate can go
    unapplied for four different reasons -- observe mode, not a settings field
    at all, a user override, or a warmup that has not cleared -- and the one
    that applies is what you need to know when you come back to the file in a
    month. ``unapplied_because`` records it in words.
    """
    previous_gates = dict((previous or {}).get("gates") or {})
    overrides = dict(user_overrides or {})
    by_name = {spec.setting: spec for spec in specs}
    kind_defaults = kind_floor_defaults()

    gates: dict[str, Any] = {}
    for name, solution in solutions.items():
        spec = by_name.get(name)
        if spec is None:
            continue
        prior = previous_gates.get(name) or {}
        overridden = spec.is_setting_field and name in overrides

        reason: str | None = None
        if spec.mode != MODE_APPLY:
            reason = "observe mode"
        elif not spec.is_setting_field:
            reason = "not a settings field yet"
        elif overridden:
            reason = "set explicitly in config/user.json"
        elif solution.clamped_by == "warmup":
            reason = solution.reason
        applied = reason is None

        entry: dict[str, Any] = {
            "value": solution.proposed,
            "mode": spec.mode,
            "applied": applied,
            "objective": spec.objective,
            "population": spec.population,
            "why": spec.why,
            "raw": solution.raw,
            "clamped_by": solution.clamped_by,
            "stats": solution.stats,
            "updated_at": now.isoformat(),
        }
        if reason is not None:
            entry["unapplied_because"] = reason
        if name in kind_defaults:
            entry["code_default"] = kind_defaults[name]
        if overridden:
            # Drift against a hand-set value is useful even when the gate is
            # never handed over: "you set 0.7, six weeks of data says 0.62".
            try:
                anchor = float(overrides[name])
            except (TypeError, ValueError):
                anchor = None
            if anchor is not None:
                entry["user_value"] = anchor
                entry["drift_from_user"] = round(
                    solution.proposed - anchor, 4
                )
        for carried in ("seeded_from", "seeded_at", "seed_value"):
            if carried in prior:
                entry[carried] = prior[carried]

        history = [
            row for row in (prior.get("history") or []) if isinstance(row, dict)
        ]
        last = history[-1].get("value") if history else None
        if last is None or abs(float(last) - solution.proposed) > 1e-9:
            history.append(
                {"at": now.isoformat(), "value": solution.proposed}
            )
        entry["history"] = history[-HISTORY_CAP:]
        gates[name] = entry

    document: dict[str, Any] = {
        "version": GATES_VERSION,
        "updated_at": now.isoformat(),
        "gates": gates,
    }
    if population:
        document["population"] = dict(population)
    return document


def save_gates(
    document: Mapping[str, Any], *, path: Path | None = None,
) -> None:
    target = path or gates_path()
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    with _lock:
        try:
            _atomic_write(target, text)
        except Exception:
            log.warning("could not write %s", target, exc_info=True)


def append_population(
    row: Mapping[str, Any],
    *,
    path: Path | None = None,
    cap: int = POPULATION_CAP,
) -> None:
    """Append one snapshot line, trimming the file to ``cap`` lines.

    Rewritten rather than rotated: at one line a day the whole file is a few
    hundred kilobytes, and a single file is easier to read back than a
    rotation set when the point is a trend over months.
    """
    target = path or population_path()
    line = json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"))
    with _lock:
        try:
            existing: list[str] = []
            if target.is_file():
                existing = [
                    text
                    for text in target.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if text.strip()
                ]
            existing.append(line)
            keep = existing[-max(1, int(cap)):]
            _atomic_write(target, "\n".join(keep) + "\n")
        except Exception:
            log.warning("could not append to %s", target, exc_info=True)


def load_population(
    *, path: Path | None = None, limit: int = 0,
) -> list[dict[str, Any]]:
    """Read snapshot lines oldest-first; junk lines are skipped."""
    target = path or population_path()
    try:
        if not target.is_file():
            return []
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for text in lines:
        text = text.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    if limit and limit > 0:
        return rows[-limit:]
    return rows


# ── applying ──────────────────────────────────────────────────────────


def apply_gates(
    memory_settings: Any,
    document: Mapping[str, Any] | None = None,
    *,
    specs: Iterable[GateSpec] = GATE_SPECS,
    config_path: Path | None = None,
) -> dict[str, float]:
    """Write applicable tuned values onto a live ``MemorySettings``.

    Returns what was actually set. Mutating the settings object in place is
    safe because every gate in the registry is read *per run* rather than
    snapshotted at construction -- the lifecycle worker's ``_f`` helper, the
    prompt assembler's ``getattr(ms, ...)`` reads, the synthesis worker's
    property accessors. That is also why a tuner run takes effect without a
    restart. A gate read once at construction would need a refresh hook
    before it could join the registry.

    Three conditions must hold before a value lands, each checked
    independently so a future edit cannot collapse them by accident: the
    gate's spec must be writable (apply mode *and* a real settings field), the
    document must have marked the entry applied, and the key must be absent
    from ``config/user.json``.
    """
    if memory_settings is None:
        return {}
    doc = document if document is not None else load_gates()
    values = tuned_values(doc)
    if not values:
        return {}
    writable = {spec.setting for spec in specs if spec.writable}
    overrides = user_memory_overrides(config_path=config_path)

    applied: dict[str, float] = {}
    for name, value in values.items():
        if name not in writable or name in overrides:
            continue
        if not hasattr(memory_settings, name):
            continue
        try:
            setattr(memory_settings, name, float(value))
        except Exception:
            log.debug("could not apply tuned %s", name, exc_info=True)
            continue
        applied[name] = float(value)
    if applied:
        log.info("gate tuning applied: %s", applied)
    return applied


# ── the seeded handoff ────────────────────────────────────────────────


def adopt_gate(
    setting: str,
    *,
    current_value: float,
    now: datetime | None = None,
    path: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Hand a hand-set threshold over to the tuner, seeded from its value.

    Two steps, in this order: record ``current_value`` in the tuning file as
    the gate's seed, then remove the key from the ``memory`` block of
    ``user.json``. Seeding first means behaviour does not jump at the moment
    of handoff -- the tuner's step clamp walks the value from where the user
    left it rather than from a code default.

    This is the *only* function in the module that touches ``user.json``, and
    nothing in the background calls it.
    """
    from app.core.concepts.gate_tuning import spec_for

    spec = spec_for(setting)
    if spec is None:
        return {"ok": False, "error": f"{setting} is not a tunable gate"}
    if not spec.is_setting_field:
        return {
            "ok": False,
            "error": f"{setting} is not a settings field, nothing to adopt",
        }
    overrides = user_memory_overrides(config_path=config_path)
    if setting not in overrides:
        return {
            "ok": False,
            "error": f"{setting} is not set in config/user.json",
        }

    when = now or datetime.now(timezone.utc)
    document = load_gates(path=path)
    gates = dict(document.get("gates") or {})
    entry = dict(gates.get(setting) or {})
    entry.update({
        "value": float(current_value),
        "mode": spec.mode,
        "applied": spec.writable,
        "seeded_from": "config/user.json",
        "seeded_at": when.isoformat(),
        "seed_value": float(current_value),
        "updated_at": when.isoformat(),
    })
    entry.pop("user_value", None)
    entry.pop("drift_from_user", None)
    history = [
        row for row in (entry.get("history") or []) if isinstance(row, dict)
    ]
    history.append({"at": when.isoformat(), "value": float(current_value)})
    entry["history"] = history[-HISTORY_CAP:]
    gates[setting] = entry
    document["gates"] = gates
    document["updated_at"] = when.isoformat()
    save_gates(document, path=path)

    removed = _remove_memory_override(setting, config_path=config_path)
    return {
        "ok": True,
        "setting": setting,
        "seeded_value": float(current_value),
        "removed_from_user_json": removed,
        "mode": spec.mode,
    }


def _remove_memory_override(
    setting: str, *, config_path: Path | None = None,
) -> bool:
    """Delete one key from the ``memory`` block of ``user.json``.

    ``prune_user_override_keys`` only reaches top-level blocks, and dropping
    the whole ``memory`` block would take a dozen deliberate settings with it.
    """
    target = config_path or USER_CONFIG_PATH
    overrides = read_user_overrides(path=target)
    block = overrides.get("memory") if isinstance(overrides, dict) else None
    if not isinstance(block, dict) or setting not in block:
        return False
    remaining = {k: v for k, v in block.items() if k != setting}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return False
        raw["memory"] = remaining
        _atomic_write(
            target, json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
        )
    except Exception:
        log.warning("could not remove %s from user.json", setting, exc_info=True)
        return False
    # persist_user_overrides owns the read cache for this path; a no-op merge
    # is the cheapest way to invalidate it without reaching into privates.
    try:
        persist_user_overrides({"memory": remaining}, path=target)
    except Exception:
        pass
    return True


__all__ = [
    "GATES_VERSION",
    "HISTORY_CAP",
    "POPULATION_CAP",
    "adopt_gate",
    "append_population",
    "apply_gates",
    "build_document",
    "empty_document",
    "gates_path",
    "load_gates",
    "load_population",
    "population_path",
    "save_gates",
    "tuned_values",
    "tuning_dir",
    "user_memory_overrides",
]
