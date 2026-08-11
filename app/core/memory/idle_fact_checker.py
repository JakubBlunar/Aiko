"""Background fact-checker (F1 personality backlog).

Runs on the existing :class:`IdleWorkerScheduler`. Each tick pops one
claim from :class:`FactCheckQueue`, asks the (existing) web-search tool
for 3 snippets, then distils a JSON verdict via
:meth:`OllamaClient.chat_stream` (so cancellation lands cleanly — see
F1.6 in the plan).

Key invariant: the chat agent never sees these web snippets. The
distillation happens with a tiny ~1.2 KB prompt and a ~80-token JSON
response, so even running on the main chat model it returns in a couple
of seconds; if the user starts a new turn mid-distil,
``_cancel_event.set()`` aborts the stream and the claim goes back to
the head of the queue.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import WorkSignal, pressure_from_count
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.memory.fact_check_queue import ClaimItem, FactCheckQueue
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.knowledge_gap_extractor import KnowledgeGapStore
    from app.core.memory.memory_store import MemoryStore
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.idle_fact_checker")


# Cap on how much of a claim / snippet / raw model output we render
# per log line. Audit-friendly previews; the rotating log stays
# scannable.
_LOG_PREVIEW_CHARS = 200


def _preview(text: str | None) -> str:
    """Single-line, length-bounded preview for the audit log."""
    if not text:
        return "<empty>"
    flat = " ".join(str(text).split())
    if len(flat) > _LOG_PREVIEW_CHARS:
        return flat[: _LOG_PREVIEW_CHARS - 1] + "…"
    return flat


# ── prompt template ────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You verify factual claims against web search excerpts. "
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {\"verdict\": \"support\"|\"contradict\"|\"inconclusive\", "
    "\"delta\": <number in [-0.3, 0.3]>, "
    "\"rewrite\": null | \"corrected claim text under 140 chars\"}. "
    "Use 'support' only when at least one excerpt directly confirms the "
    "claim. Use 'contradict' when an excerpt directly disagrees. "
    "Use 'inconclusive' otherwise. ``delta`` is positive for support, "
    "negative for contradict, zero for inconclusive. ``rewrite`` is the "
    "corrected claim text on a contradict verdict; leave null otherwise."
)

_USER_TEMPLATE = (
    "CLAIM: {claim}\n"
    "EXCERPTS:\n{excerpts}"
)


# Caps for the prompt so a chatty snippet can't blow up the context.
_MAX_SNIPPET_CHARS = 400
_MAX_EXCERPTS = 3
_DISTIL_MAX_TOKENS = 120


_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


@dataclass(frozen=True)
class Verdict:
    """Parsed distil output."""

    kind: str  # "support" / "contradict" / "inconclusive"
    delta: float  # additive change to confidence (clamped to [-0.3, 0.3])
    rewrite: str | None  # optional corrected claim text


class IdleFactChecker:
    """IdleWorker that closes the loop on F3 + F2 by verifying claims."""

    name = "idle_fact_checker"

    def __init__(
        self,
        *,
        queue: "FactCheckQueue",
        memory_store: "MemoryStore",
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        ollama: "OllamaClient",
        chat_model: str,
        web_search_tool: Any,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        knowledge_gap_store: "KnowledgeGapStore | None" = None,
        embedder: "Embedder | None" = None,
        notify_memory_updated: Any | None = None,
        user_names_provider: Any | None = None,
        assistant_name_provider: Any | None = None,
        query_reformulator: Any | None = None,
        arm_reversal: Any | None = None,
        was_surfaced: Any | None = None,
    ) -> None:
        self._queue = queue
        self._memory_store = memory_store
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._ollama = ollama
        self._chat_model = chat_model
        self._web_search = web_search_tool
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._knowledge_gap_store = knowledge_gap_store
        self._embedder = embedder
        self._notify_memory_updated = notify_memory_updated
        # Callables (no args) returning the current user name list +
        # assistant name. Late-bound so a rename mid-session is picked
        # up on the next tick without rebuilding the worker.
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        # F6: optional local-LLM query reformulator. When set, personal
        # claims are rewritten into neutral topic queries (post-filtered
        # by the deterministic scrubber) before search.
        self._query_reformulator = query_reformulator
        # F14: arm a next-turn "I looked into that and had it wrong" cue
        # when this worker's own research reverses a claim Aiko surfaced.
        # ``arm_reversal(wrong, corrected, memory_id) -> bool`` writes the
        # cue; ``was_surfaced(memory_id) -> bool`` reads the L37 ledger to
        # confirm she actually said it. Both late-bound so init order (and
        # a session without the controller) is irrelevant.
        self._arm_reversal = arm_reversal
        self._was_surfaced = was_surfaced

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(self._memory_settings.fact_checker_interval_seconds)

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        """Enabled, something queued, and budget to check it with.

        All three stay hard vetoes after the demand migration: an empty
        queue or a spent hour makes a run a guaranteed no-op, and the
        heartbeat is checked before pressure, so expressing them as
        zero pressure would not hold the worker back.
        """
        return self._backlog(now) is not None

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Pressure from the depth of the claim queue.

        The cleanest backlog in the whole registry: one kv read gives
        the exact number of claims waiting, and the worker drains
        exactly one per run. Saturation is the hourly budget, since a
        queue deeper than that cannot be drained faster however it is
        ranked.
        """
        backlog = self._backlog(now)
        if backlog is None:
            return WorkSignal(pressure=0.0, reason="nothing queued")
        pending, hour_cap = backlog
        return WorkSignal(
            pressure=pressure_from_count(pending, saturation=hour_cap),
            reason=f"{pending} queued",
            needs_llm=True,
        )

    def _backlog(self, now: datetime) -> tuple[int, int] | None:
        """``(queued, hour_cap)`` if a run could check something."""
        if not bool(getattr(self._agent_settings, "fact_checker_enabled", True)):
            return None
        try:
            snapshot = self._rate_limiter.snapshot(now)
            if snapshot["hour_used"] >= snapshot["hour_cap"]:
                return None
            if snapshot["day_used"] >= snapshot["day_cap"]:
                return None
            pending = len(self._queue.peek_all())
        except Exception:
            log.debug("fact-check readiness probe failed", exc_info=True)
            return None
        if pending <= 0:
            return None
        return pending, max(1, int(snapshot["hour_cap"]))

    def run(self) -> dict[str, Any] | None:
        if not bool(getattr(self._agent_settings, "fact_checker_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}
        claim = self._queue.pop_next()
        if claim is None:
            return {"skipped": True, "reason": "empty_queue"}
        # Re-check the rate limit + actually consume one token. We may
        # have slipped past ``is_ready`` if multiple workers were
        # scheduled in the same window.
        if not self._rate_limiter.allow():
            log.info(
                "fact-check skip: rate limited (memory_id=%s claim=%r)",
                claim.memory_id,
                _preview(claim.claim_text),
            )
            self._queue.requeue_front(claim)
            return {"skipped": True, "reason": "rate_limited"}
        log.info(
            "fact-check start: memory_id=%s kind=%s claim=%r",
            claim.memory_id,
            claim.claim_kind,
            _preview(claim.claim_text),
        )
        # Privacy gate (defense in depth — the queue gate already
        # filtered most personal memories upstream). Returns a safe
        # variant or None when the claim can't be scrubbed cleanly.
        # The privacy module logs the actual decision; we log the
        # outcome from the worker's perspective so timing context is
        # preserved.
        safe_query = self._scrub_claim(claim)
        if safe_query is None:
            log.info(
                "fact-check skip: privacy gate dropped claim "
                "memory_id=%s claim=%r",
                claim.memory_id,
                _preview(claim.claim_text),
            )
            return {"skipped": True, "reason": "privacy_gate"}
        # The proposition the model adjudicates. Scrubbed separately
        # from the query and never sent outbound -- it only reaches the
        # local LLM, which the threat model already trusts with this
        # content. When it can't be scrubbed we fall back to the span
        # rather than dropping the claim, matching the old behaviour.
        safe_sentence = self._scrub_sentence(claim) or safe_query
        log.info(
            "fact-check scrubbed: memory_id=%s safe_query=%r claim=%r",
            claim.memory_id,
            _preview(safe_query),
            _preview(safe_sentence),
        )

        search_t0 = time.monotonic()
        try:
            snippets = self._search(claim, safe_query=safe_query)
        except Exception:
            search_ms = (time.monotonic() - search_t0) * 1000.0
            log.warning(
                "fact-check search failed: memory_id=%s elapsed_ms=%.0f",
                claim.memory_id,
                search_ms,
                exc_info=True,
            )
            return {"checked": 0, "error": "search_failed"}
        search_ms = (time.monotonic() - search_t0) * 1000.0
        # Render the result list compactly: first 80 chars of each
        # title + truncated URL host so the audit can tell what the
        # search engine returned without dumping the full snippets
        # (those go in DEBUG).
        result_summary = [
            {
                "title": (s.get("title") or "")[:80],
                "url": (s.get("url") or "")[:120],
            }
            for s in snippets
        ]
        log.info(
            "fact-check search done: memory_id=%s elapsed_ms=%.0f "
            "result_count=%d top=%s",
            claim.memory_id,
            search_ms,
            len(snippets),
            result_summary,
        )
        if log.isEnabledFor(logging.DEBUG):
            for idx, s in enumerate(snippets):
                log.debug(
                    "fact-check snippet[%d]: title=%r url=%s body=%r",
                    idx,
                    (s.get("title") or "")[:120],
                    (s.get("url") or "")[:160],
                    _preview(s.get("snippet")),
                )
        if self._cancel_event.is_set():
            log.info(
                "fact-check cancelled mid-search: memory_id=%s",
                claim.memory_id,
            )
            self._queue.requeue_front(claim)
            return {"cancelled": True}

        distil_t0 = time.monotonic()
        verdict = self._distil(claim, snippets, safe_claim=safe_sentence)
        distil_ms = (time.monotonic() - distil_t0) * 1000.0
        if verdict is None:
            log.info(
                "fact-check distil cancel/parse-fail: memory_id=%s elapsed_ms=%.0f",
                claim.memory_id,
                distil_ms,
            )
            # ``_distil`` returns None on cancel or parse failure. Put
            # the claim back at the head so the next tick retries.
            self._queue.requeue_front(claim)
            return {"cancelled": True}
        log.info(
            "fact-check distil done: memory_id=%s elapsed_ms=%.0f "
            "verdict=%s delta=%+.2f rewrite=%r",
            claim.memory_id,
            distil_ms,
            verdict.kind,
            verdict.delta,
            _preview(verdict.rewrite) if verdict.rewrite else None,
        )

        applied = self._apply_verdict(claim, verdict) or {}
        log.info(
            "fact-check apply done: memory_id=%s verdict=%s "
            "confidence %.2f -> %.2f rewrote=%s resolved_gap=%s",
            claim.memory_id,
            verdict.kind,
            float(applied.get("confidence_before", 0.0)),
            float(applied.get("confidence_after", 0.0)),
            bool(applied.get("rewrote", False)),
            bool(applied.get("resolved_gap", False)),
        )
        return {
            "checked": 1,
            "verdict": verdict.kind,
            "memory_id": claim.memory_id,
            **applied,
        }

    # ── pieces ───────────────────────────────────────────────────────

    def _current_names(self) -> tuple[list[str] | None, str | None]:
        """Return the live (user_names, assistant_name) pair.

        Read from the providers on every call so a mid-session rename is
        honoured immediately.
        """
        user_names: list[str] | None = None
        if self._user_names_provider is not None:
            try:
                provided = self._user_names_provider()
                if provided:
                    user_names = list(provided)
            except Exception:
                user_names = None
        assistant_name: str | None = None
        if self._assistant_name_provider is not None:
            try:
                assistant_name = self._assistant_name_provider() or None
            except Exception:
                assistant_name = None
        return user_names, assistant_name

    def _scrub_sentence(self, claim: "ClaimItem") -> str | None:
        """Return a scrubbed variant of the claim's enclosing sentence.

        This is what the local model adjudicates, so it deliberately
        skips the F6 query reformulator — that exists to turn a personal
        claim into a neutral *search* query, and rewriting the
        proposition would change what is being verified. ``None`` when
        there is no sentence or it cannot be scrubbed; the caller falls
        back to the span.
        """
        from app.core.memory.fact_check_privacy import scrub_claim_for_search

        sentence = (getattr(claim, "claim_sentence", "") or "").strip()
        if not sentence:
            return None
        user_names, assistant_name = self._current_names()
        return scrub_claim_for_search(
            sentence,
            user_names=user_names,
            assistant_name=assistant_name,
        )

    def _scrub_claim(self, claim: "ClaimItem") -> str | None:
        """Return a privacy-scrubbed variant of the claim text.

        This is the **outbound** search query. The actual scrubbing
        logic lives in
        :func:`app.core.memory.fact_check_privacy.scrub_claim_for_search`.
        """
        from app.core.memory.fact_check_privacy import scrub_claim_for_search

        user_names, assistant_name = self._current_names()
        if self._query_reformulator is not None:
            from app.core.memory.query_reformulation import (
                reformulate_query_for_search,
            )

            return reformulate_query_for_search(
                claim.claim_text,
                reformulate_fn=self._query_reformulator,
                user_names=user_names,
                assistant_name=assistant_name,
            )
        return scrub_claim_for_search(
            claim.claim_text,
            user_names=user_names,
            assistant_name=assistant_name,
        )

    def _search(
        self,
        claim: "ClaimItem",
        *,
        safe_query: str | None = None,
    ) -> list[dict[str, str]]:
        """Run the web-search helper and return up to ``_MAX_EXCERPTS``.

        ``safe_query`` is the privacy-scrubbed query produced by
        :func:`scrub_claim_for_search`. The original ``claim.claim_text``
        is only used as a fallback when the worker is called via legacy
        paths (e.g. tests) that didn't pre-scrub.
        """
        if self._web_search is None:
            return []
        query = safe_query if safe_query else claim.claim_text
        result_text = self._web_search.run(
            {"query": query, "max_results": _MAX_EXCERPTS},
        )
        try:
            parsed = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return []
        results = parsed.get("results", []) if isinstance(parsed, dict) else []
        out: list[dict[str, str]] = []
        for item in results[:_MAX_EXCERPTS]:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or item.get("body") or "").strip()
            if not snippet:
                continue
            out.append({
                "title": str(item.get("title", ""))[:120],
                "url": str(item.get("url", ""))[:200],
                "snippet": snippet[:_MAX_SNIPPET_CHARS],
            })
        return out

    def _distil(
        self,
        claim: "ClaimItem",
        snippets: list[dict[str, str]],
        *,
        safe_claim: str | None = None,
    ) -> Verdict | None:
        if not snippets:
            return Verdict(kind="inconclusive", delta=0.0, rewrite=None)
        excerpts_text = "\n".join(
            f"- {s['title']} ({s['url']}): {s['snippet']}"
            for s in snippets[:_MAX_EXCERPTS]
        )
        # Always feed the *scrubbed* version of the claim to the LLM
        # too. The model is local so this is belt-and-braces, but it
        # keeps the privacy boundary consistent — there's only one
        # place that sees the original claim text (the verdict
        # application step, which writes back to the memory store).
        # Prefer the enclosing sentence: the bare span is not a
        # proposition, so a verdict on it is uninterpretable.
        prompt_claim = safe_claim or claim.claim_sentence or claim.claim_text
        user_content = _USER_TEMPLATE.format(
            claim=prompt_claim,
            excerpts=excerpts_text,
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        # The full prompt only goes to the LLM (local) and to DEBUG
        # logs so an audit can see exactly what was sent. The user
        # part already contains the scrubbed claim + the search
        # excerpts, so this is the single source of truth for "what
        # did the model see".
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "fact-check distil prompt: model=%s prompt_chars=%d "
                "user_payload=%r",
                self._chat_model,
                len(user_content) + len(_SYSTEM_PROMPT),
                _preview(user_content),
            )
        # We rely on chat_stream's stop_event support for cancellation.
        # The format_json hint nudges Ollama-supporting models to emit
        # a single JSON object; we still tolerate stray prose via the
        # JSON object regex below.
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _DISTIL_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="idle_fact_checker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("fact-check distil call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            log.info(
                "fact-check distil produced empty output: memory_id=%s",
                claim.memory_id,
            )
            return None
        log.debug(
            "fact-check distil raw: memory_id=%s chars=%d preview=%r",
            claim.memory_id,
            len(raw),
            _preview(raw),
        )
        return self._parse_verdict(raw)

    def _parse_verdict(self, raw: str) -> Verdict | None:
        # Some models still wrap JSON with stray prose despite the hint.
        # Find the first JSON-looking blob to be robust.
        text = raw.strip()
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        kind = str(parsed.get("verdict", "")).strip().lower()
        if kind not in ("support", "contradict", "inconclusive"):
            return None
        try:
            delta = float(parsed.get("delta", 0.0))
        except (TypeError, ValueError):
            delta = 0.0
        delta = max(-0.3, min(0.3, delta))
        # Cross-check delta sign against verdict so a confused model
        # can't bump confidence on a contradict.
        if kind == "support" and delta < 0:
            delta = abs(delta)
        elif kind == "contradict" and delta > 0:
            delta = -delta
        elif kind == "inconclusive":
            delta = 0.0
        rewrite_raw = parsed.get("rewrite")
        rewrite = None
        if isinstance(rewrite_raw, str):
            cleaned = rewrite_raw.strip()
            if cleaned and 4 <= len(cleaned) <= 240:
                rewrite = cleaned
        return Verdict(kind=kind, delta=delta, rewrite=rewrite)

    def _apply_verdict(
        self,
        claim: "ClaimItem",
        verdict: Verdict,
    ) -> dict[str, Any]:
        memory = self._memory_store.get(int(claim.memory_id))
        if memory is None:
            # The underlying memory was deleted while the claim was
            # queued — nothing to update.
            return {"memory_missing": True}
        now_iso = timephrase.utcnow().isoformat()
        metadata = dict(memory.metadata) if memory.metadata else {}
        flags = dict(metadata.get("flags") or {}) if isinstance(metadata.get("flags"), dict) else {}

        current_conf = float(memory.confidence)
        new_conf = current_conf
        new_content = memory.content
        details: dict[str, Any] = {}
        # F14: snapshot the pre-update claim + provenance before ``update``
        # mutates (or replaces) the row, so the reversal cue owns the text
        # Aiko actually said and the suppression reads the original state.
        orig_content = memory.content
        orig_meta = dict(memory.metadata) if memory.metadata else {}
        orig_tier = str(getattr(memory, "tier", ""))

        if verdict.kind == "support":
            new_conf = min(0.95, current_conf + verdict.delta)
            metadata["last_verified_at"] = now_iso
            # Reset any prior conflict flag — the new evidence supports
            # the claim.
            flags.pop("conflict", None)
        elif verdict.kind == "contradict":
            new_conf = max(0.2, current_conf + verdict.delta)
            metadata["last_verified_at"] = now_iso
            flags["conflict"] = True
            # Only accept the rewrite when the model is confident
            # enough (|delta| > 0.2). Otherwise leave the original
            # content and let the user decide.
            if verdict.rewrite and abs(verdict.delta) > 0.2:
                new_content = verdict.rewrite
                details["rewrote"] = True
        else:  # inconclusive
            metadata["last_checked_at"] = now_iso

        if flags:
            metadata["flags"] = flags
        elif "flags" in metadata:
            metadata.pop("flags", None)

        try:
            updated = self._memory_store.update(
                int(claim.memory_id),
                content=new_content if new_content != memory.content else None,
                metadata=metadata,
                metadata_merge=True,
                confidence=new_conf,
            )
        except Exception:
            log.warning(
                "fact-check update failed: memory_id=%s",
                claim.memory_id,
                exc_info=True,
            )
            return {"update_failed": True}

        if updated is not None and self._notify_memory_updated is not None:
            try:
                self._notify_memory_updated(updated.to_dict())
            except Exception:
                log.debug("fact-check notify failed", exc_info=True)

        # Knowledge-gap resolution: when the queued item *is* a gap and
        # the verdict supports it, write the answer as a sibling memory
        # and stamp ``resolved_at`` so the journal closes the loop.
        gap_store = self._knowledge_gap_store
        if (
            gap_store is not None
            and claim.claim_kind == "knowledge_gap"
            and verdict.kind == "support"
            and self._embedder is not None
        ):
            answer_text = self._pick_answer_text(verdict, claim)
            answer_memory_id: int | None = None
            if answer_text:
                try:
                    emb = self._embedder.embed(answer_text)
                    answer_mem = self._memory_store.add(
                        content=answer_text,
                        kind="fact",
                        embedding=emb,
                        salience=0.7,
                        confidence=0.85,
                        tier="long_term",
                    )
                    if answer_mem is not None:
                        answer_memory_id = int(answer_mem.id)
                except Exception:
                    log.debug("gap answer write failed", exc_info=True)
            try:
                gap_store.mark_resolved(
                    int(claim.memory_id),
                    answer_memory_id=answer_memory_id,
                )
                details["resolved_gap"] = True
                if answer_memory_id is not None:
                    details["answer_memory_id"] = answer_memory_id
            except Exception:
                log.debug("gap mark_resolved failed", exc_info=True)

        details["confidence_before"] = float(current_conf)
        details["confidence_after"] = float(new_conf)

        # F14: if this check reversed a claim Aiko actually surfaced, arm a
        # next-turn cue so she owns it. Uses the pre-update snapshot (old
        # content + original metadata/tier) and the freshly written
        # ``new_content``.
        self._maybe_arm_reversal(
            memory_id=int(claim.memory_id),
            verdict=verdict,
            wrong_text=orig_content,
            corrected_text=new_content,
            rewrote=bool(details.get("rewrote", False)),
            meta=orig_meta,
            tier=orig_tier,
        )
        return details

    def _maybe_arm_reversal(
        self,
        *,
        memory_id: int,
        verdict: Verdict,
        wrong_text: str,
        corrected_text: str,
        rewrote: bool,
        meta: dict[str, Any],
        tier: str,
    ) -> None:
        """Arm the F14 "I looked into that and had it wrong" cue.

        Fires only on a genuine reversal Aiko already told the user: the
        verdict contradicts, the confidence drop clears
        ``memory.fact_reversal_min_delta``, and the content was actually
        rewritten (drift with no rewrite is not a conversational event).
        Then two more gates: she must have surfaced the claim (L37 ledger),
        and F13 must not have already handled it (a user correction arriving
        first makes the fact-checker redundant and a little insulting).
        """
        if not bool(getattr(self._agent_settings, "fact_reversal_enabled", True)):
            return
        if self._arm_reversal is None or self._was_surfaced is None:
            return
        if verdict.kind != "contradict" or not rewrote:
            return
        min_delta = float(
            getattr(self._memory_settings, "fact_reversal_min_delta", 0.25)
        )
        if abs(verdict.delta) < min_delta:
            return

        mid = int(memory_id or 0)
        if mid <= 0:
            return

        # F13 suppression: skip a row the user already set straight, or one
        # that has since been archived/superseded.
        if isinstance(meta, dict) and (
            meta.get("superseded_reason") == "user_correction"
            or meta.get("superseded_by")
            or meta.get("archived_at")
        ):
            return
        if str(tier or "").strip().lower() == "archive":
            return

        # She must actually have said it -- a low-confidence note quietly
        # fixed before it ever surfaced is nothing to apologise for.
        try:
            if not bool(self._was_surfaced(mid)):
                return
        except Exception:
            log.debug("fact-reversal surfaced-gate raised", exc_info=True)
            return

        wrong = str(wrong_text or "").strip()
        corrected = (corrected_text or "").strip()
        if not corrected or corrected == wrong:
            return
        try:
            armed = bool(
                self._arm_reversal(
                    wrong=wrong,
                    corrected=corrected,
                    memory_id=mid,
                )
            )
            log.info(
                "fact-reversal cue armed=%s: memory_id=%s delta=%+.2f "
                "wrong=%r corrected=%r",
                armed,
                mid,
                verdict.delta,
                _preview(wrong),
                _preview(corrected),
            )
        except Exception:
            log.debug("fact-reversal arm raised", exc_info=True)

    @staticmethod
    def _pick_answer_text(verdict: Verdict, claim: "ClaimItem") -> str | None:
        """Best-effort short answer text for a resolved gap.

        Prefer the model's ``rewrite`` (already a clean restatement);
        fall back to the original claim text so something gets written
        even when the model didn't bother with a rewrite.
        """
        if verdict.rewrite:
            return verdict.rewrite
        return (claim.claim_text or "").strip() or None
