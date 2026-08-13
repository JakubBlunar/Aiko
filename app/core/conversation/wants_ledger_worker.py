"""K52 — wants-ledger feeder worker.

IdleWorker that keeps :mod:`app.core.conversation.wants_ledger`
stocked from producers that already exist. No LLM call — ingestion is
deterministic:

- **Curiosity seeds** (K9, pending ``curiosity_seed`` cues) — unspent
  seeds become ``ask`` wants ("bring up what you've been curious
  about: ...").
- **Forward-curiosity questions** (K34 journal ring on kv_meta) —
  the newest drafted wonderings become ``ask`` wants ("ask {user}
  ..."), except for K87's ``wondering`` entries, which are subjects of
  hers and become the ledger's only ``share`` wants.
- **Active goals** (K1 ``GoalStore``) — the newest active goals
  become low-pressure ``steer`` wants ("steer toward something of
  yours: ...").
- **Active pursuits** (K85 ``pursuit`` concepts, read through
  ``ConceptView`` under the ``wants_ledger`` diet) — a subject of hers
  becomes a ``share`` want, so K53's "this turn is yours" has
  something to open on that isn't about him.

Dedup / capping / re-entry cooldown all live in the pure module's
:func:`add_want`; the worker just walks the producers and offers each
candidate. The worker also applies pressure growth each tick so the
ledger keeps maturing even when no chat turns happen (the provider
applies growth lazily too — both paths land on the same pure
function, so semantics are identical).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.concepts.concept_diets import diet_for
from app.core.conversation import wants_ledger
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.concepts.concept_view import ConceptView
    from app.core.goals.goal_store import GoalStore
    from app.core.memory.memory_store import MemoryStore


log = logging.getLogger("app.wants_ledger_worker")


# Per-run ingestion caps — keep each tick cheap and let the ledger
# fill over hours, not in one burst.
_MAX_SEEDS_PER_RUN = 2
_MAX_FORWARD_PER_RUN = 2
_MAX_GOALS_PER_RUN = 2
# K85e: one pursuit per tick. The ledger caps at 8 and a pursuit is a
# standing fact rather than a fresh event, so letting several in at once
# would crowd out the time-sensitive wants with things that will still
# be true tomorrow.
_MAX_PURSUITS_PER_RUN = 1
# Most a single tick can ingest -- the saturation point for demand().
_MAX_PER_RUN = (
    _MAX_SEEDS_PER_RUN
    + _MAX_FORWARD_PER_RUN
    + _MAX_GOALS_PER_RUN
    + _MAX_PURSUITS_PER_RUN
)
# Goal-derived wants start lower than ask/share wants: steering toward
# a goal is a background pull, not a fresh itch.
_GOAL_INITIAL_PRESSURE = 0.05
# K85e: a pursuit starts lower still. It has no deadline and nothing
# was asked of her, so it should surface on a genuinely open turn after
# the questions and wonderings have had their chance, not before.
_PURSUIT_INITIAL_PRESSURE = 0.04


def _utcnow() -> datetime:
    return timephrase.utcnow()


@dataclass(frozen=True)
class _IngestPlan:
    """What one tick would do to the ledger, before anything is written.

    Every stage of the ingest is a pure function over an immutable
    :class:`~app.core.conversation.wants_ledger.LedgerState`, so the
    whole next state can be computed and then either persisted (by
    ``run()``) or discarded (by ``demand()``).
    """

    state: "wants_ledger.LedgerState"
    added: tuple[tuple[str, str], ...]
    """``(source, source_ref)`` per want that would be added."""
    dropped: tuple[str, ...]
    """Want ids that would be retired."""
    dead_refs: tuple[str, ...]
    """``source_ref``s whose backing curiosity seed is gone."""


class WantsLedgerWorker:
    """IdleWorker feeding the K52 wants ledger from existing stores."""

    name = "wants_ledger"

    def __init__(
        self,
        *,
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_display_name_provider: Callable[[], str],
        memory_store: "MemoryStore | None" = None,
        goal_store: "GoalStore | None" = None,
        cue_store_provider: Callable[[], Any] | None = None,
        view_provider: Callable[[], "ConceptView | None"] | None = None,
        pursuit_wants_enabled_provider: Callable[[], bool] | None = None,
        pursuit_min_confidence: float = 0.6,
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 3600.0,
        cap: int = 8,
        per_source_cap: int = 4,
        growth_per_day: float = 0.25,
        max_age_days: float = 14.0,
        reentry_cooldown_days: float = 5.0,
    ) -> None:
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_display_name_provider = user_display_name_provider
        self._memory_store = memory_store
        self._goal_store = goal_store
        self._cue_store_provider = cue_store_provider
        self._view_provider = view_provider
        self._pursuit_wants_enabled_provider = pursuit_wants_enabled_provider
        self._pursuit_min_confidence = max(0.0, float(pursuit_min_confidence))
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._cap = max(1, int(cap))
        self._per_source_cap = max(0, int(per_source_cap))
        self._growth_per_day = max(0.0, float(growth_per_day))
        self._max_age_days = max(1.0, float(max_age_days))
        self._reentry_cooldown_days = max(0.0, float(reentry_cooldown_days))

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        return self._enabled()

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            # Matches run(): a raising provider is no opinion, not a veto.
            return True

    def _plan(self, now: datetime) -> _IngestPlan:
        """Compute the ledger this tick would persist, without writing it.

        Shared by ``run()`` and ``demand()``. Emits no log lines — the
        caller decides whether this is a real tick worth narrating.
        """
        state = wants_ledger.deserialize(
            self._kv_get_safe(wants_ledger.KV_WANTS_LEDGER)
        )
        state = wants_ledger.apply_growth(
            state, now,
            growth_per_day=self._growth_per_day,
            max_age_days=self._max_age_days,
            reentry_cooldown_days=self._reentry_cooldown_days,
        )
        # Tie curiosity-seed wants to their seed's lifetime: once the
        # seed is consumed/archived (its topic came up) or deleted, the
        # want is orphaned — the feeder stops offering it but nothing
        # removed the live row, so its pressure kept climbing and drove
        # Aiko to re-ask a question she'd already had answered. Self-heal
        # every tick.
        state, dropped, dead_refs = self._prune_dead_seed_wants(state)
        state, gone, gone_refs = self._prune_dead_pursuit_wants(state)
        dropped = list(dropped) + gone
        dead_refs = list(dead_refs) + gone_refs

        added: list[tuple[str, str]] = []
        name = self._safe_name()
        for text, kind, source, ref, pressure in self._candidates(name):
            state, ok = wants_ledger.add_want(
                state,
                text=text,
                kind=kind,
                source=source,
                source_ref=ref,
                now=now,
                cap=self._cap,
                initial_pressure=pressure,
                per_source_cap=self._per_source_cap,
            )
            if ok:
                added.append((source, ref))
        return _IngestPlan(
            state=state,
            added=tuple(added),
            dropped=tuple(dropped),
            dead_refs=tuple(dead_refs),
        )

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Count the wants this tick would add or retire.

        Pressure deliberately ignores the growth step. Growth is
        elapsed-time exponential and the provider applies it lazily on
        the next turn through the same pure function, so a tick spent
        only to re-persist grown pressures changes nothing anyone can
        observe. What is worth a slot is a genuinely new want, or a
        stale seed want that would otherwise keep climbing toward
        re-asking a question already answered.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        try:
            plan = self._plan(now)
        except Exception:
            log.debug("wants ledger demand probe failed", exc_info=True)
            return None
        pending = len(plan.added) + len(plan.dropped)
        if not pending:
            return WorkSignal(pressure=0.0, reason="ledger current")
        return WorkSignal(
            pressure=pressure_from_count(pending, saturation=_MAX_PER_RUN),
            reason=f"{len(plan.added)} new, {len(plan.dropped)} dead",
        )

    def run(self) -> dict[str, Any]:
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return {"added": 0, "disabled": True}
            except Exception:
                pass
        plan = self._plan(_utcnow())
        for ref in sorted(plan.dead_refs):
            log.info("wants-ledger pruned dead seed want: ref=%s", ref)
        for source, ref in plan.added:
            log.info("wants-ledger added: source=%s ref=%s", source, ref)

        try:
            self._kv_set(
                wants_ledger.KV_WANTS_LEDGER,
                wants_ledger.serialize(plan.state),
            )
        except Exception:
            log.debug("wants ledger persist failed", exc_info=True)
        return {
            "added": len(plan.added),
            "pruned": len(plan.dropped),
            "live": len(plan.state.wants),
        }

    # ── maintenance ──────────────────────────────────────────────────

    def _prune_dead_seed_wants(
        self, state: "wants_ledger.LedgerState",
    ) -> tuple["wants_ledger.LedgerState", list[str], list[str]]:
        """Drop ``curiosity_seed`` wants whose subject is settled.

        A want retires when its seed does — but only for the two exits
        that mean the subject is *done*: ``used`` (the topic came up) and
        ``superseded`` (the row was merged away). Returns the pruned
        state, the dropped want ids, and the retired ``source_ref``s.

        The third terminal state is the one this method has twice been
        wrong about, in opposite directions. It first asked for
        ``pending`` seeds, which a seed stops being the moment it renders
        into a prompt (H28) — so wants died 1.9 hours in. H28 moved it to
        ``live``, which is correct about surfacing and still wrong about
        ``expired``: a seed expires by being *offered and refused*, and
        all 110 expired seeds on this install hit ``max_surfacings`` at
        exactly two showings, median 2.9 hours (H29). Against a pressure
        mechanic needing 19 hours to reach K54's bar and 53 to reach the
        imperative, that is the same drain with a longer fuse. Being
        shown something and not biting is the state that most deserves to
        keep wanting, so an expired seed now leaves its want behind and
        the want ages on the ledger's own clock.

        The read is by *presence* rather than absence
        (:meth:`CueStore.resolved_ids`), so an unreadable pool, an empty
        result and a huge ledger all retire nothing, and there is no page
        to overflow.
        """
        if not state.wants:
            return state, [], []
        by_id: dict[int, str] = {}
        for want in state.wants:
            if want.source != "curiosity_seed":
                continue
            if not want.source_ref.startswith("cue:"):
                continue
            try:
                by_id[int(want.source_ref.split(":", 1)[1])] = want.source_ref
            except (IndexError, ValueError):
                continue
        if not by_id:
            return state, [], []
        store = self._cue_store()
        if store is None:
            return state, [], []
        try:
            settled = store.resolved_ids(by_id.keys())
        except Exception:
            log.debug("wants: seed resolution read failed", exc_info=True)
            return state, [], []
        dead = {by_id[cue_id] for cue_id in settled if cue_id in by_id}
        if not dead:
            return state, [], []
        state, dropped = wants_ledger.drop_source_refs(state, dead)
        return state, dropped, sorted(dead)

    def _prune_dead_pursuit_wants(
        self, state: "wants_ledger.LedgerState",
    ) -> tuple["wants_ledger.LedgerState", list[str], list[str]]:
        """Retire ``pursuit`` wants whose concept is no longer active.

        Same self-heal as the curiosity seeds above, for the same
        reason: L3 can demote or decay a pursuit at any time, and a want
        left behind by one keeps growing pressure until she volunteers
        an interest she no longer has.
        """
        refs = {
            w.source_ref for w in state.wants
            if w.source == "pursuit" and w.source_ref.startswith("pursuit:")
        }
        if not refs:
            return state, [], []
        rows = self._active_pursuits()
        if rows is None:
            return state, [], []
        live = {f"pursuit:{_concept_ref(c)}" for c in rows}
        dead = refs - live
        if not dead:
            return state, [], []
        state, dropped = wants_ledger.drop_source_refs(state, dead)
        return state, dropped, sorted(dead)

    def _active_pursuits(self) -> list[Any] | None:
        """Active ``pursuit`` concepts, or ``None`` if unreadable.

        Read through :class:`~app.core.concepts.concept_view.ConceptView`
        under the ``wants_ledger`` diet, per L24's "read through the view,
        never the store": this worker was the last direct ``ConceptStore``
        reader, which meant its own copy of the status / subject / kind
        filter and its own confidence sort, both of which the view already
        does and neither of which anything kept in step.

        The ``None`` / ``[]`` distinction matters to the pruner exactly
        as it does for the seed pool: an empty store retires every
        pursuit want, a failed read must retire none of them. So a missing
        or cold view is ``None``, not ``[]``.
        """
        provider = self._view_provider
        if provider is None:
            return None
        if self._pursuit_wants_enabled_provider is not None:
            try:
                if not bool(self._pursuit_wants_enabled_provider()):
                    return None
            except Exception:
                pass
        try:
            view = provider()
        except Exception:
            log.debug("wants: concept view provider raised", exc_info=True)
            return None
        if view is None or not getattr(view, "enabled", False):
            return None
        diet = diet_for(self.name)
        kinds = diet.kinds if diet is not None else ("pursuit",)
        subject = diet.subject if diet is not None else "aiko"
        floor = max(
            self._pursuit_min_confidence,
            float(diet.min_confidence) if diet is not None else 0.0,
        )
        rows: list[Any] = []
        for kind in kinds:
            try:
                rows.extend(
                    view.core(
                        kind=kind, subject=subject, min_confidence=floor,
                    )
                )
            except Exception:
                log.debug("wants: pursuit read failed", exc_info=True)
                return None
        # Strongest first, so the one want a tick may add is her most
        # settled interest rather than whichever row L3 touched last.
        rows.sort(
            key=lambda c: float(getattr(c, "confidence", 0.0) or 0.0),
            reverse=True,
        )
        return rows

    def _pending_seeds(self, *, limit: int) -> list[Any] | None:
        """Unspent curiosity seeds, or ``None`` if the pool can't be read.

        The *producer* read: only a seed Aiko has never been offered
        should mint a new want. The pruner asks a different question
        entirely — see :meth:`_prune_dead_seed_wants`, which reads
        settlement rather than availability.
        """
        store = self._cue_store()
        if store is None:
            return None
        try:
            return store.pending("curiosity_seed", limit=max(1, int(limit)))
        except Exception:
            log.debug("wants: seed pool pending read failed", exc_info=True)
            return None

    def _cue_store(self) -> Any | None:
        provider = self._cue_store_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            log.debug("wants: cue store provider raised", exc_info=True)
            return None

    # ── candidate producers ──────────────────────────────────────────

    def _candidates(self, name: str) -> list[tuple[str, str, str, str, float]]:
        """Yield ``(text, kind, source, source_ref, initial_pressure)``."""
        out: list[tuple[str, str, str, str, float]] = []

        # 1. Curiosity seeds, in the pool's own order — the same
        # least-surfaced-first rule the K9 surfacing block sees.
        for row in (self._pending_seeds(limit=_MAX_SEEDS_PER_RUN) or []):
            topic = (row.subject or "").strip()
            if not topic:
                continue
            out.append((
                f"bring up what you've been curious about: {_clip(topic)}",
                "ask",
                "curiosity_seed",
                f"cue:{row.id}",
                0.15,
            ))

        # 2. Forward-curiosity journal (newest entries first).
        try:
            from app.core.proactive.forward_curiosity_worker import (
                is_hers,
                load_questions,
            )

            ring = load_questions(self._kv_get)
        except Exception:
            log.debug("wants: forward-curiosity load failed", exc_info=True)
            ring = []
            is_hers = lambda _e: False  # noqa: E731 - one-line degradation
        for entry in list(reversed(ring))[:_MAX_FORWARD_PER_RUN]:
            question = str(entry.get("question") or "").strip()
            if not question:
                continue
            ref = str(entry.get("source_id") or entry.get("at") or "").strip()
            if not ref:
                continue
            # K87: an entry of hers is a subject of her own, not a
            # question about him, and it becomes the ledger's first
            # ``share`` want. Filing it as an ``ask`` would hand K53 an
            # interview line under a different label, which is exactly
            # the failure the quota exists to prevent. L28's concepts of
            # hers arrive on the same side, which is why the predicate is
            # shared rather than a source check here.
            if is_hers(entry):
                out.append((
                    f"say what you've been chewing on: {_clip(question)}",
                    "share",
                    "forward_curiosity",
                    f"fc:{ref}",
                    0.15,
                ))
                continue
            out.append((
                f"ask {name} {_clip(question)}",
                "ask",
                "forward_curiosity",
                f"fc:{ref}",
                0.15,
            ))

        # 3. Active goals (newest first, low starting pressure).
        goals = self._goal_store
        if goals is not None:
            try:
                rows = goals.list_active()
            except Exception:
                log.debug("wants: goal list failed", exc_info=True)
                rows = []
            for goal in rows[:_MAX_GOALS_PER_RUN]:
                summary = (goal.content or "").strip()
                if not summary:
                    continue
                out.append((
                    f"steer toward something of yours: {_clip(summary)}",
                    "steer",
                    "goal",
                    f"goal:{goal.id}",
                    _GOAL_INITIAL_PRESSURE,
                ))

        # 4. K85e -- active pursuits, as the ledger's standing ``share``
        # want. Phrased as an offer of the concrete thing rather than
        # the label, because "tell him about gardening" gets a topic
        # announcement and "the bit of it that's on your mind" gets a
        # sentence with something in it.
        for concept in (self._active_pursuits() or [])[:_MAX_PURSUITS_PER_RUN]:
            label = str(getattr(concept, "label", "") or "").strip()
            cid = _concept_ref(concept)
            if not label or not cid:
                continue
            out.append((
                f"offer something of your own: {_clip(label)}"
                " -- the part of it that's actually on your mind",
                "share",
                "pursuit",
                f"pursuit:{cid}",
                _PURSUIT_INITIAL_PRESSURE,
            ))
        return out

    # ── helpers ──────────────────────────────────────────────────────

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _safe_name(self) -> str:
        try:
            return (self._user_display_name_provider() or "them").strip() or "them"
        except Exception:
            return "them"


def _concept_ref(concept: Any) -> str:
    """The id half of a ``pursuit:{id}`` want ref.

    A persisted ``Concept`` carries ``concept_id``; ``id`` is read as a
    fallback so the pruner and the producer agree on the key regardless of
    which shape a row arrives in.
    """
    for attr in ("concept_id", "id"):
        raw = getattr(concept, attr, None)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text != "0":
            return text
    return ""


def _clip(text: str, limit: int = 140) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip(",;: ") + "…"


__all__ = ["WantsLedgerWorker"]
