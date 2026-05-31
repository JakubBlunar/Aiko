"""Privacy gate for the F1 background fact-checker.

The fact-checker sends claim text to a **public** search engine
(DuckDuckGo) and the resulting snippets to the local LLM. This module is
the policy layer that decides which claims are safe to send out of the
box.

Two checkpoints are exposed:

1. :func:`classify_memory_for_fact_check` — called at enqueue time.
   Inspects the memory's ``kind`` and ``content`` and returns a verdict
   plus reason. Memories deemed personal never get a queue entry, so
   nothing about them can leak to the web.

2. :func:`scrub_claim_for_search` — called at search time as
   belt-and-braces. Returns a redacted, search-safe variant of the
   claim text, or ``None`` when even the redacted version would leak
   personal context (e.g. the only words in the claim are the user's
   name or first-person pronouns).

Both helpers are conservative by design. False negatives (claims we
skip) cost nothing — the memory simply doesn't get fact-checked. False
positives (claims that slip through) leak personal data to a
third-party search engine, which is the failure mode this module
exists to prevent.

Threat model:

  * The local LLM and the local Ollama runtime are trusted; everything
    stays on-device.
  * The web-search backend (DDGS over HTTPS) is **not** trusted with
    personal information. Search queries are visible to the engine
    operator and any on-path observer of the TLS endpoint.
  * The chat agent's main context is also trusted (it already saw all
    of this content when the memory was written). The privacy gate
    only protects the *outbound web search*.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable


log = logging.getLogger("app.fact_check_privacy")


# Maximum chars of raw memory / claim content we render in a single
# log line. Privacy auditing wants to see *what* was checked so the
# rules can be tightened, but a long memory blob would clobber the
# rotating log. ``data/app.log`` is local-only — the truncation is
# purely about line readability, not a leak boundary.
_LOG_PREVIEW_CHARS = 160


def _preview(text: str | None) -> str:
    """Return a single-line, length-bounded preview of ``text``.

    Newlines collapse to spaces so every log entry stays on one line
    (cheap grep-ability). Empty / None input produces ``"<empty>"`` so
    a missing field is obvious in the audit trail.
    """
    if not text:
        return "<empty>"
    flat = " ".join(str(text).split())
    if len(flat) > _LOG_PREVIEW_CHARS:
        return flat[: _LOG_PREVIEW_CHARS - 1] + "…"
    return flat


# ── memory kinds that are inherently personal ───────────────────────────
#
# These kinds describe the user (or the user-assistant relationship)
# rather than facts about the world. Even if the claim extractor pulls
# a clean-looking span out of one of these, the *context* is personal,
# so we refuse to fact-check them.
_PERSONAL_KINDS: frozenset[str] = frozenset(
    {
        "self",
        "self_tagged",
        "promise",
        "shared_moment",
        "user_state",
        "user_profile",
        "relationship",
        "agenda",
    }
)


# ── PII pattern catalogue ───────────────────────────────────────────────
#
# Email / phone / URL detection is well-trodden ground. We keep
# patterns conservative (favouring recall over precision) since the
# downstream cost of a false positive is just "don't fact-check this
# claim" rather than anything destructive.

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE_RE = re.compile(r"(?<![\w@])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}(?!\w)")
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"']{4,}\b", flags=re.IGNORECASE)
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_COORDS_RE = re.compile(r"\b-?\d{1,3}\.\d+\s*[,\s]\s*-?\d{1,3}\.\d+\b")
# Cheap street-address sniffer ("123 Main Street", "5 Rue de la Paix").
_STREET_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){0,4}\s+"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Court|Ct|Plaza|Square|Sq|Terrace|Place|Pl|Way|Highway|Hwy|"
    r"Route|Rue|Strasse|Straße|Allee)\b",
    flags=re.IGNORECASE,
)


# First-person pronouns + possessives that signal "this is about the
# user / the assistant" rather than a public fact. Plain "you" and
# "your" are also included because a claim like "you live in Berlin"
# is just as personal as one with "I".
_FIRST_PERSON_TOKENS: frozenset[str] = frozenset(
    {
        "i", "me", "my", "mine", "myself",
        "we", "us", "our", "ours", "ourselves",
        "you", "your", "yours", "yourself", "yourselves",
    }
)


# Tokens we always strip out of the search query because they describe
# private temporal references (the search engine can't verify
# "yesterday" anyway and they reveal session timing).
_PRIVATE_TIME_TOKENS: frozenset[str] = frozenset(
    {"yesterday", "today", "tonight", "tomorrow", "earlier", "later"}
)


# Minimum length of a scrubbed claim. Anything shorter is treated as
# semantically empty (e.g. only the user's name was in the original).
_MIN_SAFE_CLAIM_CHARS = 8


@dataclass(frozen=True)
class PrivacyDecision:
    """Outcome of :func:`classify_memory_for_fact_check`."""

    personal: bool
    reason: str  # ``""`` when ``personal`` is False


# ── helpers ─────────────────────────────────────────────────────────────


def _name_tokens(names: Iterable[str] | None) -> list[str]:
    """Lower-cased, length>=2 name tokens.

    We split each provided name on whitespace so "Jacob Smith" yields
    {"jacob", "smith"} and either alone trips the gate.
    """
    out: list[str] = []
    if not names:
        return out
    for raw in names:
        if not raw:
            continue
        for tok in str(raw).split():
            cleaned = tok.strip().lower()
            if len(cleaned) >= 2 and cleaned.isalpha():
                out.append(cleaned)
    return out


def _contains_word(text: str, words: Iterable[str]) -> bool:
    """True when any of ``words`` appears as a whole word in ``text``."""
    if not words:
        return False
    lower = text.lower()
    for w in words:
        if not w:
            continue
        pattern = r"\b" + re.escape(w.lower()) + r"\b"
        if re.search(pattern, lower):
            return True
    return False


# ── public API ──────────────────────────────────────────────────────────


def classify_memory_for_fact_check(
    *,
    kind: str,
    content: str,
    user_names: Iterable[str] | None = None,
    assistant_name: str | None = None,
) -> PrivacyDecision:
    """Decide whether a freshly-written memory may be fact-checked.

    Returns a :class:`PrivacyDecision` with ``personal=True`` when the
    memory should be skipped. The ``reason`` field is intended for
    logs/debugging and never user-facing.

    Logs every decision so a privacy audit can replay what was sent
    where: blocks land at INFO (rare, important to see), allows land
    at DEBUG (one per memory write — high volume).
    """
    kind_norm = (kind or "").strip().lower()
    text = (content or "").strip()

    decision = _classify_inner(
        kind_norm=kind_norm,
        text=text,
        user_names=user_names,
        assistant_name=assistant_name,
    )

    if decision.personal:
        log.info(
            "privacy classify BLOCK kind=%s reason=%s preview=%r",
            kind_norm or "<missing>",
            decision.reason,
            _preview(text),
        )
    else:
        log.debug(
            "privacy classify ALLOW kind=%s preview=%r",
            kind_norm or "<missing>",
            _preview(text),
        )
    return decision


def _classify_inner(
    *,
    kind_norm: str,
    text: str,
    user_names: Iterable[str] | None,
    assistant_name: str | None,
) -> PrivacyDecision:
    """Pure decision logic split out from :func:`classify_memory_for_fact_check`.

    Kept private because the public wrapper owns the audit-log line
    that fires for every decision; callers should always go through
    the wrapper so logs stay consistent.
    """
    if kind_norm in _PERSONAL_KINDS:
        return PrivacyDecision(True, f"personal_kind:{kind_norm}")

    if not text:
        return PrivacyDecision(True, "empty_content")

    # The PII catalogue is the same for memories and claims — anything
    # matching here is so clearly personal that we refuse outright.
    if _EMAIL_RE.search(text):
        return PrivacyDecision(True, "email")
    if _URL_RE.search(text):
        return PrivacyDecision(True, "url")
    if _PHONE_RE.search(text):
        return PrivacyDecision(True, "phone")
    if _IPV4_RE.search(text):
        return PrivacyDecision(True, "ipv4")
    if _COORDS_RE.search(text):
        return PrivacyDecision(True, "coordinates")
    if _STREET_ADDRESS_RE.search(text):
        return PrivacyDecision(True, "street_address")

    if _contains_word(text, _FIRST_PERSON_TOKENS):
        return PrivacyDecision(True, "first_person_pronoun")

    name_tokens = _name_tokens(user_names)
    if _contains_word(text, name_tokens):
        return PrivacyDecision(True, "user_name")

    if assistant_name:
        if _contains_word(text, [assistant_name]):
            return PrivacyDecision(True, "assistant_name")

    return PrivacyDecision(False, "")


def scrub_claim_for_search(
    claim_text: str,
    *,
    user_names: Iterable[str] | None = None,
    assistant_name: str | None = None,
) -> str | None:
    """Return a search-safe variant of ``claim_text``.

    The output is suitable for handing to a third-party search engine.
    Returns ``None`` when the claim cannot be made safe — either it
    only contained personal tokens, or the PII detectors found something
    we can't redact without losing too much meaning.

    Redaction rules:

    * URLs / emails / phone numbers / IPs / coords / street addresses
      → bail out entirely (``None``). These usually point at a single
      person and have no fact-checkable surface.
    * User / assistant names → drop those tokens (rather than placeholder
      them, since search engines treat ``<user>`` as a literal query
      token). What survives is the rest of the claim, which may still
      be checkable (e.g. "violin practice since 2010").
    * First-person pronouns / private time tokens → drop them.
    * After redaction, the result must contain at least one non-tokenized
      alphabetic word and be ``_MIN_SAFE_CLAIM_CHARS`` characters or
      more.
    """
    text = (claim_text or "").strip()
    if not text:
        log.info("privacy scrub BLOCK reason=empty_claim")
        return None

    # Hard rejects — no safe redaction. Logged at INFO so the
    # privacy audit shows every refused claim with the reason; the
    # text preview goes alongside so we can tighten patterns later
    # without re-running the original write path.
    for matcher, reason in (
        (_EMAIL_RE, "email"),
        (_PHONE_RE, "phone"),
        (_URL_RE, "url"),
        (_IPV4_RE, "ipv4"),
        (_COORDS_RE, "coordinates"),
        (_STREET_ADDRESS_RE, "street_address"),
    ):
        if matcher.search(text):
            log.info(
                "privacy scrub BLOCK reason=%s preview=%r",
                reason,
                _preview(text),
            )
            return None

    # Build the token list of words to strip out: names + private time
    # markers + first-person pronouns. We rebuild the string by
    # filtering tokens so the structure stays intact (search engines
    # do better with natural word order).
    strip_tokens = set(_FIRST_PERSON_TOKENS) | set(_PRIVATE_TIME_TOKENS)
    for tok in _name_tokens(user_names):
        strip_tokens.add(tok)
    if assistant_name:
        for tok in _name_tokens([assistant_name]):
            strip_tokens.add(tok)

    # Split on whitespace while preserving punctuation-adjacent words.
    # ``re.findall`` of word-or-non-word chunks would over-fragment;
    # a simple split + per-token clean is good enough for short claims.
    dropped_tokens: list[str] = []
    out_tokens: list[str] = []
    for raw_token in text.split():
        # Strip surrounding punctuation for the comparison only.
        bare = re.sub(r"^\W+|\W+$", "", raw_token).lower()
        if bare in strip_tokens:
            dropped_tokens.append(bare)
            continue
        out_tokens.append(raw_token)

    scrubbed = " ".join(out_tokens).strip()
    # Collapse any double spaces introduced by missing tokens.
    scrubbed = re.sub(r"\s{2,}", " ", scrubbed)

    if len(scrubbed) < _MIN_SAFE_CLAIM_CHARS:
        log.info(
            "privacy scrub BLOCK reason=too_short_after_redaction "
            "preview=%r dropped=%s",
            _preview(text),
            dropped_tokens,
        )
        return None

    # Require at least one alphabetic word survives so a bare year /
    # date doesn't make it through ("2023" on its own is not a
    # checkable claim).
    if not re.search(r"[A-Za-z]{3,}", scrubbed):
        log.info(
            "privacy scrub BLOCK reason=no_alpha_word "
            "preview=%r dropped=%s",
            _preview(text),
            dropped_tokens,
        )
        return None

    if dropped_tokens:
        # Redaction actually fired — INFO-log so the audit captures
        # every claim where private tokens were removed before the
        # claim went out to the search engine.
        log.info(
            "privacy scrub REDACT in=%r out=%r dropped=%s",
            _preview(text),
            _preview(scrubbed),
            dropped_tokens,
        )
    else:
        # Pass-through. DEBUG because this is the normal high-volume
        # path; the audit can opt in by lowering the level.
        log.debug(
            "privacy scrub PASS in=%r out=%r",
            _preview(text),
            _preview(scrubbed),
        )
    return scrubbed
