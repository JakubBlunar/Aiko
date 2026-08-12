"""L2 near-duplicate consolidation: fuse paraphrase-twin concepts.

Creation-time dedup (the synthesis worker's ``reinforces_id`` path plus its
``_DEDUPE_COS`` label-embedding guard) stops anything at / above the dedup
cosine from ever becoming two rows. But paraphrase twins that land just
*below* that bar -- or that formed when the twin wasn't in the proposer's
shown list -- accumulate with no retroactive fix, so the ``active`` set
slowly fills with restatements of the same belief (heaviest in
``identity/user``). This worker is that retroactive fix.

**Shape.** An idle-scheduler worker on its own interval. Each tick stacks
the active set once and finds *every* same-``(subject, kind)`` pair whose
cosine clears ``concept_consolidation_merge_cosine``, worst (most similar)
first. Such a pair is a *candidate*, never a decision: pure cosine can't
tell a true paraphrase from a template collision ("X energizes him" vs
"Y energizes him"), so a maintenance-tier LLM adjudicates whether the two
are genuinely the same belief. Only on a ``same`` verdict does it merge (via
:meth:`ConceptStore.merge_into`, which folds the weaker row's evidence into
the stronger and deletes it).

A zero-LLM path exists above ``concept_consolidation_auto_merge_cosine`` but
**ships disabled**, because measuring it found template collisions all the
way up to 0.90 -- see that setting in
:mod:`~app.core.infra.memory_settings` for the numbers. Merging here is
destructive where the creation guard's equivalent is merely reinforcing, so
the two cannot share a threshold.

**Bounded + safe.** LLM adjudications are capped per hour / day by a
dedicated :class:`FactCheckRateLimiter` (its own ``state_key`` so it never
shares budget with L9 / L15 / F5), and rejected pairs are remembered so the
budget isn't re-spent re-asking about them. The worker never mutates
``confidence`` / ``plasticity`` / ``status`` -- it always keeps the stronger
row as canonical, so the single-writer L3 lifecycle engine stays the only
writer of those fields.

L46: what the first six weeks of real use showed
------------------------------------------------
Three separate ceilings kept this from converging, and the graph grew a
147-pair unfused backlog under it.

*Discovery* walked ``list_stalest(40)`` and kept only each seed's single
strongest neighbour, so a tick could see at most forty pairs -- and its
cursor was ``last_lifecycle_at``, a column only the L3 worker writes, so
consolidation could not advance its own position and re-derived roughly the
same forty every fifteen minutes. It now stacks the active set once and
does one matmul per ``(subject, kind)`` block, which measured well under a
second over 975 concepts.

*Budget* went entirely on questions already answered. The rejection cache
lived in process memory with a six-hour TTL, so every restart re-litigated
the same template collisions out of the same thirty-a-day allowance; the
live rate state showed it exhausted by 04:00 and denied for the following
eighteen hours. Verdicts now persist, keyed on the pair *and* a digest of
both labels so an L17 relabel invalidates the answer instead of freezing it.

*The bar itself* had a hole above it. ``find_duplicate`` runs against the
graph as it stood at proposal time, and labels move afterwards, so eighteen
pairs had drifted to or past 0.86 -- above the creation bar -- and no path
existed to notice. Those merge with no adjudication now, which is also what
frees the budget for the ambiguous band the LLM is actually needed for.

H16: one bar cannot fit every kind
----------------------------------
The bar had a second hole, under it. 0.84 turns out to sit above the 99th
percentile of in-block cosine for 17 of the graph's 19 ``(subject, kind)``
blocks, and for ``tension/relationship`` it admits **zero** of 406 pairs --
against 28 rows a reader sees as roughly five frictions restated. The
adjudicator was never saying no to those; it was never being asked.

Label shape is why. A tension is two clauses ("X seeks A, yet I value B")
and the longest label in the register, so restating one clause moves only
half the vector: two rows opening with an identical sentence reach 0.851
while two unrelated frictions reach 0.846. No absolute number separates
those, and one tuned on single-topic labels is far stricter than intended
for a kind like this.

So ``merge_cosine`` became the ceiling and each block now also nominates
its own top few pairs from the band beneath it -- the same H23 move of
ranking within the corpus instead of trusting an absolute bar to be sited
right for every distribution. Within the band, metas that **share a base
concept** rank first: two tensions standing on the same underlying belief
are the same friction whatever words they wear, which is the one judgement
cosine reliably cannot make here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from app.core.concepts.concept_event_store import ConceptEvent
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count

if TYPE_CHECKING:
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter

log = logging.getLogger("app.concept_consolidation_worker")

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_TEMPERATURE = 0.0
_MAX_TOKENS = 200
# How long a rejected ("not the same belief") pair stays remembered so the
# LLM budget isn't re-spent re-asking about it.
#
# Was six hours, in a dict that died with the process. Two facts made that
# the dominant drain on a thirty-a-day budget: the pairs that get rejected
# are template collisions, which are *stable* (the same two labels will
# collide next week and the week after), and a restart wiped the answers
# entirely. A month is long enough that a stable rejection is paid for once,
# and the label digest below -- not the clock -- is what re-opens a pair
# whose wording actually changed.
_VERDICT_TTL_SECONDS = 30 * 86400.0
# Cap on the remembered-verdict map so it can't grow unbounded; entries
# closest to expiry are dropped first past this size.
_VERDICT_MAX = 2000
# ``kv_meta`` key the verdict map is persisted under.
_VERDICT_KEY = "concept_consolidation.verdicts"
# Ceiling on one block's pair scan. Pair count is quadratic in block size,
# and a block is one ``(subject, kind)`` -- ``identity/user`` is the biggest
# at 125, i.e. ~7.7k pairs, so this is slack rather than a real constraint.
# It exists so that a graph an order of magnitude larger degrades by
# skipping its worst block rather than by stalling the tick.
_MAX_BLOCK = 1500


class ConceptConsolidationWorker:
    """IdleWorker: LLM-adjudicated near-duplicate concept fusion."""

    name = "concept_consolidation"

    def __init__(
        self,
        *,
        concept_store: "ConceptStore",
        memory_settings: Any,
        agent_settings: Any,
        ollama: Any,
        chat_model: str | None = None,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: Any = None,
        concept_event_store: "ConceptEventStore | None" = None,
        graph_mature_provider: Callable[[], bool] | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = concept_store
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._events = concept_event_store
        self._graph_mature_provider = graph_mature_provider
        self._user_name_provider = user_display_name_provider
        self._assistant_name_provider = assistant_display_name_provider
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._clock = clock or timephrase.utcnow
        # ``"loid:hid"`` -> ``{"at": expiry iso, "sig": label digest}`` for
        # pairs the LLM has already called *not* the same belief. Loaded
        # from kv on first use; ``None`` means "not loaded yet", which is
        # distinct from "loaded and empty".
        self._verdicts: dict[str, dict[str, str]] | None = None
        self._verdicts_dirty = False

    # ── idle worker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "concept_consolidation_interval_seconds",
                900,
            )
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> bool:
        # Feature flag only; the interval became the heartbeat (P36).
        return self._enabled()

    def demand(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> "WorkSignal | None":
        """Are there near-duplicate pairs left to act on?

        Reuses :meth:`_collect_pairs` against the in-memory mirror, which
        is one stack plus a matmul per block and no LLM. Pairs with a live
        rejection verdict are excluded, so a graph the worker has fully
        adjudicated reports zero pressure and stops burning a tick every
        fifteen minutes to rediscover that.

        ``needs_llm`` is false when every fresh pair is above the auto-merge
        bar: those cost no tokens, and claiming otherwise would park the
        work behind whatever gate the scheduler puts in front of LLM
        workers for a run that never calls one.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        if not self._graph_mature():
            return WorkSignal(pressure=0.0, reason="immature_graph")
        merge_cos = self._fl("concept_consolidation_merge_cosine", 0.84)
        auto_cos = self._auto_merge_cosine()
        try:
            pairs = self._collect_pairs(merge_cos, {})
        except Exception:
            log.debug(
                "concept_consolidation: demand probe failed", exc_info=True,
            )
            return None
        auto = 0
        needs_llm = 0
        for cos, a, b in pairs:
            if cos >= auto_cos:
                auto += 1
            elif not self._rejected(a, b):
                needs_llm += 1
        fresh = auto + needs_llm
        return WorkSignal(
            pressure=pressure_from_count(fresh, saturation=5),
            reason=f"{fresh} candidate pairs ({auto} auto-mergeable)",
            needs_llm=needs_llm > 0,
        )

    def _enabled(self) -> bool:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return False
        return bool(
            getattr(
                self._memory_settings,
                "concept_consolidation_enabled",
                True,
            )
        )

    def _graph_mature(self) -> bool:
        """Only consolidate once the topic graph has cleared the L21
        maturity floor (same gate the L2/L3 workers use). No provider
        wired => treat as mature so lean / test deployments still run."""
        provider = self._graph_mature_provider
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception:
            log.debug("graph_mature_provider raised", exc_info=True)
            return True

    # ── config knobs ──────────────────────────────────────────────────

    def _i(self, name: str, default: int) -> int:
        return int(getattr(self._memory_settings, name, default))

    def _fl(self, name: str, default: float) -> float:
        return float(getattr(self._memory_settings, name, default))

    # ── run ────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        if not self._graph_mature():
            return {"skipped": True, "reason": "immature_graph"}

        now = self._clock()
        started = time.monotonic()
        self._evict_expired(now)

        batch_size = max(1, self._i("concept_consolidation_batch_size", 40))
        merge_cos = self._fl("concept_consolidation_merge_cosine", 0.84)
        auto_cos = self._auto_merge_cosine()

        stats: dict[str, Any] = {
            "scanned": 0,
            "pairs_considered": 0,
            "banded": 0,
            "auto_merged": 0,
            "adjudicated": 0,
            "merged": 0,
            "rate_limited": False,
        }

        pairs = self._collect_pairs(merge_cos, stats)
        # Highest-cosine (most-likely-dup) pairs first, so a tight LLM
        # budget is spent on the strongest candidates.
        pairs.sort(key=lambda p: p[0], reverse=True)
        pairs = pairs[:batch_size]

        # A merge deletes the absorbed row, so any later pair naming it is
        # about a concept that no longer exists. Skipping those is not just
        # tidiness: ``merge_into`` would refuse the missing id and the pair
        # would still have cost a rate-limiter token to discover that.
        gone: set[int] = set()

        for cos, a, b in pairs:
            if a.concept_id in gone or b.concept_id in gone:
                continue
            # Off by default; see :meth:`_auto_merge_cosine`.
            if cos >= auto_cos:
                absorbed = self._merge(a, b, cos, "at or above the dedup bar")
                if absorbed is not None:
                    stats["auto_merged"] += 1
                    gone.add(absorbed)
                continue
            if self._rejected(a, b):
                continue
            # The LLM adjudication is the real work unit -> spend a token.
            if not self._rate_limiter.allow(now):
                stats["rate_limited"] = True
                break
            stats["adjudicated"] += 1
            same, reason = self._adjudicate(a, b)
            if not same:
                self._remember_rejection(a, b, now)
                continue
            absorbed = self._merge(a, b, cos, reason)
            if absorbed is not None:
                stats["merged"] += 1
                gone.add(absorbed)

        self._flush_verdicts()
        stats["duration_ms"] = int((time.monotonic() - started) * 1000)
        log.info(
            "concept_consolidation run: scanned=%s pairs=%s (banded=%s) "
            "auto_merged=%s adjudicated=%s merged=%s rate_limited=%s "
            "duration_ms=%s",
            stats["scanned"],
            stats["pairs_considered"],
            stats["banded"],
            stats["auto_merged"],
            stats["adjudicated"],
            stats["merged"],
            stats["rate_limited"],
            stats["duration_ms"],
        )
        return stats

    def _auto_merge_cosine(self) -> float:
        """The bar above which a pair merges without adjudication.

        ``1.0`` -- disabled -- is the shipped default, so normally nothing
        takes this path. It was meant to default to the creation-time dedup
        bar on the theory that both paths mean the same thing by "the same
        belief"; measurement said otherwise, and the asymmetry is why (a
        false positive reinforces at creation and destroys here). Kept
        configurable for a graph whose labels are less templated. Never
        falls below the candidate bar -- ``memory_settings`` floors it there
        on load, since an auto bar under the candidate bar would fuse
        everything the scan found.
        """
        return self._fl("concept_consolidation_auto_merge_cosine", 1.0)

    # ── candidate generation ──────────────────────────────────────────

    def _collect_pairs(
        self, merge_cos: float, stats: dict[str, Any]
    ) -> list[tuple[float, "Concept", "Concept"]]:
        """Candidate pairs: everything above the bar, plus each block's own
        worst offenders in the band beneath it.

        One :meth:`ConceptStore.matrix_snapshot` for the whole active set,
        then a matmul per ``(subject, kind)`` block over row slices of that
        one matrix. Both halves matter. Stacking once is what keeps this off
        the per-call ``_filtered_matrix`` path that took the old
        ``demand()`` probe down with an access violation; blocking by
        ``(subject, kind)`` is what keeps the cost near ``sum(n_block^2)``
        instead of ``n_total^2``, which at 975 actives is ~19k pair
        comparisons rather than 475k.

        **The band (H16).** ``merge_cos`` alone assumes every kind's
        similarity distribution sits in the same place. It does not: 0.84
        is above the 99th percentile for 17 of 19 blocks in the live graph,
        and admits zero of ``tension/relationship``'s 406 pairs even though
        those 28 rows are about five frictions restated. So each block also
        nominates its top ``block_top_n`` pairs above a looser floor, which
        lets a compressed distribution contribute without dragging the bar
        down for kinds that don't need it. Ranking inside the band prefers
        metas that **share a base concept**: two tensions built on the same
        underlying belief are the same friction however differently they
        are worded, and that is exactly the judgement cosine cannot make
        here.
        """
        actives = self._store.list_by(status="active")
        ids, mat = self._store.matrix_snapshot(
            [c.concept_id for c in actives]
        )
        stats["scanned"] = len(ids)
        if len(ids) < 2 or mat.size == 0:
            stats["pairs_considered"] = 0
            return []
        row_of = {cid: i for i, cid in enumerate(ids)}
        by_block: dict[tuple[str, str], list["Concept"]] = {}
        for concept in actives:
            if concept.concept_id not in row_of:
                continue
            by_block.setdefault(
                (concept.subject, concept.kind), []
            ).append(concept)

        top_n = max(0, self._i("concept_consolidation_block_top_n", 3))
        floor = min(
            merge_cos, self._fl("concept_consolidation_candidate_floor", 0.78)
        )
        bases = self._base_map(actives) if top_n else {}

        pairs: list[tuple[float, "Concept", "Concept"]] = []
        banded = 0
        for (subject, kind), members in by_block.items():
            if len(members) < 2:
                continue
            if len(members) > _MAX_BLOCK:
                log.warning(
                    "concept_consolidation: %s/%s block of %s exceeds the "
                    "pair-scan ceiling; skipped",
                    kind, subject, len(members),
                )
                continue
            rows = np.array(
                [row_of[c.concept_id] for c in members], dtype=np.intp
            )
            block = mat[rows]
            sims = block @ block.T
            # Upper triangle only: the matrix is symmetric and the diagonal
            # is each concept against itself.
            hi, lo = np.triu_indices(len(members), k=1)
            vals = sims[hi, lo]
            for h in np.nonzero(vals >= merge_cos)[0]:
                i, j = int(hi[h]), int(lo[h])
                pairs.append(
                    (float(sims[i, j]), members[i], members[j])
                )
            if not top_n:
                continue
            band = np.nonzero((vals >= floor) & (vals < merge_cos))[0]
            if band.size == 0:
                continue
            # Shared-base pairs first, then by cosine. Both keys descend,
            # so the block's most-likely twins are what the budget buys.
            ranked = sorted(
                (int(h) for h in band),
                key=lambda h: (
                    self._shares_base(
                        bases, members[int(hi[h])], members[int(lo[h])]
                    ),
                    float(vals[h]),
                ),
                reverse=True,
            )
            for h in ranked[:top_n]:
                i, j = int(hi[h]), int(lo[h])
                pairs.append(
                    (float(sims[i, j]), members[i], members[j])
                )
                banded += 1
        stats["pairs_considered"] = len(pairs)
        stats["banded"] = banded
        return pairs

    def _base_map(
        self, actives: list["Concept"]
    ) -> dict[int, frozenset[int]]:
        """``{concept id: base ids}`` for the meta rows among ``actives``.

        One query, and skipped entirely on a graph with no metas, because
        this runs on the ``demand()`` probe as well as the run.
        """
        metas = [
            c.concept_id for c in actives if c.evidence_model == "meta"
        ]
        if not metas:
            return {}
        try:
            return self._store.concept_base_map(metas)
        except Exception:
            log.debug("concept_consolidation: base map failed", exc_info=True)
            return {}

    @staticmethod
    def _shares_base(
        bases: dict[int, frozenset[int]], a: "Concept", b: "Concept"
    ) -> bool:
        left = bases.get(int(a.concept_id))
        right = bases.get(int(b.concept_id))
        return bool(left and right and (left & right))

    # ── remembered verdicts ────────────────────────────────────────────

    @staticmethod
    def _pair_key(a: "Concept", b: "Concept") -> str:
        lo, hi = sorted((int(a.concept_id), int(b.concept_id)))
        return f"{lo}:{hi}"

    @staticmethod
    def _pair_sig(a: "Concept", b: "Concept") -> str:
        """Digest of both labels, in id order.

        A verdict is about two *statements*, not two row ids, and L17 can
        relabel either one underneath us. Eighteen of the pairs in the
        backlog this was written for had drifted past the dedup bar after
        birth, so a verdict keyed on ids alone would have gone on
        suppressing pairs whose wording no longer matched what the LLM was
        shown. The digest makes a relabel re-open the question.
        """
        first, second = (
            (a, b) if int(a.concept_id) <= int(b.concept_id) else (b, a)
        )
        raw = f"{first.label}\x00{second.label}".encode("utf-8", "replace")
        return hashlib.blake2s(raw, digest_size=8).hexdigest()

    def _verdict_map(self) -> dict[str, dict[str, str]]:
        if self._verdicts is not None:
            return self._verdicts
        loaded: dict[str, dict[str, str]] = {}
        raw = None
        if self._kv_get is not None:
            try:
                raw = self._kv_get(_VERDICT_KEY)
            except Exception:
                log.debug("verdict cache load failed", exc_info=True)
        if raw:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                for key, entry in parsed.items():
                    if isinstance(entry, dict) and entry.get("at"):
                        loaded[str(key)] = {
                            "at": str(entry.get("at") or ""),
                            "sig": str(entry.get("sig") or ""),
                        }
        self._verdicts = loaded
        return loaded

    def _rejected(self, a: "Concept", b: "Concept") -> bool:
        entry = self._verdict_map().get(self._pair_key(a, b))
        if entry is None:
            return False
        return entry.get("sig") == self._pair_sig(a, b)

    def _remember_rejection(
        self, a: "Concept", b: "Concept", now: datetime
    ) -> None:
        self._verdict_map()[self._pair_key(a, b)] = {
            "at": (now + timedelta(seconds=_VERDICT_TTL_SECONDS)).isoformat(),
            "sig": self._pair_sig(a, b),
        }
        self._verdicts_dirty = True

    def _evict_expired(self, now: datetime) -> None:
        verdicts = self._verdict_map()
        expired = [
            key for key, entry in verdicts.items()
            if self._expiry(entry) <= now
        ]
        for key in expired:
            verdicts.pop(key, None)
        # Hard cap: drop the soonest-to-expire entries if oversized.
        if len(verdicts) > _VERDICT_MAX:
            ordered = sorted(
                verdicts.items(), key=lambda kv: self._expiry(kv[1])
            )
            for key, _entry in ordered[: len(verdicts) - _VERDICT_MAX]:
                verdicts.pop(key, None)
                expired.append(key)
        if expired:
            self._verdicts_dirty = True

    @staticmethod
    def _expiry(entry: dict[str, str]) -> datetime:
        """Parse an entry's expiry, treating an unreadable one as expired.

        A stamp we can't read is a stamp we can't honour, and the safe
        direction is to re-ask rather than to suppress a pair forever on a
        value nothing can interpret.
        """
        try:
            parsed = datetime.fromisoformat(str(entry.get("at") or ""))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _flush_verdicts(self) -> None:
        if not getattr(self, "_verdicts_dirty", False):
            return
        self._verdicts_dirty = False
        if self._kv_set is None:
            return
        try:
            self._kv_set(_VERDICT_KEY, json.dumps(self._verdict_map()))
        except Exception:
            log.debug("verdict cache save failed", exc_info=True)

    # ── merge ──────────────────────────────────────────────────────────

    def _merge(
        self, a: "Concept", b: "Concept", cos: float, reason: str
    ) -> int | None:
        """Fuse the pair; returns the absorbed (deleted) id, or ``None``.

        The caller needs the id back rather than a bool, because a later
        pair in the same run may name the row this one just deleted.
        """
        canonical, absorbed = self._pick_canonical(a, b)
        absorbed_id = absorbed.concept_id
        absorbed_label = absorbed.label
        ok = self._store.merge_into(
            canonical_id=canonical.concept_id,
            absorbed_id=absorbed_id,
        )
        if not ok:
            return None
        log.info(
            "concept consolidation: merged #%s %r into #%s %r (cos=%.3f)",
            absorbed_id,
            absorbed_label,
            canonical.concept_id,
            canonical.label,
            cos,
        )
        self._emit_event(canonical.concept_id, absorbed_id, absorbed_label,
                         cos, reason)
        return absorbed_id

    @staticmethod
    def _pick_canonical(
        a: "Concept", b: "Concept"
    ) -> tuple["Concept", "Concept"]:
        """Stronger row (confidence, then evidence breadth, then id for a
        stable tiebreak) survives as canonical; the other is absorbed."""
        def strength(c: "Concept") -> tuple:
            return (
                float(c.confidence),
                int(c.distinct_source_count),
                int(c.evidence_count),
                int(c.concept_id),
            )

        return (a, b) if strength(a) >= strength(b) else (b, a)

    def _emit_event(
        self,
        canonical_id: int,
        absorbed_id: int,
        absorbed_label: str,
        cos: float,
        reason: str,
    ) -> None:
        if self._events is None:
            return
        canonical = self._store.get(canonical_id)
        if canonical is None:
            return
        detail = (
            f"Merged #{absorbed_id} '{absorbed_label}' into "
            f"#{canonical_id} '{canonical.label}'"
        )
        if reason:
            detail = f"{detail} -- {reason}"
        try:
            self._events.add(
                ConceptEvent(
                    event_type="merged",
                    kind=canonical.kind,
                    subject=canonical.subject,
                    label=canonical.label,
                    confidence=float(canonical.confidence),
                    novelty=max(0.0, 1.0 - float(cos)),
                    evidence_count=int(canonical.evidence_count),
                    distinct_source_count=int(
                        canonical.distinct_source_count
                    ),
                    reason=detail,
                    concept_id=canonical_id,
                )
            )
        except Exception:
            log.debug("concept merge event insert failed", exc_info=True)

    # ── LLM adjudication ───────────────────────────────────────────────

    def _subject_word(self, subject: str) -> str:
        if subject == "aiko":
            name = None
            if self._assistant_name_provider is not None:
                try:
                    name = self._assistant_name_provider()
                except Exception:
                    name = None
            return name or "Aiko"
        name = None
        if self._user_name_provider is not None:
            try:
                name = self._user_name_provider()
            except Exception:
                name = None
        return name or "the user"

    def _adjudicate(self, a: "Concept", b: "Concept") -> tuple[bool, str]:
        subject_word = self._subject_word(a.subject)
        kind = a.kind.replace("_", " ")
        system = (
            "You are a precise annotator. Two short belief statements about "
            f"{subject_word} are given. Decide whether they express the SAME "
            f"underlying {kind}: one is a paraphrase, restatement, or a "
            "subset of the other and they should be merged into a single "
            "belief. Two statements that merely share a sentence template or "
            "a broad theme while asserting DIFFERENT specifics are NOT the "
            "same. When uncertain, answer false. "
            'Return JSON only: {"same": boolean, "reason": string}.'
        )
        user = (
            f"Statement A: {a.label}\n"
            f"{self._rationale_line(a)}"
            f"Statement B: {b.label}\n"
            f"{self._rationale_line(b)}"
            f"\nAre A and B the same {kind}?"
        )
        parsed = self._call_llm(system, user)
        if not isinstance(parsed, dict):
            return (False, "")
        same = bool(parsed.get("same") is True)
        reason = str(parsed.get("reason") or "").strip()
        if len(reason) > 200:
            reason = reason[:199] + "\u2026"
        return (same, reason)

    @staticmethod
    def _rationale_line(c: "Concept") -> str:
        rationale = (getattr(c, "rationale", "") or "").strip()
        if not rationale:
            return ""
        if len(rationale) > 200:
            rationale = rationale[:199] + "\u2026"
        return f"(rationale: {rationale})\n"

    def _call_llm(self, system: str, user: str) -> dict[str, Any] | None:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={
                    "num_predict": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="concept_consolidation_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning(
                "concept consolidation LLM call failed", exc_info=True
            )
            return None
        return self._parse("".join(chunks))

    @staticmethod
    def _parse(raw: str) -> dict[str, Any] | None:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


__all__ = ["ConceptConsolidationWorker"]
