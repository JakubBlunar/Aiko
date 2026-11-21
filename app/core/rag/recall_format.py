"""Render selected recall hits and their grounding cues for the chat prompt."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.core.infra import timephrase
from app.core.rag.recall_semantics import recall_origin, recall_scope, recall_sections

if TYPE_CHECKING:
    from app.core.rag.rag_store import RagHit


# F16 (v30) — the memory kinds that carry the visible ``(inferred)``
# suffix. Durable claims *about the user* only: a conclusion Aiko drew
# about him should be voiced as an impression, not asserted as testimony.
# ``self`` notes are her own stance (their own voice),
# and ``knowledge`` / ``curiosity_finding`` already carry ``(learned)`` /
# ``(curiosity)`` hedges -- none of them ever render ``(inferred)``.
_PROVENANCE_TAGGED_KINDS = frozenset(
    {"fact", "preference", "relationship", "event", "self_tagged"}
)

# K7 — Forgetting protocol defaults. The original implementation only
# fired ``(faded)`` for ``tier=="archive"`` rows; this completion adds a
# graded predicate so long_term rows that have decayed in place
# (``salience`` below the threshold AND idle longer than ``idle_days``)
# also pick up the suffix. The 30-180 day window between "decayed" and
# "demoted to archive" was passing through with no hedge.
#
# Defaults are quiet on purpose: with the long_term decay rate of
# 0.02/day, a fresh ``salience=0.5`` row hits the 0.20 threshold around
# day 15; combined with the 30-day idle floor, only rows that genuinely
# haven't surfaced in over a month qualify. The master switch
# ``fade_hedge_enabled`` disables every ``(faded)`` suffix (including
# archive) for users who want sharp memories only.
_FADED_DEFAULT_SALIENCE_THRESHOLD = 0.20
_FADED_DEFAULT_IDLE_DAYS = 30


def _is_faded_memory(
    *,
    tier: str | None,
    salience: float | None,
    last_used_at: str | None,
    created_at: str | None,
    now: datetime,
    salience_threshold: float,
    idle_days: int,
) -> bool:
    """K7 graded fade predicate.

    Returns ``True`` when the row should pick up the ``(faded)``
    suffix. Two cases:

    - ``tier == "archive"`` — always faded (cold history that survived
      the score offset because the cosine match was strong).
    - ``tier == "long_term"`` (or missing) AND salience below the
      threshold AND last touched longer than ``idle_days`` ago — the
      slow-decay-in-place case.

    Scratchpad never fades: that tier has its own lifecycle
    (``scratchpad_ttl_days`` prunes; promotion lifts the warm ones)
    and conflating "raw new observation" with "old half-forgotten"
    muddies two different signals.
    """
    if tier == "archive":
        return True
    if tier not in (None, "long_term"):
        return False
    if salience is None:
        return False
    if float(salience) >= float(salience_threshold):
        return False
    ts = last_used_at or created_at
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    # ``timedelta.days`` floors, so a strict ``>`` reads correctly:
    # "more than N days idle" means at least N+1 calendar days.
    return (now - last).days > int(idle_days)


# ── K25: memory confidence time-decay ─────────────────────────────────
# Read-side time-decay on memory confidence with the new ``(distant)``
# suffix. Distinct from ``(uncertain)`` (which means "stored value is
# already low — claim quality was shaky at write time") and from
# ``(faded)`` (which means "low salience AND idle, so tier/use have
# decayed it"). K25's signal is **raw age**: a 6-month-old default-
# confidence claim that's been used recently is still actively
# retrieved, but Aiko should hedge with time-language ("a while back",
# "don't quote me") rather than quote it as if it were yesterday.
#
# Pure read-side derivation -- no schema change, no decay-writer.
# Each retrieval recomputes ``effective = stored * max(floor, 1 -
# days_since_created / horizon_days)``. Pinned rows bypass (return
# stored as-is) since pin == "user explicitly trusts this".
#
# Defaults at ``horizon_days=365, floor=0.3, threshold=0.5``:
#
# * default-confidence (0.7) memory hits the threshold at ~104 days
# * high-confidence (0.9) memory hits the threshold at ~165 days
# * pinned rows (>=0.9) never trigger regardless of age
#
# The storage column ``memories.confidence`` is left untouched —
# ``_confidence_penalty``, ``MemoryConflictWorker`` and
# ``BeliefGapDetector`` all keep reading the raw stored value.
_CONFIDENCE_DECAY_DEFAULT_HORIZON_DAYS = 365
_CONFIDENCE_DECAY_DEFAULT_FLOOR = 0.3
_CONFIDENCE_DECAY_DEFAULT_THRESHOLD = 0.5


def _compute_effective_confidence(
    stored: float,
    *,
    age_days: float,
    horizon_days: int,
    floor: float,
) -> float:
    """Linear-with-floor time decay on a stored confidence value.

    Multiplier ramps from ``1.0`` at age ``0`` down to ``floor`` at
    ``horizon_days``, and clamps at ``floor`` thereafter. The returned
    value is clamped to ``[0.0, 1.0]`` so downstream comparisons stay
    well-behaved regardless of how the caller stored its value.

    ``horizon_days <= 0`` short-circuits to the raw stored value so an
    accidentally-zero config never raises ZeroDivisionError. ``floor``
    is treated literally — a negative floor decays past zero (clamped),
    a floor of ``1.0`` disables decay entirely.
    """
    if horizon_days <= 0:
        return max(0.0, min(1.0, float(stored)))
    multiplier = max(float(floor), 1.0 - float(age_days) / float(horizon_days))
    return max(0.0, min(1.0, float(stored) * multiplier))


def _is_distant_memory(
    *,
    stored_confidence: float | None,
    created_at: str | None,
    now: datetime,
    horizon_days: int,
    floor: float,
    threshold: float,
    pinned: bool,
) -> bool:
    """K25 ``(distant)`` predicate.

    Returns ``True`` when the row's ``effective_confidence`` (after
    age-based decay) falls below ``threshold``. Returns ``False`` —
    no signal — when any of:

    - ``pinned`` is True (user explicitly trusts this row)
    - ``stored_confidence`` is None (cold-start / corrupted row)
    - ``created_at`` is None or unparseable (no age data to work with)

    Mirrors the defensive shape of :func:`_is_faded_memory`: every
    failure mode returns ``False`` rather than raising, since a single
    bad row should never poison the whole RAG render.
    """
    if pinned or stored_confidence is None or created_at is None:
        return False
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    age_seconds = (now - created).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)
    effective = _compute_effective_confidence(
        float(stored_confidence),
        age_days=age_days,
        horizon_days=horizon_days,
        floor=floor,
    )
    return effective < float(threshold)


# Below this age a bullet renders bare. Inside a couple of days "noted"
# is not something anyone would say out loud, and the tag is pure prompt
# cost on exactly the rows that need it least.
_RECORDED_MIN_AGE = timedelta(hours=48)


def _recorded_suffix(created_at: str | None, now: datetime) -> str:
    """``(noted N ago)`` for a memory the v10 tense tag left untagged.

    :func:`temporal_suffix` deliberately says nothing about ``durable`` /
    ``preference`` rows, on the theory that a standing fact is timeless.
    That holds for "Jacob is a software engineer" and fails badly for the
    rest: ``durable`` is the *default* temporal type, so it collects every
    one-off event the extractor never classified ("Jacob mowed the lawn
    today", stored in May). An untagged bullet is then read present-tense,
    because the persona says in as many words that no tag means fair game.

    Stamping the *recording* date is the honest fallback. It never claims
    when the thing happened -- only when Aiko learned it, which is true of
    every row regardless of type -- so it reads correctly for a standing
    fact and defuses a stale event at the same time. Deliberately worded
    "noted" rather than the bare "(3 days ago)" that
    :func:`temporal_suffix` emits for ``past_event``: those two tags mean
    different things and the persona keys off the difference.
    """
    when = timephrase.parse_iso(created_at)
    if when is None:
        return ""
    if timephrase.to_aware(now) - when < _RECORDED_MIN_AGE:
        return ""
    return f" (noted {timephrase.humanize_past(created_at, now)})"


def _today_line() -> str:
    """Date-only anchor for the head of the recall block.

    The full "right now it's ..." sentence lives at T4, which the
    assembler appends *after* this region -- so without this the model
    reads a page of dated recall before it is ever told what day it is,
    and "(noted 2 months ago)" has nothing to be two months before.

    Date only, no clock: every tag below is measured in days, and a
    ticking HH:MM would rewrite the block on every single turn to say
    nothing new.
    """
    try:
        today = timephrase.now().strftime("%A, %B %d, %Y").replace(" 0", " ")
    except Exception:  # pragma: no cover - defensive
        return ""
    return f"(Today is {today}. The time tags below are relative to it.)"


def _truncate(s: str, limit: int) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[: max(20, limit - 1)].rstrip() + "…"


def format_block(
    hits: list[RagHit],
    *,
    user_display_name: str = "the user",
    memory_provenance_enabled: bool = True,
    fade_hedge_enabled: bool = True,
    faded_salience_threshold: float = _FADED_DEFAULT_SALIENCE_THRESHOLD,
    faded_idle_days: int = _FADED_DEFAULT_IDLE_DAYS,
    confidence_time_decay_enabled: bool = True,
    confidence_decay_horizon_days: int = _CONFIDENCE_DECAY_DEFAULT_HORIZON_DAYS,
    confidence_decay_floor: float = _CONFIDENCE_DECAY_DEFAULT_FLOOR,
    confidence_decay_distant_threshold: float = _CONFIDENCE_DECAY_DEFAULT_THRESHOLD,
) -> str:
    """Render hits into a system-prompt-ready block.

    Group memories by subject and follow them with recalled messages,
    documents, topic digests and related notes. Render only non-empty sections.

    F16 — ``memory_provenance_enabled`` (default on) gates the
    ``(inferred)`` suffix appended to durable user-fact hits
    (fact/preference/relationship/event) whose ``memory_provenance``
    is ``'inferred'``, so Aiko voices a conclusion she drew as an
    impression. ``'stated'`` is the unmarked default. The tie-break
    ranking nudge is applied earlier in ``retrieve`` and is not gated
    here.

    K7 — ``fade_hedge_enabled`` / ``faded_salience_threshold`` /
    ``faded_idle_days`` control the ``(faded)`` suffix. Defaults
    preserve the original archive-only behaviour with the graded
    long_term predicate enabled; flip ``fade_hedge_enabled=False``
    to silence every fade hedge including archive.

    K25 — ``confidence_time_decay_enabled`` plus the three
    ``confidence_decay_*`` knobs control the ``(distant)`` suffix.
    Computes ``effective_confidence = stored * max(floor, 1 -
    days_since_created / horizon_days)`` and stamps the row when
    the result drops below ``distant_threshold``. Pinned rows
    bypass. Disabled-by-default-but-on-here master switch lets a
    user kill the suffix without disabling K7.
    """
    if not hits:
        return ""
    memory_lines: dict[str, list[str]] = {}
    message_lines: list[str] = []
    document_lines: list[str] = []
    expansion_lines: list[str] = []
    digest_lines: list[str] = []
    now = timephrase.utcnow()
    for hit in hits:
        text = (hit.text or "").strip()
        if (getattr(hit.record, "kind", "") in {"shared_term", "strategy"}
                and getattr(hit, "memory_provenance", "") != "validated_pattern"):
            continue
        if getattr(hit.record, "kind", "") in {"shared_term", "strategy"}:
            memory_lines.setdefault("relationship", []).append(
                "- [validated shared pattern] " + text
            )
            continue
        if not text:
            continue
        if hit.source == "memory":
            kind = (getattr(hit.record, "kind", "") or "").lower()
            if kind == "topic_digest":
                stamp = _recorded_suffix(getattr(hit.record, "created_at", None), now)
                digest_lines.extend(f"- {clause}{stamp}" for clause in text.splitlines())
                continue
            # Suffix tags. Order matters: "(uncertain)" first so
            # confidence reads before provenance.
            suffix_tags: list[str] = []
            # Schema v9: append "(uncertain)" so the LLM hedges when
            # the underlying memory has a low confidence score (the
            # F1 fact-checker may have flagged it, or it never had
            # a high-confidence source to begin with).
            confidence = getattr(hit, "confidence", None)
            if confidence is not None and float(confidence) < 0.5:
                suffix_tags.append("(uncertain)")
            # F16 (v30): append "(inferred)" on durable user-fact kinds
            # so Aiko phrases a conclusion she drew as an impression
            # ("I get the sense…") rather than asserting the user said
            # it. ``stated`` is the unmarked default (no tag on the
            # majority), and self-notes / distilled knowledge / findings
            # keep their own voice (see ``_PROVENANCE_TAGGED_KINDS``).
            # Gated by the ``memory_provenance_enabled`` master switch.
            if (
                memory_provenance_enabled
                and kind in _PROVENANCE_TAGGED_KINDS
                and (getattr(hit, "memory_provenance", None) or "")
                .strip()
                .lower()
                == "inferred"
            ):
                suffix_tags.append("(inferred)")
            # K25 — append "(distant)" when the row's effective
            # confidence (after age-based decay) drops below the
            # threshold. Distinct from "(uncertain)" — that's the
            # "stored value was already low" hedge; "(distant)" is
            # the "raw age" hedge for actively-used rows whose
            # stored confidence is fine but they're old enough that
            # Aiko shouldn't quote them as if they were yesterday.
            # Pinned rows bypass via the helper. Order: lands
            # after "(uncertain)" so when both stack the LLM reads
            # the stored-doubt cue first, then the time cue.
            if confidence_time_decay_enabled and _is_distant_memory(
                stored_confidence=confidence,
                created_at=getattr(hit.record, "created_at", None),
                now=now,
                horizon_days=confidence_decay_horizon_days,
                floor=confidence_decay_floor,
                threshold=confidence_decay_distant_threshold,
                pinned=bool(getattr(hit, "memory_pinned", False)),
            ):
                suffix_tags.append("(distant)")
            # G3: append "(curiosity)" so the persona rule can
            # surface findings as "I was reading about X — turns
            # out…" rather than reciting them as bare facts. The
            # tag is invisible to the user; only the LLM ever
            # sees it.
            if kind == "curiosity_finding":
                suffix_tags.append("(curiosity)")
            # F8 — append "(learned)" so the persona rule lets Aiko
            # surface a distilled fact she picked up between sessions
            # naturally ("oh — try Slowdive") rather than reciting it
            # like a textbook. Invisible to the user; only the LLM
            # sees it. Sibling of the G3 "(curiosity)" tag.
            if kind == "knowledge":
                suffix_tags.append("(learned)")
            # K11 — pre-thought / counterfactual cache. Tag drafts
            # so the persona rule lets Aiko lean on a reply she
            # already mulled ("I actually thought about this…")
            # rather than treat it as a recalled fact. Invisible to
            # the user; only the LLM sees it.
            if kind == "pre_thought":
                suffix_tags.append("(pre-thought)")
            # K7 — Forgetting protocol. Graded fade predicate
            # covers both archive-tier rows (cold history) AND
            # long_term rows that have decayed in place
            # (salience below threshold AND idle longer than
            # ``faded_idle_days``). The 30-180 day window between
            # "decayed" and "archive demoted" used to pass
            # through with no hedge — closing that gap is what
            # the K7 completion adds. The ``fade_hedge_enabled``
            # master switch lets a user disable every fade
            # suffix (including archive) for a sharper feel.
            if fade_hedge_enabled and _is_faded_memory(
                tier=getattr(hit, "memory_tier", None),
                salience=getattr(hit.record, "salience", None),
                last_used_at=getattr(hit.record, "last_used_at", None),
                created_at=getattr(hit.record, "created_at", None),
                now=now,
                salience_threshold=faded_salience_threshold,
                idle_days=faded_idle_days,
            ):
                suffix_tags.append("(faded)")
            suffix = (" " + " ".join(suffix_tags)) if suffix_tags else ""
            # Schema v10: append the temporal time-tag (e.g.
            # "(yesterday)", "(planned for tonight 20:00)",
            # "(ongoing)") so Aiko reads the memory at the right
            # tense. Empty for durable/preference rows so the
            # output stays identical to pre-v10 for legacy /
            # timeless memories.
            time_suffix = timephrase.temporal_suffix(
                temporal_type=getattr(hit, "temporal_type", None),
                event_time=getattr(hit, "event_time", None),
                created_at=getattr(hit.record, "created_at", None),
                now=now,
                precision=getattr(hit, "event_time_precision", None),
            )
            # K-time10: a row the tense tag skipped still gets an
            # anchor, so nothing reaches the prompt claiming to be
            # about now purely because nobody classified it.
            if not time_suffix:
                time_suffix = _recorded_suffix(
                    getattr(hit.record, "created_at", None), now,
                )
            scoped = recall_scope(hit)
            if getattr(hit, "expansion", False):
                # Keep qualifications intact; the context selector can
                # omit a whole oversized record rather than cutting it.
                if len(text) > 1200:
                    continue
                line = f"- {scoped}{text}{suffix}{time_suffix}"
                expansion_lines.append(line)
            else:
                subject = recall_origin(hit).subject
                memory_lines.setdefault(subject, []).append(
                    f"- {scoped}{text}{suffix}{time_suffix}"
                )
        elif hit.source == "message":
            role = (getattr(hit.record, "role", "") or "").lower()
            speaker = (
                f"{user_display_name} said" if role == "user" else "You said"
            )
            # K-time10: a recalled snippet used to render with no
            # timestamp at all, so a line from three weeks ago looked
            # exactly like one from this morning. The K-time1 bracket
            # is reused deliberately -- the persona already explains
            # that register for history messages, so this needs no
            # new vocabulary taught.
            age = timephrase.age_prefix(getattr(hit.record, "created_at", None), now)
            stamp = f"[{age}] " if age else ""
            message_lines.append(
                f'- {stamp}{speaker}: "{_truncate(text, 200)}"'
            )
        elif hit.source == "document":
            title = getattr(hit.record, "title", "")
            head = f"({title}) " if title else ""
            document_lines.append(f"- {head}{_truncate(text, 240)}")
    sections = recall_sections(memory_lines, user_display_name)
    if message_lines:
        sections.append(
            "Snippets you remembered from past chats:\n"
            + "\n".join(message_lines)
        )
    if document_lines:
        sections.append("From your notes:\n" + "\n".join(document_lines))
    if digest_lines:
        sections.append(
            "Topic summaries (check the cited memories for details):\n"
            + "\n".join(digest_lines)
        )
    if expansion_lines:
        sections.append(
            "Related notes from the same topic (you've circled around this "
            "before — lean on them only if they fit naturally):\n"
            + "\n".join(expansion_lines)
        )
    if not sections:
        return ""
    today_line = _today_line()
    if today_line:
        sections.insert(0, today_line)
    return "\n\n".join(sections)
