"""Source meaning shared by memory writers and concept evidence admission.

Legacy kinds describe intended roles, not proof of truth. Reading an origin
never rewrites a personal record or turns a missing source into testimony.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import SimpleNamespace


@dataclass(frozen=True)
class MemoryOrigin:
    subject: str
    mode: str
    writer: str
    domain: str = "conversation"
    version: int = 1


_SUBJECTS = {"user", "aiko", "relationship", "world", "unknown"}
_MODES = {"observation", "declaration", "reflection", "intention", "hypothetical",
          "question", "summary", "world_fact", "virtual_event", "interpretation", "unknown"}


def memory_origin(memory) -> MemoryOrigin:
    meta = getattr(memory, "metadata", None) or {}
    meta = meta if isinstance(meta, dict) else {}
    explicit = meta.get("memory_origin")
    if explicit is not None:
        if (isinstance(explicit, dict) and explicit.get("version") == 1
                and isinstance(explicit.get("subject"), str)
                and isinstance(explicit.get("mode"), str)
                and explicit.get("subject") in _SUBJECTS and explicit.get("mode") in _MODES
                and isinstance(explicit.get("writer"), str)
                and isinstance(explicit.get("domain", "conversation"), str)
                and explicit.get("domain", "conversation")
                in {"conversation", "virtual", "external"}):
            if (getattr(memory, "kind", "") == "callback"
                    and explicit.get("writer") == "legacy_kind:callback"
                    and isinstance(meta.get("turn_outcome"), dict)
                    and not (isinstance(meta.get("admission"), dict)
                             and meta["admission"].get("subject") == "user")):
                return MemoryOrigin("aiko", "intention", "reflection_worker")
            return MemoryOrigin(**{key: explicit[key] for key in
                                  ("subject", "mode", "writer", "domain", "version")
                                  if key in explicit})
        return MemoryOrigin("unknown", "unknown", "invalid_contract")
    kind = str(getattr(memory, "kind", "") or "")
    admission = meta.get("admission") or {}
    admission = admission if isinstance(admission, dict) else {}
    writer = str(admission.get("writer") or meta.get("source") or "legacy_kind:" + kind)
    if kind in {"knowledge", "curiosity_finding"} or meta.get("source") == "hypothesis":
        return MemoryOrigin("world", "world_fact", writer, "external")
    if kind == "pre_thought":
        return MemoryOrigin("unknown", "hypothetical", writer)
    if kind in {"open_question", "knowledge_gap"}:
        return MemoryOrigin("aiko", "question", writer)
    if kind == "topic_digest":
        return MemoryOrigin("unknown", "summary", writer)
    if kind == "promise":
        who = meta.get("promise_who")
        if not isinstance(who, str) or who not in {"assistant", "user"}:
            who = ("assistant" if str(getattr(memory, "content", "")).startswith("Aiko promised:")
                   else "unknown")
        return MemoryOrigin("aiko" if who == "assistant" else who, "intention", writer)
    if kind == "goal":
        return MemoryOrigin("aiko", "intention", writer)
    if kind == "pursuit_note":
        return MemoryOrigin("aiko", "virtual_event", writer, "virtual")
    if kind in {"diary", "reflection", "goal_progress"}:
        return MemoryOrigin("aiko", "reflection", writer)
    if admission.get("subject") == "user":
        mode = ("intention" if getattr(memory, "temporal_type", None) == "future_plan"
                else "observation")
        return MemoryOrigin("user", mode, writer)
    if kind == "self" or admission.get("subject") == "assistant":
        mode = ("intention" if getattr(memory, "temporal_type", None) == "future_plan"
                else "declaration")
        return MemoryOrigin("aiko", mode, writer)
    if kind == "shared_moment":
        return MemoryOrigin("relationship", "observation", writer)
    if kind == "callback":
        # A cue to revisit a subject is not an observation about the user.
        # Validated user admission above remains authoritative.
        if isinstance(meta.get("turn_outcome"), dict):
            return MemoryOrigin("aiko", "intention", "reflection_worker")
        return MemoryOrigin("unknown", "intention", writer)
    if kind == "self_tagged" and str(getattr(memory, "content", "")).startswith(("I ", "My ")):
        return MemoryOrigin("unknown", "reflection", writer)
    if kind == "catchphrase":
        who = meta.get("origin")
        return MemoryOrigin("aiko" if who == "assistant" else "user" if who == "user"
                            else "relationship", "declaration", writer)
    if kind in {"fact", "event", "preference", "relationship", "self_tagged"}:
        mode = ("intention" if getattr(memory, "temporal_type", None) == "future_plan"
                else "observation")
        return MemoryOrigin("user", mode, writer)
    return MemoryOrigin("unknown", "unknown", writer)


def stamp_origin(kind, content, metadata, temporal_type=None):
    result = dict(metadata or {})
    if "memory_origin" not in result:
        result["memory_origin"] = asdict(memory_origin(SimpleNamespace(
            kind=kind, content=content, metadata=result, temporal_type=temporal_type,
        )))
    return result


def claim_source_refusal(memory, *, subject, kind) -> str | None:
    origin = memory_origin(memory)
    if origin.mode in {"hypothetical", "question", "summary", "unknown", "world_fact"}:
        return origin.mode
    if origin.subject == "unknown":
        return "unknown_subject"
    if (origin.mode == "intention" or getattr(memory, "temporal_type", None) == "future_plan"
            ) and kind == "narrative":
        return "intention_is_not_completion"
    if subject == "user" and origin.subject not in {"user", "relationship"}:
        return "different_subject"
    if subject == "aiko" and origin.subject not in {"aiko", "relationship"}:
        return "different_subject"
    return None


def origin_annotation(memory) -> str:
    origin = memory_origin(memory)
    parts = [f"subject={origin.subject}", f"source={origin.mode}", f"domain={origin.domain}"]
    parts.append("authority=" + str(getattr(memory, "provenance", "unknown") or "unknown"))
    if origin.writer.startswith("legacy_kind:"):
        parts.append("basis=kind_contract")
    temporal = getattr(memory, "temporal_type", None)
    if temporal:
        parts.append(f"temporal={temporal}")
    from app.core.infra import timephrase

    event = getattr(memory, "event_time", None)
    if timephrase.parse_iso(event):
        parts.append(f"event_time={event}")
    if getattr(memory, "kind", "") == "promise":
        meta = getattr(memory, "metadata", None) or {}
        parts.append(f"promise_status={meta.get('promise_status', 'unknown')}")
        if meta.get("promise_deadline"):
            parts.append(f"deadline={meta['promise_deadline']}")
    from app.core.memory.observation_receipts import observation_context

    observation = observation_context(memory)
    if observation:
        parts.append(observation)
    from app.core.memory.plan_reports import report_annotation

    report = report_annotation(memory)
    if report:
        parts.append(report)
    return "; ".join(parts)
