"""Bounded, source-cited communication-style applicability (L50 pilot)."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from app.core.infra import timephrase


SCOPE_PREFIX = "concept.applicability:"
_CONTEXTS = {
    "learning": re.compile(r"\b(learning|learn|studying|study|tutorial)\b", re.I),
    "troubleshooting": re.compile(
        r"\b(troubleshoot(?:ing)?|debug(?:ging)?|diagnos(?:e|ing))\b",
        re.I,
    ),
    "casual": re.compile(
        r"\b(casual|small[ -]talk|chit[ -]?chat|just chatt?ing|just talk(?:ing)?"
        r"|catching up|hanging out|everyday (?:conversation|discussion)s?"
        r"|(?:relaxed|light.?hearted) (?:chat|conversation|discussion)s?)\b",
        re.I,
    ),
}
_NONASSERTION = re.compile(
    r"\b(role.?play(?:ing)?|hypothetical(?:ly)?|suppos(?:e|ing)|imagin(?:e|ing)"
    r"|pretend(?:ing)?|quoted|quotation|fictional)\b|[\"\u2018\u201c\u201d]"
    r"|(?:^|\s)'[^\n]{4,320}'(?:\s|[.,!?]|$)",
    re.I,
)
_CONDITION = re.compile(r"\b(when|while|during|unless|except|for)\b", re.I)


def context_names(text: str) -> set[str]:
    if _NONASSERTION.search(text or ""):
        return set()
    return {name for name, pattern in _CONTEXTS.items() if pattern.search(text or "")}


def asserted_style_source(memory: Any) -> bool:
    return not bool(_NONASSERTION.search(str(getattr(memory, "content", "") or "")))


def parse_applicability(value: Any, memories: dict[int, Any]) -> dict[str, Any] | None:
    """Accept exact qualifier spans in offered memories, never invented dates/IDs."""
    if not isinstance(value, dict):
        return None

    def citation(item: Any) -> tuple[Any, str]:
        if not isinstance(item, dict) or type(item.get("memory_id")) is not int:
            raise ValueError("invalid citation")
        memory = memories.get(item["memory_id"])
        quote = item.get("quote")
        content = str(getattr(memory, "content", "") or "")
        if (
            memory is None
            or not isinstance(quote, str)
            or not 4 <= len(quote) <= 160
            or quote not in content
            or _NONASSERTION.search(content)
        ):
            raise ValueError("unsupported qualifier")
        return memory, quote

    try:
        result: dict[str, Any] = {"version": 1}
        for field in ("contexts", "exceptions"):
            items = value.get(field, [])
            if not isinstance(items, list) or len(items) > 3:
                return None
            qualifiers = []
            for item in items:
                memory, quote = citation(item)
                name = item.get("name")
                if name not in context_names(quote) or not _CONDITION.search(quote):
                    return None
                content = str(memory.content)
                prefix = content[: content.find(quote)].rstrip()
                negative = bool(
                    re.search(r"\b(unless|except|not|never)\b", quote, re.I)
                    or re.search(r"\b(unless|except|not|never)\s*$", prefix, re.I)
                )
                if negative != (field == "exceptions"):
                    return None
                qualifiers.append(
                    {
                        "name": name,
                        "memory_id": item["memory_id"],
                        "quote": quote,
                        "observed_at": str(getattr(memory, "event_time", None) or ""),
                        "source_recorded_at": str(getattr(memory, "created_at", "") or ""),
                    }
                )
            result[field] = qualifiers
        if not result["contexts"]:
            return None
        validity = value.get("validity")
        if validity is not None:
            memory, quote = citation(validity)
            dates = {}
            for field in ("from", "until"):
                stamp = validity.get(field)
                if stamp is not None:
                    parsed = date.fromisoformat(stamp).isoformat()
                    prefix = r"\b(?:from|since)\s+" if field == "from" else r"\b(?:until|before)\s+"
                    if not re.search(prefix + re.escape(parsed), quote, re.I):
                        return None
                    dates[field] = parsed
            if not dates or dates.get("from", "") >= dates.get("until", "9999-12-31"):
                return None
            result["validity"] = {
                **dates,
                "memory_id": validity["memory_id"],
                "quote": quote,
                "observed_at": str(getattr(memory, "event_time", None) or ""),
                "source_recorded_at": str(getattr(memory, "created_at", "") or ""),
            }
        return result
    except (TypeError, ValueError, AttributeError):
        return None


def scope_key(scope: Any) -> tuple:
    if not isinstance(scope, dict) or scope.get("version") != 1:
        return ()
    try:
        fields = [scope.get("contexts"), scope.get("exceptions", [])]
        if not fields[0] or any(not isinstance(items, list) or len(items) > 3 for items in fields):
            return ()
        if any(
            not isinstance(item, dict) or item.get("name") not in _CONTEXTS
            for items in fields
            for item in items
        ):
            return ()
        validity = scope.get("validity", {})
        if not isinstance(validity, dict):
            return ()
        for field in ("from", "until"):
            if field in validity:
                date.fromisoformat(validity[field])
        return (
            tuple(sorted({item["name"] for item in fields[0]})),
            tuple(sorted({item["name"] for item in fields[1]})),
            validity.get("from"),
            validity.get("until"),
        )
    except (TypeError, ValueError, KeyError):
        return ()


class ApplicabilityLedger:
    def __init__(self, db: Any) -> None:
        self._db = db

    def get(self, concept_id: int) -> dict[str, Any] | None:
        try:
            raw = self._db.kv_get(SCOPE_PREFIX + str(int(concept_id)))
            scope = json.loads(raw) if raw else None
            if not scope_key(scope):
                return None
            return scope
        except (TypeError, ValueError, KeyError, AttributeError):
            return None

    def record(self, concept_id: int, scope: dict[str, Any]) -> None:
        if self.get(concept_id) is None:
            self._db.kv_set(SCOPE_PREFIX + str(int(concept_id)), json.dumps(scope))

    def supersede(self, old_id: int, new_id: int, citation: dict[str, Any]) -> None:
        scope = self.get(old_id)
        if scope is not None and not scope.get("superseded_by"):
            successor = self.get(new_id) or {}
            scope.update(
                superseded_by=int(new_id),
                supersession=citation,
                superseded_from=successor.get("validity", {}).get("from"),
                superseded_at=timephrase.utcnow().isoformat(),
            )
            self._db.kv_set(SCOPE_PREFIX + str(int(old_id)), json.dumps(scope))


def current_contexts(user_text: str, snapshot: Any = None) -> set[str]:
    if re.search(
        r"\b(?:not|stop|done|finished|no longer)\b.*"
        r"\b(?:learning|debugging|troubleshooting)\b",
        user_text or "",
        re.I,
    ):
        return set()
    direct = context_names(user_text)
    if "casual" in direct and re.search(
        r"\b(?:not|stop|done|finished|no longer)\b", user_text or "", re.I,
    ):
        return set()
    if direct or _NONASSERTION.search(user_text or ""):
        return direct
    state = getattr(snapshot, "inferred", None)
    if (
        state is None
        or getattr(state, "status", "") != "active"
        or getattr(snapshot, "inferred_stale", True)
        or not getattr(snapshot, "world_compatible", False)
    ):
        return set()
    return context_names(str(getattr(state, "shared_activity", "") or ""))


def applicability_state(scope: Any, contexts: set[str]) -> str:
    if not scope_key(scope):
        return "unknown"
    today = timephrase.now().date().isoformat()
    if scope.get("superseded_by") and (scope.get("superseded_from") or "") <= today:
        return "superseded"
    validity = scope.get("validity", {})
    if validity.get("from", "") > today or validity.get("until", "9999-12-31") <= today:
        return "outside_validity"
    if len(contexts) != 1:
        return "unknown_context"
    applies = {item["name"] for item in scope.get("contexts", [])}
    excludes = {item["name"] for item in scope.get("exceptions", [])}
    if contexts & excludes:
        return "exception"
    return "matched" if contexts <= applies else "other_context"


def scope_hint(scope: Any) -> str:
    if not scope_key(scope):
        return "applicability unverified; do not apply as a rule"
    names, exceptions, start, end = scope_key(scope)
    hint = "only in " + "/".join(names)
    if exceptions:
        hint += "; except " + "/".join(exceptions)
    if start:
        hint += "; from " + start
    if end:
        hint += "; before " + end
    return hint
