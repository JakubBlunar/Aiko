"""F6 — privacy-preserving query reformulation.

The deterministic privacy gate
(:func:`app.core.memory.fact_check_privacy.scrub_claim_for_search`)
strips name / pronoun / PII tokens and then *rejects* the whole query
when what survives is too short or has no real word. That is correct
(don't leak the name) but the outcome is often wrong — the underlying
*topic* was perfectly searchable. F6 adds a local-LLM step that rewrites
the personal claim into its neutral, name-free topic query *before* the
length/word reject, with the deterministic scrubber kept as a hard
post-filter so a hallucinated name can never slip through to the search
engine.

The core :func:`reformulate_query_for_search` is LLM-agnostic — it takes
a ``reformulate_fn`` callable so it stays trivially unit-testable.
:func:`make_reformulator` builds that callable over a worker
``OllamaClient`` (local model, zero cloud cost, the name never leaves
the box).
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Iterable

from app.core.memory.fact_check_privacy import scrub_claim_for_search


log = logging.getLogger("app.query_reformulation")


_REFORMULATE_SYSTEM = (
    "You rewrite a personal note into a neutral web-search query about "
    "the general topic only. Remove every personal name, pronoun, date, "
    "and private detail; keep only the searchable subject. Reply with "
    "ONLY the query on a single line (no quotes, no preamble), or the "
    "single word NONE if there is no general topic worth searching."
)


# Prompt used when the caller supplies surrounding context. The claim
# spans this runs on are sub-sentence fragments with no predicate — the
# extractor's own measurement was that 0 of 90 spans contained a verb — so
# without the enclosing sentence the model is asked to write a search
# query for "Fullmetal Alchemist" or "2026" and can only pad it with a
# guess. The sentence says what is actually being asserted, which is the
# thing a search has to be able to confirm or refute. It is included as
# CONTEXT rather than as the query so the model still targets the span,
# and it never reaches the network: the returned query goes through the
# deterministic scrubber before any search.
_REFORMULATE_CONTEXT_SYSTEM = (
    "You turn a claim into a neutral web-search query that could confirm "
    "or refute it. You get the CLAIM (a short fragment) and the CONTEXT "
    "sentence it was taken from. Use the context to understand what is "
    "being asserted, then write the query someone would type to check "
    "it. Remove every personal name, pronoun, date, and private detail; "
    "keep only the searchable subject and whatever makes the claim "
    "checkable. Reply with ONLY the query on a single line (no quotes, no "
    "preamble), or the single word NONE if there is no general, "
    "checkable topic."
)


def _context_prompt(claim_text: str, context: str) -> str:
    """Render the two-part user message for a context-aware rewrite."""
    return f"CLAIM: {claim_text}\nCONTEXT: {context}"

# Cap the reformulation completion — a search query is short.
_REFORMULATE_MAX_TOKENS = 64


def _clean_llm_query(raw: str) -> str | None:
    """Take the first non-empty line, strip quotes/backticks/prefixes.

    Returns ``None`` when the model declined (``NONE``) or produced
    nothing usable.
    """
    if not raw:
        return None
    # First non-empty line only.
    line = ""
    for candidate in str(raw).splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    if not line:
        return None
    # Strip a leading "query:" / "search:" label some models prepend.
    line = re.sub(r"^(?:query|search)\s*[:\-]\s*", "", line, flags=re.IGNORECASE)
    # Strip wrapping quotes / backticks.
    line = line.strip().strip("\"'`").strip()
    if not line:
        return None
    if line.strip().upper() == "NONE":
        return None
    return line


def reformulate_query_for_search(
    claim_text: str,
    *,
    reformulate_fn: Callable[..., str | None],
    context: str = "",
    already_neutral: bool = False,
    user_names: Iterable[str] | None = None,
    assistant_name: str | None = None,
) -> str | None:
    """Return a search-safe query for ``claim_text``, or ``None``.

    Flow:

    1. Ask ``reformulate_fn`` (the local LLM) to rewrite the claim into a
       neutral topic query. A ``NONE`` / blank / raised result skips to
       the deterministic fallback.
    2. Post-filter the LLM output through
       :func:`scrub_claim_for_search` — this is the leak guard: any name
       / PII the model failed to remove is caught here. If the post-filter
       passes, use it.
    3. Otherwise fall back to the deterministic scrub of the *original*
       claim (the pre-F6 behaviour). Only when that also fails do we
       return ``None`` (silent skip).

    ``context`` is the sentence the claim was cut from. Pass it whenever
    it is available: a bare span usually has no predicate, and rewriting
    one without context can only pad it. The context never leaves the box
    — it shapes the query, and the query still faces the scrubber.

    ``already_neutral`` skips step 1 for queries that some *other* LLM
    pass already wrote to be impersonal (F9's research planner). Measured
    over the live log, 28 of 34 reformulations of planner output were
    byte-identical no-ops, so the rewrite was a second generation per
    search that changed nothing. The deterministic leak guard still runs,
    which is the part that actually protects the outbound query.
    """
    text = (claim_text or "").strip()
    if not text:
        return None

    if already_neutral:
        return scrub_claim_for_search(
            text,
            user_names=user_names,
            assistant_name=assistant_name,
        )

    hint = (context or "").strip()
    raw: str | None = None
    try:
        raw = reformulate_fn(text, hint) if hint else reformulate_fn(text)
    except TypeError:
        # A caller-supplied ``reformulate_fn`` that only accepts the claim
        # (older callables, test doubles) still works — just without the
        # context.
        try:
            raw = reformulate_fn(text)
        except Exception:
            log.debug(
                "reformulation call raised; using deterministic scrub",
                exc_info=True,
            )
            raw = None
    except Exception:
        log.debug("reformulation call raised; using deterministic scrub", exc_info=True)
        raw = None

    candidate = _clean_llm_query(raw or "")
    if candidate:
        safe = scrub_claim_for_search(
            candidate,
            user_names=user_names,
            assistant_name=assistant_name,
        )
        if safe:
            log.info(
                "query reformulated: in=%r context=%r out=%r",
                text[:80],
                hint[:80],
                safe[:80],
            )
            return safe
        log.debug(
            "reformulation %r failed post-filter; using deterministic scrub",
            candidate[:80],
        )

    # Deterministic fallback on the original claim.
    return scrub_claim_for_search(
        text,
        user_names=user_names,
        assistant_name=assistant_name,
    )


def make_reformulator(
    *,
    ollama: Any,
    chat_model: str,
    cancel_event: "threading.Event | None" = None,
    surface: str = "query_reformulation",
    max_tokens: int = _REFORMULATE_MAX_TOKENS,
) -> Callable[..., str | None]:
    """Build a ``reformulate_fn`` over a worker ``OllamaClient``.

    The returned callable streams a one-line completion from the local
    worker model. Any transport error returns ``None`` so the caller
    falls back to the deterministic scrub. It accepts an optional second
    argument — the sentence the claim came from — and switches to the
    context-aware prompt when one is given.
    """

    def _reformulate(claim_text: str, context: str = "") -> str | None:
        hint = (context or "").strip()
        if hint:
            system = _REFORMULATE_CONTEXT_SYSTEM
            user = _context_prompt(claim_text, hint)
        else:
            system = _REFORMULATE_SYSTEM
            user = claim_text
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            chunks: list[str] = []
            stream = ollama.chat_stream(
                messages,
                options={"num_predict": int(max_tokens)},
                model=chat_model,
                stop_event=cancel_event,
                surface=surface,
            )
            for chunk in stream:
                chunks.append(chunk)
            return "".join(chunks).strip() or None
        except Exception:
            log.debug("reformulator stream raised", exc_info=True)
            return None

    return _reformulate


__all__ = ["reformulate_query_for_search", "make_reformulator"]
