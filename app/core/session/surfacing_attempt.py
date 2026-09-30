"""Attempt-scoped pooled claims and content-free surfacing diagnostics."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable
from uuid import uuid4

from app.core.conversation.turn_shape import brief_reply_kind

@dataclass
class SurfaceClaim:
    cue_id: int
    cue_type: str
    text: str
    commit: Callable[[], None]
    state: str = "staged"
    blocks: set[str] = field(default_factory=set)
    source_revision: str = ""


@dataclass
class SurfaceAttempt:
    identity: str = field(default_factory=lambda: uuid4().hex)
    completed_exchange: bool = False
    claims: dict[tuple[int, int], SurfaceClaim] = field(default_factory=dict)
    declines: dict[str, str] = field(default_factory=dict)
    decision: dict[str, str] = field(default_factory=dict)
    optional_choice: dict = field(default_factory=dict)

    def stage(self, owner: object, row, commit: Callable[[], None], *, block: str = "") -> None:
        claim = self.claims.setdefault(
            (id(owner), row.id), SurfaceClaim(row.id, row.cue_type, row.text, commit),
        )
        claim.blocks.add(block)
        claim.source_revision = f"{row.created_at}:{row.surfaced_count}"

    def finish(
        self, text: str, *, discarded: str = "", included_blocks: dict | None = None,
    ) -> None:
        for claim in self.claims.values():
            if claim.state != "staged":
                continue
            if discarded:
                claim.state = discarded
            elif not claim.text or claim.text not in text:
                claim.state = "omitted"
            elif included_blocks is not None and not any(
                included_blocks.get(block, 0) for block in claim.blocks
            ):
                claim.state = "omitted"
            else:
                try:
                    claim.commit()
                    claim.state = "committed"
                except Exception as exc:
                    claim.state = "error:" + type(exc).__name__

    def snapshot(self) -> dict:
        return {
            "attempt_id": self.identity,
            "declines": dict(self.declines),
            "decision": dict(self.decision),
            "optional_choice": dict(self.optional_choice),
            "claims": [
                {
                    "cue_id": claim.cue_id, "cue_type": claim.cue_type, "state": claim.state,
                    "source_revision": claim.source_revision, "blocks": sorted(claim.blocks),
                }
                for claim in self.claims.values()
            ],
        }


current_attempt: ContextVar[SurfaceAttempt | None] = ContextVar(
    "surfacing_attempt", default=None,
)


def guard_optional_provider(provider, block: str):
    if getattr(provider, "_completion_guarded", False) is True:
        return provider

    @wraps(provider)
    def guarded(*args, **kwargs):
        attempt = current_attempt.get()
        if attempt is not None and attempt.completed_exchange:
            attempt.declines[block] = "completed_exchange"
            return ""
        return provider(*args, **kwargs)

    guarded._completion_guarded = True
    return guarded


def trace_assembly(assemble):
    @wraps(assemble)
    def wrapped(self, *args, **kwargs):
        preview = bool(kwargs.pop("preview", False))
        user_text = kwargs.get("user_text", args[1] if len(args) > 1 else "")
        attempt = SurfaceAttempt(
            completed_exchange=brief_reply_kind(user_text) == "completion",
        )
        token = current_attempt.set(attempt)
        try:
            messages, telemetry = assemble(self, *args, **kwargs)
            discarded = "preview" if preview else (
                "overflow_retry" if telemetry.compaction_triggered else ""
            )
            attempt.finish(
                telemetry.system_prompt, discarded=discarded, included_blocks=telemetry.block_chars,
            )
            record = attempt.snapshot()
            record["providers"] = dict(telemetry.provider_outcomes)
            record["blocks"] = {
                name: "included" if size else "empty_or_gated"
                for name, size in telemetry.block_chars.items()
            }
            record["handling"] = {
                "included": bool(telemetry.block_chars.get("handling_notes_block")),
                "bundles_omitted": [
                    name for name, reason in attempt.declines.items() if reason == "handling_budget"
                ],
            }
            previous = getattr(self, "_surfacing_attempt_history", [])
            history = previous[-1:] if kwargs.get("aggressive", False) else []
            self._surfacing_attempt_history = [*history, record]
            telemetry.surfacing_trace = list(self._surfacing_attempt_history)
            return messages, telemetry
        finally:
            current_attempt.reset(token)

    return wrapped
