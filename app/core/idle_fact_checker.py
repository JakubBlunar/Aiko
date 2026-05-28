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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.core.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.fact_check_queue import ClaimItem, FactCheckQueue
    from app.core.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.knowledge_gap_extractor import KnowledgeGapStore
    from app.core.memory_store import MemoryStore
    from app.core.settings import AgentSettings, MemorySettings
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
        if not bool(getattr(self._agent_settings, "fact_checker_enabled", True)):
            return False
        if not self._queue.has_pending():
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        ):
            return False
        snapshot = self._rate_limiter.snapshot(now)
        if snapshot["hour_used"] >= snapshot["hour_cap"]:
            return False
        if snapshot["day_used"] >= snapshot["day_cap"]:
            return False
        return True

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
        log.info(
            "fact-check scrubbed: memory_id=%s safe_query=%r",
            claim.memory_id,
            _preview(safe_query),
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
        verdict = self._distil(claim, snippets, safe_query=safe_query)
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

    def _scrub_claim(self, claim: "ClaimItem") -> str | None:
        """Return a privacy-scrubbed variant of the claim text.

        Pulls the current user / assistant names from the configured
        providers so a mid-session rename is honoured immediately. The
        actual scrubbing logic lives in
        :func:`app.core.fact_check_privacy.scrub_claim_for_search`.
        """
        from app.core.fact_check_privacy import scrub_claim_for_search

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
        safe_query: str | None = None,
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
        prompt_claim = safe_query if safe_query else claim.claim_text
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
        now_iso = datetime.now(timezone.utc).isoformat()
        metadata = dict(memory.metadata) if memory.metadata else {}
        flags = dict(metadata.get("flags") or {}) if isinstance(metadata.get("flags"), dict) else {}

        current_conf = float(memory.confidence)
        new_conf = current_conf
        new_content = memory.content
        details: dict[str, Any] = {}

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
        return details

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
