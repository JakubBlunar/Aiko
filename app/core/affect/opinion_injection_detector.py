"""Opinion injection detector (K29).

Per-turn detector that fires a one-line cue when {user_name}'s latest
message contradicts one of Aiko's stored ``kind="self"`` stance
memories. The whole point of K29 is to make the persona's "have
opinions, disagree when you disagree" claim *actually fire* against
LLM RLHF agreeability -- without flipping into contrarianism.

The anti-contrarianism guardrails are layered:

1. **Predicate filter**: only stance memories with an opinion-shaped
   predicate ("I prefer", "I don't like", "I love", "I find ...
   annoying", etc.) are eligible. Pure facts ("I was born in", "I
   live in") are filtered out -- those aren't stances, they're
   biographical data.
2. **Cosine threshold**: top-cosine match against the live user
   message must clear ``min_cosine`` (default 0.55). Below that,
   topic match is too weak to claim a real contradiction.
3. **Heuristic gate** (reused from F5): the eligible stance text is
   passed through :func:`app.core.memory.conflict_heuristics.classify_pair`
   against the user text. Only ``definite`` and (when caller
   allows) ``borderline`` results proceed.
4. **LLM YES/NO gate on borderline** (caller's responsibility): the
   detector signals a borderline classification by returning a
   pending result; the caller decides whether to spend the LLM
   budget. Keeps the detector itself pure (no async I/O).
5. **Cooldown + per-session cap**: enforced by the *caller* (see
   :meth:`app.core.session.inner_life_providers_mixin.InnerLifeProvidersMixin._render_opinion_injection_block`),
   not by the detector. The detector is a pure read.

The persona block (see ``data/persona/aiko_companion.txt`` "When you
have your own take") does the second half of the anti-contrarianism
work -- the cue text steers Aiko toward "*share* your take, not
*prescribe* his behavior". A failure where the detector fires
correctly but Aiko lectures Jacob is a persona-block bug, not a
detector bug.

This module is pure-python: no embedding calls, no SQL writes, no
threading. The caller embeds the user text once and passes the
vector in; the detector handles the cosine math + heuristic gate
+ predicate filter. That keeps the detector trivially testable
(see ``tests/test_opinion_injection_detector.py``).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Literal

import numpy as np

from app.core.memory.conflict_heuristics import (
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    classify_pair,
)
from app.llm.embedder import cosine_similarity

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory


log = logging.getLogger("app.opinion_injection_detector")


# Default thresholds. Mirrored verbatim in
# :class:`app.core.infra.settings.MemorySettings` so the call-site
# can wire user overrides through without losing the source-of-truth.
DEFAULT_MIN_COSINE: float = 0.55
DEFAULT_MIN_USER_WORDS: int = 4
DEFAULT_COOLDOWN_TURNS: int = 5
DEFAULT_PER_SESSION_CAP: int = 3
DEFAULT_PER_HOUR_CAP: int = 6
DEFAULT_PER_DAY_CAP: int = 30
DEFAULT_REQUIRE_DEFINITE: bool = False


TriggerLabel = Literal["contradiction_definite", "contradiction_borderline"]


# ── Opinion-shaped predicates ────────────────────────────────────────────
#
# A stance memory qualifies for K29 only if its content matches at
# least one of these patterns. The list is deliberately tight: we want
# "I really don't like horror" to qualify and "I was born in Tokyo"
# to not. Compiled once at module load; pattern strings live below
# for greppability.
#
# Notes on shape choices:
#  - Lowercase comparison; patterns are anchored to "i " (first-person)
#    because that's how Aiko writes self-tags.
#  - "i think X is/are <adj>" requires the adjective form to filter
#    out "I think we should ..." (intent, not stance).
#  - "i find X <adj>" lists the most opinion-loaded adjectives so
#    "I find the gym energising" qualifies but "I find Tokyo
#    interesting to walk in" does not (the latter is descriptive).
#  - The "i love" / "i hate" / "i like" / "i don't like" axis is
#    the most common; we want comprehensive cover there.
_OPINION_PATTERNS: tuple[str, ...] = (
    r"\bi\s+prefer\b",
    r"\bi['\u2019]?d\s+(?:rather|prefer)\b",
    r"\bi\s+(?:really\s+|honestly\s+)?(?:don['\u2019]?t|do\s+not)\s+(?:like|enjoy|trust|believe)\b",
    r"\bi\s+(?:really\s+|honestly\s+)?(?:like|love|enjoy)\b",
    r"\bi\s+(?:really\s+|honestly\s+)?(?:hate|dislike|loathe|despise)\b",
    r"\bi['\u2019]?m\s+not\s+a\s+(?:fan|huge\s+fan)\b",
    r"\bi\s+find\s+[\w\s'\-]{1,60}?\s+(?:annoying|boring|exhausting|exciting|interesting|tedious|charming|wonderful|terrible|cringy|fun|delightful|gross)\b",
    r"\bi\s+think\s+(?:it|that|they|this|those|these|he|she)\s+(?:is|are|was|were)\s+(?:not\s+)?(?:\w+ly\s+)?(?:\w+)\b",
    r"\bi\s+bounce\s+off\b",
    r"\bnot\s+(?:my|really\s+my)\s+(?:thing|favourite|favorite|jam|cup\s+of\s+tea)\b",
    r"\bmy\s+(?:least|absolute\s+)?favourite\b",
    r"\bmy\s+(?:least|absolute\s+)?favorite\b",
    r"\b(?:make|makes|made)\s+me\s+(?:anxious|nervous|happy|sad|grumpy|jumpy)\b",
)

_OPINION_RE = re.compile("|".join(_OPINION_PATTERNS), flags=re.IGNORECASE)


def _has_opinion_shape(content: str) -> bool:
    """Return True when ``content`` looks like a stance, not a fact.

    Cheap-as-it-gets: one compiled multi-alternative regex; we don't
    need full NLP because the regex is anchored on first-person
    stance markers that Aiko's persona explicitly instructs her to
    use when writing ``[[remember:self:...]]`` tags.
    """
    if not content or len(content) < 4:
        return False
    return _OPINION_RE.search(content) is not None


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OpinionInjectionResult:
    """One per-turn opinion-injection signal.

    ``trigger`` is the diagnostic label (``contradiction_definite`` or
    ``contradiction_borderline``). ``stance_text`` is the raw content
    of the matched ``kind="self"`` memory -- rendered into the cue so
    the LLM has Aiko's prior take in front of it. The persona block
    forbids quoting it back at Jacob.

    ``llm_verdict`` is ``None`` for definite hits (LLM not called) and
    one of ``"YES"`` / ``"NO"`` / ``"UNRELATED"`` for borderline hits
    that ran through the gate. Stored for the MCP debug tool.
    """

    trigger: TriggerLabel
    stance_text: str
    stance_memory_id: int
    cosine: float
    heuristic_label: str
    heuristic_signals: list[str]
    llm_verdict: str | None = None


# ── Detection pipeline ───────────────────────────────────────────────────


def _filter_opinion_memories(memories: Iterable["Memory"]) -> list["Memory"]:
    """Drop stance memories that don't have an opinion-shaped predicate."""
    out: list["Memory"] = []
    for mem in memories:
        if _has_opinion_shape(mem.content or ""):
            out.append(mem)
    return out


def _top_cosine(
    user_vec: np.ndarray,
    memories: list["Memory"],
) -> tuple["Memory", float] | None:
    """Return the highest-cosine (memory, score) pair or None on empty input."""
    if not memories or user_vec is None:
        return None
    best: tuple["Memory", float] | None = None
    for mem in memories:
        if mem.embedding is None:
            continue
        try:
            score = float(cosine_similarity(user_vec, mem.embedding))
        except Exception:
            log.debug("cosine raised for stance id=%s", mem.id, exc_info=True)
            continue
        if best is None or score > best[1]:
            best = (mem, score)
    return best


def detect(
    user_text: str,
    *,
    user_vec: np.ndarray | None,
    self_memories: Iterable["Memory"],
    llm_gate: Callable[[str, str], str | None] | None = None,
    min_cosine: float = DEFAULT_MIN_COSINE,
    min_user_words: int = DEFAULT_MIN_USER_WORDS,
    require_definite: bool = DEFAULT_REQUIRE_DEFINITE,
    defer_borderline: bool = False,
) -> OpinionInjectionResult | None:
    """Classify the current turn and return a result or ``None``.

    Pipeline:

    1. Length gate: drop messages under ``min_user_words`` (default 4).
       Short replies like "ok"/"lol"/"yeah" are K23 territory; K29
       only fires when there's enough text to credibly contradict.
    2. Predicate filter: drop self-memories that aren't opinion-
       shaped. Saves cosine compute and rules out the fact-shaped
       false positives.
    3. Cosine gate: top match must clear ``min_cosine``.
    4. Heuristic + LLM gate:

       * ``classify_pair`` runs against ``(user_text, stance.content)``.
       * ``"definite"`` (clear negation-flip with high content
         overlap, or explicit verb-pair antonym hit) fires
         immediately as ``contradiction_definite``. No LLM call.
       * Anything else (``"borderline"`` numerical mismatch, OR
         ``"no"`` due to diluted content overlap on a verbose
         stance) routes through ``llm_gate``. The gate returns
         ``"YES"`` to fire as ``contradiction_borderline``;
         ``"NO"`` / ``"UNRELATED"`` / ``None`` stays silent.
       * ``require_definite=True`` (Path C, no-LLM-cost) skips
         the LLM path entirely — only ``definite`` heuristic
         results fire.

    5. ``llm_gate`` is the caller's hook for the rate-limited LLM
       YES/NO check (the caller owns the limiter, the Ollama
       client, and the cancel event so the detector stays pure).
       Pass ``None`` to skip the LLM path entirely (equivalent to
       Path C behaviour without flipping ``require_definite``).

    6. ``defer_borderline`` (P21): when ``True``, a borderline candidate
       that clears the predicate + cosine gates is returned *without*
       calling ``llm_gate`` -- the result carries ``llm_verdict="PENDING"``
       and the caller is expected to run the (rate-limited, expensive)
       LLM verdict off the hot path and render the cue a turn later.
       The ``definite`` path is unaffected (still fires inline, no LLM).
    """
    text = (user_text or "").strip()
    if not text:
        return None
    word_count = len(text.split())
    if word_count < max(0, int(min_user_words)):
        return None
    if user_vec is None:
        return None

    eligible = _filter_opinion_memories(self_memories)
    if not eligible:
        return None

    top = _top_cosine(user_vec, eligible)
    if top is None:
        return None
    stance, score = top
    if score < float(min_cosine):
        return None

    verdict = classify_pair(text, stance.content or "")
    label = verdict.label
    if label == HEURISTIC_DEFINITE:
        return OpinionInjectionResult(
            trigger="contradiction_definite",
            stance_text=stance.content or "",
            stance_memory_id=int(stance.id),
            cosine=score,
            heuristic_label=label,
            heuristic_signals=list(verdict.signals),
            llm_verdict=None,
        )
    # ``borderline`` (numerical-mismatch) AND ``no`` paths route
    # through the LLM gate when one is available. The conservative
    # heuristic returns ``no`` for most real contradictions in
    # verbose stance memories ("I don't like X because Y" vs "I like
    # X because Z" -- Jaccard usually < 0.4 once descriptive context
    # is added), so without LLM-gating the cue would only fire on
    # very tight phrasing. The LLM prompt's "prefer NO / UNRELATED
    # when uncertain" bias is the contrarianism guardrail; the rate
    # limiter and per-session cap are the cost guardrails.
    #
    # ``require_definite=True`` is the strictest no-LLM-cost config
    # (Path C): only ``definite`` heuristic results fire, the
    # borderline + no paths stay silent. Use it when LLM budget is
    # exhausted or when you want zero contrarianism risk.
    if require_definite:
        return None
    # P21: hot-path callers defer the LLM verdict. Signal the borderline
    # candidate via a PENDING result and let the caller run the verdict
    # post-turn (rendering the cue a turn later). The detector stays pure.
    if defer_borderline:
        return OpinionInjectionResult(
            trigger="contradiction_borderline",
            stance_text=stance.content or "",
            stance_memory_id=int(stance.id),
            cosine=score,
            heuristic_label=label,
            heuristic_signals=list(verdict.signals),
            llm_verdict="PENDING",
        )
    if llm_gate is None:
        return None
    try:
        llm_answer = llm_gate(text, stance.content or "")
    except Exception:
        log.debug("opinion-injection LLM gate raised", exc_info=True)
        return None
    normalized = (llm_answer or "").strip().upper()
    if normalized != "YES":
        return None
    return OpinionInjectionResult(
        trigger="contradiction_borderline",
        stance_text=stance.content or "",
        stance_memory_id=int(stance.id),
        cosine=score,
        heuristic_label=label,
        heuristic_signals=list(verdict.signals),
        llm_verdict=normalized,
    )


# ── Render ───────────────────────────────────────────────────────────────


def render_inner_life_block(
    result: OpinionInjectionResult,
    *,
    user_display_name: str = "the user",
) -> str:
    """Render ``result`` into a system-prompt-ready block.

    The cue includes Aiko's stored stance in single quotes so the
    LLM has the prior take in front of it. The persona block
    explicitly forbids quoting the stance back at Jacob; the quote
    here is for Aiko's reading, not for echo.

    Anti-moralizing guardrail lives in the persona block ("When you
    have your own take") rather than in this render text -- the
    persona block can carry concrete bad/good pairs for the
    lifestyle (smoking / horror / late-night) failure mode, which
    the inline cue can't fit without becoming a sermon.
    """
    snippet = (result.stance_text or "").strip()
    # Trim very long stance memories so the cue stays compact in the
    # system prompt; the full content is still on the memory row.
    if len(snippet) > 180:
        snippet = snippet[:177].rstrip() + "\u2026"
    head = (
        f"Heads-up: you've got a stored stance on this and it actually "
        f"differs from what {user_display_name} just said -- you wrote: "
        f"'{snippet}'."
    )
    body = (
        "Say your take in your own register -- one sentence, your "
        "preference, not advice for him. \"ugh, that's not my favourite\" "
        "rather than \"you should change\". Don't lecture, don't apologise, "
        "don't pile on -- one line, then make room for his reply."
    )
    return f"{head}\n{body}"
