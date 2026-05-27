"""Cheap regex-based factual-claim extractor (F1 personality backlog).

Used by :class:`IdleFactChecker` to decide whether a freshly-written
memory contains anything worth fact-checking against the web. The
heuristic is deliberately conservative — false negatives (claims we
skip) cost nothing, false positives (claims we send to the LLM for
distillation) cost a Lance + Ollama roundtrip each.

The four pattern classes we care about:
  - **year** — 4-digit years in the 19xx/20xx range. Most cheap-to-verify
    claims have a year ("Python 3.12 was released in 2023") and most
    hallucinations involve incorrect years.
  - **measurement** — numeric quantities with a unit suffix.
  - **date** — slash- or dash-separated calendar dates.
  - **proper_noun** — sequences of capitalised words (names of people,
    places, products). Picks up "Saturn V" / "Yosemite National Park".

``find_claims`` returns up to ``max_claims`` spans (default 3) per
memory so a single chatty observation can't enqueue dozens of checks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_CLAIM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Years 19xx / 20xx. Bounded with word breaks so dates like
    # ``20/01/2024`` don't match twice; the date pattern handles those.
    (re.compile(r"\b(?:19|20)\d{2}\b"), "year"),
    # Measurements: number + (optional decimal) + unit. Whitelist the
    # common units we expect to see in casual chat; anything weirder
    # falls through.
    (
        re.compile(
            r"\b\d+(?:\.\d+)?\s*"
            r"(?:%|km|miles|mi|kg|kgs|lbs|°C|°F|degrees|years|year|days|day|"
            r"hours|hour|minutes|minute|seconds|second|gb|mb|tb|kb)\b",
            flags=re.IGNORECASE,
        ),
        "measurement",
    ),
    # Dates: dd/mm or dd-mm with optional year.
    (
        re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"),
        "date",
    ),
    # Proper-noun chains: 2-4 capitalised words in a row. We tolerate
    # the trailing noun being capitalised too because the chain pattern
    # is recursive on word boundaries.
    (
        re.compile(r"\b(?:[A-Z][a-z]+\s+){1,3}[A-Z][a-z]+\b"),
        "proper_noun",
    ),
]

_DEFAULT_MAX_CLAIMS = 3


@dataclass(frozen=True)
class ClaimCandidate:
    """A single span identified as fact-checkable."""

    text: str
    kind: str  # one of "year" / "measurement" / "date" / "proper_noun"
    start: int
    end: int


def find_claims(text: str, *, max_claims: int = _DEFAULT_MAX_CLAIMS) -> list[ClaimCandidate]:
    """Return up to ``max_claims`` factual spans found in ``text``.

    Identical spans (same start/end) are deduped. Spans are returned in
    document order so callers can correlate to the original sentence
    later if needed.
    """
    source = (text or "").strip()
    if not source:
        return []
    seen_spans: set[tuple[int, int]] = set()
    out: list[ClaimCandidate] = []
    for pattern, kind in _CLAIM_PATTERNS:
        for match in pattern.finditer(source):
            span = (match.start(), match.end())
            if span in seen_spans:
                continue
            seen_spans.add(span)
            out.append(
                ClaimCandidate(
                    text=match.group(0).strip(),
                    kind=kind,
                    start=match.start(),
                    end=match.end(),
                )
            )
            if len(out) >= max_claims:
                # Return early in document order to keep behaviour
                # deterministic when the cap kicks in.
                out.sort(key=lambda c: c.start)
                return out
    out.sort(key=lambda c: c.start)
    return out
