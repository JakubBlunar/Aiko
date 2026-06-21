"""K10 persona regression — pure golden-turn scoring.

This module is the deterministic, I/O-light core of the persona
regression harness. It knows nothing about LLM clients, the prompt
assembler, or the SessionController; it only:

  - models a *golden turn* (a canonical user line plus the style
    markers a healthy persona reply must satisfy),
  - loads a JSONL fixture of golden turns,
  - scores a single reply string against one golden turn, and
  - aggregates a run's results into a snapshot dict for kv_meta / REST.

The orchestration (building the prompt, calling the worker LLM,
persisting + broadcasting the snapshot) lives in
``app/core/session/persona_regression_mixin.py``. Keeping the scoring
pure makes it cheap to unit-test the brittle part (marker matching)
without standing up a model.

Marker semantics (all case-insensitive substring matches):

  - ``require_any``  — at least ONE must appear (advisory tone words).
  - ``require_all``  — every entry must appear.
  - ``require_tags`` — literal self-tag substrings (e.g. ``[[reaction:``)
    that the reply must contain. The strongest *positive* signal.
  - ``forbid``       — corporate-tell phrases that must NOT appear
    ("as an ai", "i cannot", ...). The strongest *drift* signal.

A turn passes when every ``require_*`` is satisfied and no ``forbid``
entry matches.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("app.persona_regression")

# Default scope when a fixture line omits it. ``minimal`` isolates
# persona-sheet drift; ``full`` runs the live prompt (memory + RAG).
SCOPE_MINIMAL = "minimal"
SCOPE_FULL = "full"
_VALID_SCOPES = (SCOPE_MINIMAL, SCOPE_FULL)

# Reply preview length stored in the snapshot (keeps kv_meta small and
# avoids dumping a whole reply into the diagnostics panel).
_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class GoldenTurn:
    """One canonical prompt + the style markers a healthy reply meets."""

    id: str
    user: str
    scope: str = SCOPE_MINIMAL
    require_any: tuple[str, ...] = ()
    require_all: tuple[str, ...] = ()
    require_tags: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class GoldenResult:
    """Outcome of scoring one reply against one :class:`GoldenTurn`."""

    id: str
    scope: str
    passed: bool
    failures: tuple[str, ...] = ()
    reply_preview: str = ""


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a JSON value into a tuple of non-empty trimmed strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return ()
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            out.append(text)
    return tuple(out)


def parse_golden_turn(raw: dict[str, Any]) -> GoldenTurn | None:
    """Build a :class:`GoldenTurn` from one decoded JSON object.

    Returns ``None`` (caller skips + logs) when the line lacks the two
    required fields (``id`` + ``user``). An unknown ``scope`` falls back
    to ``minimal`` rather than rejecting the line.
    """
    turn_id = str(raw.get("id") or "").strip()
    user = str(raw.get("user") or "").strip()
    if not turn_id or not user:
        return None
    scope = str(raw.get("scope") or SCOPE_MINIMAL).strip().lower()
    if scope not in _VALID_SCOPES:
        scope = SCOPE_MINIMAL
    return GoldenTurn(
        id=turn_id,
        user=user,
        scope=scope,
        require_any=_as_str_tuple(raw.get("require_any")),
        require_all=_as_str_tuple(raw.get("require_all")),
        require_tags=_as_str_tuple(raw.get("require_tags")),
        forbid=_as_str_tuple(raw.get("forbid")),
        notes=str(raw.get("notes") or "").strip(),
    )


def load_golden_turns(path: str | Path) -> list[GoldenTurn]:
    """Load + parse a JSONL fixture of golden turns.

    Blank lines and lines starting with ``#`` are ignored (so the
    fixture can carry comments). Malformed JSON or objects missing the
    required fields are skipped with a WARNING and do not abort the
    load. Returns ``[]`` when the file is missing.
    """
    fixture_path = Path(path)
    if not fixture_path.exists():
        log.warning("persona-regression fixture missing: %s", fixture_path)
        return []
    turns: list[GoldenTurn] = []
    seen_ids: set[str] = set()
    try:
        text = fixture_path.read_text(encoding="utf-8")
    except OSError:
        log.warning(
            "persona-regression fixture unreadable: %s", fixture_path,
            exc_info=True,
        )
        return []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            log.warning(
                "persona-regression: skipping malformed line %d in %s",
                lineno, fixture_path,
            )
            continue
        if not isinstance(raw, dict):
            log.warning(
                "persona-regression: skipping non-object line %d in %s",
                lineno, fixture_path,
            )
            continue
        turn = parse_golden_turn(raw)
        if turn is None:
            log.warning(
                "persona-regression: skipping line %d (missing id/user) in %s",
                lineno, fixture_path,
            )
            continue
        if turn.id in seen_ids:
            log.warning(
                "persona-regression: duplicate id %r on line %d in %s",
                turn.id, lineno, fixture_path,
            )
            continue
        seen_ids.add(turn.id)
        turns.append(turn)
    return turns


def score_reply(reply: str, turn: GoldenTurn) -> GoldenResult:
    """Score one reply against one golden turn (case-insensitive).

    Records every individual miss in ``failures`` so the diagnostics
    panel can show exactly why a turn regressed.
    """
    text = str(reply or "")
    low = text.lower()
    failures: list[str] = []

    if turn.require_any:
        if not any(marker.lower() in low for marker in turn.require_any):
            failures.append(
                "missing require_any: one of "
                + ", ".join(repr(m) for m in turn.require_any),
            )

    for marker in turn.require_all:
        if marker.lower() not in low:
            failures.append(f"missing require_all: {marker!r}")

    for tag in turn.require_tags:
        if tag.lower() not in low:
            failures.append(f"missing tag: {tag!r}")

    for phrase in turn.forbid:
        if phrase.lower() in low:
            failures.append(f"forbidden: {phrase!r}")

    preview = text.strip().replace("\n", " ")
    if len(preview) > _PREVIEW_CHARS:
        preview = preview[: _PREVIEW_CHARS - 1].rstrip() + "\u2026"

    return GoldenResult(
        id=turn.id,
        scope=turn.scope,
        passed=not failures,
        failures=tuple(failures),
        reply_preview=preview,
    )


def result_to_dict(result: GoldenResult) -> dict[str, Any]:
    """Serialize a :class:`GoldenResult` for the snapshot JSON."""
    return {
        "id": result.id,
        "scope": result.scope,
        "passed": result.passed,
        "failures": list(result.failures),
        "reply_preview": result.reply_preview,
    }


def build_snapshot(
    results: list[GoldenResult],
    *,
    model: str = "",
    ran_ms: float = 0.0,
    ran_at: datetime | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Aggregate a run's results into the kv_meta / REST snapshot dict."""
    when = (ran_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    snapshot: dict[str, Any] = {
        "ran_at": when.isoformat(),
        "model": str(model or ""),
        "ran_ms": round(float(ran_ms), 1),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": [result_to_dict(r) for r in results],
    }
    if error:
        snapshot["error"] = str(error)
    return snapshot


__all__ = [
    "SCOPE_FULL",
    "SCOPE_MINIMAL",
    "GoldenResult",
    "GoldenTurn",
    "build_snapshot",
    "load_golden_turns",
    "parse_golden_turn",
    "result_to_dict",
    "score_reply",
]
