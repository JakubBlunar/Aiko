"""Session-side half of the cue pool: surfacing and consumption.

Two things happen here, at the two ends of a turn.

**Surfacing.** A T6 provider asks for the best pending cue of its type,
applies its own gate to the payload, and if it renders the cue it says so.
That marks the row ``surfaced`` and stashes it for post-turn. Marking is
separate from picking on purpose: a cue that was inspected and rejected
must not spend one of its two surfacings.

**Consumption.** After the turn, we ask whether Aiko actually used the
cue. Before the pool this question was never asked -- a cue was retired
the moment its block rendered, so ignoring one and acting on one were
indistinguishable. The verdict is two cheap local tests
(:mod:`app.core.memory.echo_detector`) against the cue's *subject*, never
an LLM: post-turn is not a place to spend a generation.

Why the subject rather than the cue line: a cue reads "we haven't talked
about film photography in ages", and almost every word of that is framing
that will never appear in Aiko's reply. What identifies the cue is ``film
photography``. Matching the sentence would dilute the overlap toward a
miss on exactly the turns where she used the cue perfectly.

And why it takes two stages for some types: if she asks about X and the
user answers about Y, she asked and got nothing. The curiosity is not
satisfied, so the cue has to survive -- see ``CuePolicy.fulfilment``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.core.infra import timephrase
from app.core.memory import echo_detector
from app.core.proactive.cue_accounting import (
    CUE_POLICIES,
    FULFILMENT_ANSWERED,
    FULFILMENT_EITHER,
    MATCH_LEXICAL_OR_COSINE,
    MATCH_SCOPE_TURN,
    CuePolicy,
    policy_for,
)
from app.core.proactive.cue_producer import pick_pool_cue

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.proactive.cue_store import CueRow, CueStore


log = logging.getLogger("app.cue_pool")

# Above any real cosine, so a lexical-only policy still has its cosine
# measured and recorded as ``used_evidence`` while never being decided by
# it. That record is the calibration data the conservative types need
# before they can be promoted to ``lexical_or_cosine`` on evidence.
_UNREACHABLE_COSINE = 2.0

# Ceiling on the per-turn eavesdrop scan. Inventory targets are single
# digits, so this is slack rather than a real limit -- it exists so a
# pathological pool cannot turn a post-turn hook into a table scan.
_EITHER_PARTY_SCAN = 20

# A cue drafted inside this window is close enough to "just now" that
# saying when it was noticed would be noise. Past it, the gap is worth
# disclosing: cue text is frozen at draft time and pending rows have been
# observed surfacing up to 44 hours later.
_DRAFT_AGE_DISCLOSE_HOURS = 6.0


def _draft_age_note(created_at: str | None) -> str:
    """A parenthetical telling Aiko when a cue was actually noticed.

    The pool's whole design is that a worker drafts a line while the user
    is away and it waits for a fitting moment -- but the line is written
    in the present tense of the draft. Surfaced a day later, "you've been
    wondering how his interview went" reads as a thought she is having
    right now, and she relays it as fresh news.

    Disclosing the lag is cheaper and safer than rewriting the sentence:
    the text keeps whatever nuance the worker gave it, and Aiko is simply
    told how old the thought is so she can pitch it accordingly.
    """
    when = timephrase.parse_iso(created_at)
    if when is None:
        return ""
    now = timephrase.utcnow()
    if (timephrase.to_aware(now) - when) < timedelta(
        hours=_DRAFT_AGE_DISCLOSE_HOURS
    ):
        return ""
    return (
        " (you first noticed this "
        f"{timephrase.humanize_past(str(created_at), now)}, not just now -- "
        "bring it up as something you've been sitting on)"
    )


class CuePoolMixin:
    """Mixed into :class:`~app.core.session.session_controller.SessionController`."""

    # ── surfacing ─────────────────────────────────────────────────────

    def _cue_pool_store(self) -> "CueStore | None":
        return getattr(self, "_cue_store", None)

    def take_pool_cue(
        self,
        cue_type: str,
        *,
        relevant: Callable[[dict[str, Any]], bool] | None = None,
        force: bool = False,
    ) -> "CueRow | None":
        """Claim one pending cue for the prompt, or ``None``.

        Marks the row ``surfaced`` and remembers it for the post-turn
        verdict. Callers render ``row.text``; the handling instructions for
        the type ride along separately, appended in T6 by the assembler.
        """
        store = self._cue_pool_store()
        row = pick_pool_cue(
            store,
            cue_type,
            relevant=relevant,
            force=force,
            allow_first_claim=force or not self._cadence_blocked(cue_type),
        )
        if row is None or store is None:
            return None
        try:
            store.mark_surfaced(row.id)
        except Exception:
            log.debug("cue mark_surfaced failed: id=%s", row.id, exc_info=True)
        self._register_surfaced_cue(row)
        # K-time10. The note goes on a copy: the registered row is what
        # post-turn accounting judges, and it should see the cue exactly
        # as its producer wrote it rather than with our parenthetical
        # glued on.
        note = _draft_age_note(row.created_at)
        if note:
            return replace(row, text=f"{row.text}{note}")
        return row

    def _queue_pool_cue(
        self,
        cue_type: str,
        subject: str,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        hold_hours: float = 0.0,
    ) -> bool:
        """Queue a cue the turn path detected, ``pending``, under its TTL.

        The session-side twin of :meth:`CueProducer.publish`. Most cues
        are drafted by a worker while the user is away, but a couple are
        noticed post-turn -- a contradiction only exists once Aiko has
        said the contradicting thing -- and those have no worker to route
        through.

        Distinct from :meth:`record_surfaced_cue`, which writes a row that
        is *already* in the prompt. This one is written a turn early and
        waits to be claimed, which is what the ordinary pool cues do.

        ``hold_hours`` seals it for longer than that: a cue that has to
        ripen before it is worth saying, rather than one merely waiting
        its turn.
        """
        store = self._cue_pool_store()
        if store is None:
            return False
        policy = self._policy_for(cue_type)
        try:
            cue_id = store.add(
                cue_type,
                subject,
                text,
                payload=payload,
                ttl_hours=policy.ttl_hours if policy is not None else None,
                hold_hours=hold_hours,
            )
        except Exception:
            log.debug("cue queue failed: type=%s", cue_type, exc_info=True)
            return False
        return bool(cue_id)

    def record_surfaced_cue(
        self,
        cue_type: str,
        subject: str,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        ttl_hours: float | None = None,
    ) -> "CueRow | None":
        """Log a cue that was chosen at render time, already ``surfaced``.

        The pool's other entry point assumes a worker drafted the cue in
        advance and the prompt merely claims one. Several cues cannot work
        that way: they are armed by an event carrying no content -- a typed
        gap stores a duration and nothing else -- and *which* reflection or
        journal entry gets used is picked when the block renders, against
        the message being answered. Drafting ahead would throw that
        query-awareness away.

        So the row is written at the moment it surfaces. Everything after
        that point is shared: the same post-turn matching decides whether
        Aiko used it, and a miss releases it back to ``pending`` where the
        block's next render finds it via :meth:`take_pool_cue`. That is
        what turns a one-shot slot into something with retries.
        """
        store = self._cue_pool_store()
        if store is None:
            return None
        try:
            cue_id = store.add(
                cue_type,
                subject,
                text,
                payload=payload,
                ttl_hours=ttl_hours,
            )
            if not cue_id:
                return None
            # Read before marking, matching what ``take_pool_cue``
            # stashes: post-turn counts this surfacing itself, and a row
            # carrying it already would have the cue expire a turn early.
            row = store.get(cue_id, with_embedding=True)
            store.mark_surfaced(cue_id)
        except Exception:
            log.debug(
                "cue record_surfaced failed: type=%s", cue_type, exc_info=True,
            )
            return None
        if row is None:
            return None
        self._register_surfaced_cue(row)
        return row

    def _cadence_blocked(self, cue_type: str) -> bool:
        """Is this type too soon off its last surfacing to open a new cue?

        Rarity used to be a side effect of production: a worker drafting
        one entry a fortnight surfaced one a fortnight, and no gate was
        needed because there was never a second cue to reach for. Stocking
        the shelf against an inventory target removes that accident, so
        the types whose whole character is scarcity carry a
        ``surface_cooldown_hours`` and this is where it is spent.

        Reads the pool rather than a watermark of its own. Every row
        already records ``last_surfaced_at``, so the newest of them is the
        answer, and a second source of truth for the same fact is exactly
        the kind of drift the pool was built to end.
        """
        policy = self._policy_for(cue_type)
        hours = float(getattr(policy, "surface_cooldown_hours", 0.0) or 0.0)
        if policy is None or hours <= 0.0:
            return False
        store = self._cue_pool_store()
        if store is None:
            return False
        try:
            last = timephrase.parse_iso(store.last_surfaced_at(cue_type))
        except Exception:
            log.debug("cue cadence read failed: type=%s", cue_type, exc_info=True)
            return False
        if last is None:
            return False
        return timephrase.utcnow() - last < timedelta(hours=hours)

    def _register_surfaced_cue(self, row: "CueRow") -> None:
        """Remember a row for this turn's post-turn verdict."""
        pending = getattr(self, "_surfaced_pool_cues", None)
        if pending is None:
            pending = []
            self._surfaced_pool_cues = pending
        pending.append(row)

    # ── consumption, stage A: did Aiko raise it? ──────────────────────

    def _settle_pool_cues(
        self,
        *,
        user_text: str,
        assistant_text: str,
        reply_vec: Any = None,
        turn_vec: Any = None,
    ) -> None:
        """Judge every cue that reached this turn's prompt.

        Both vectors are passed in rather than computed here: post-turn
        already embeds Aiko's reply for K22 and the combined turn for the
        seed and gap resolvers, and which of the two a cue is matched
        against is its ``match_scope``. Handing them down is what keeps
        this check free rather than a third round-trip.
        """
        surfaced = list(getattr(self, "_surfaced_pool_cues", None) or [])
        self._surfaced_pool_cues = []
        store = self._cue_pool_store()
        if store is None:
            return
        eavesdrop = self._either_party_pending(store, skip=surfaced)
        if not surfaced and not eavesdrop:
            return

        reply_tokens = echo_detector.tokens(assistant_text or "")
        turn_tokens = reply_tokens | echo_detector.tokens(user_text or "")
        now = timephrase.utcnow()
        for row, was_surfaced in (
            [(row, True) for row in surfaced]
            + [(row, False) for row in eavesdrop]
        ):
            policy = self._policy_for(row.cue_type)
            if policy is None:
                continue
            whole_turn = policy.match_scope == MATCH_SCOPE_TURN
            verdict = self._match_cue(
                row,
                policy,
                tokens=turn_tokens if whole_turn else reply_tokens,
                text_vec=turn_vec if whole_turn else reply_vec,
            )
            evidence = f"{verdict.kind}:{verdict.score:.2f}"
            if not verdict.echoed:
                if was_surfaced:
                    self._retire_or_retry(
                        store, row, policy, evidence=evidence,
                    )
                continue
            if policy.fulfilment == FULFILMENT_ANSWERED:
                # She raised it. Whether that was worth anything depends on
                # what the user says next -- stage B.
                store.mark_asked(row.id, now=now)
                self._stamp_hypothesis_ask(row)
                log.info(
                    "cue asked: type=%s subject=%r via=%s",
                    row.cue_type, row.subject[:60], verdict.kind,
                )
                continue
            self._mark_cue_used(store, row, evidence=evidence)

    def _stamp_hypothesis_ask(self, row: "CueRow") -> None:
        """Spend an invented hypothesis's one ask, now that it was asked.

        L30 Phase B allows each invention a single ask, and this is the
        only place that knows the ask happened. The counter used to be
        bumped when the *cue was published* instead, which deadlocked the
        whole lane on the live graph: the shelf surfaces a
        ``concept_hypothesis`` about once a day by policy while the
        proposer queues several, so 22 of 26 cues were never rendered --
        yet every one of their rows counted as asked, which made them
        simultaneously un-re-askable (the ask worker filters
        ``asked_count <= 0``) and un-expirable (TTL skips asked rows).
        Twelve of them then filled ``hypothesis_max_open`` and invention
        stopped for good, reported only as a healthy-looking
        ``skipped: max_open``.

        Best-effort: a missing store or a row deleted since the cue was
        published just means no counter to move.
        """
        try:
            payload = row.payload or {}
            if str(payload.get("target_type") or "") != "hypothesis":
                return
            store = getattr(self, "_hypothesis_store", None)
            if store is None:
                return
            target = store.get(int(payload.get("target_id") or 0))
            if target is None:
                return
            target.asked_count = int(target.asked_count) + 1
            store.update(target)
        except Exception:
            log.debug(
                "hypothesis ask stamp failed: cue=%s", row.id, exc_info=True,
            )

    def _either_party_pending(
        self, store: "CueStore", *, skip: list["CueRow"],
    ) -> list["CueRow"]:
        """Pending ``either_party`` cues that were never in the prompt.

        The one place consumption looks beyond what surfaced, and K9's
        behaviour is the reason: a curiosity seed is spent the moment the
        conversation lands on its subject, no matter who steered it there
        or whether Aiko was ever shown the cue. Holding a seed open for a
        topic the two of them just spent a turn on would be the opposite
        of curiosity.

        Restricted to ``either_party`` because for the other two
        fulfilments an unsurfaced cue genuinely has nothing to answer for
        -- nobody was asked to use it.
        """
        seen = {row.id for row in skip}
        out: list[CueRow] = []
        for cue_type, policy in CUE_POLICIES.items():
            if policy.fulfilment != FULFILMENT_EITHER:
                continue
            try:
                rows = store.pending(
                    cue_type, limit=_EITHER_PARTY_SCAN, with_embedding=True,
                )
            except Exception:
                log.debug(
                    "either-party cue read failed: type=%s",
                    cue_type,
                    exc_info=True,
                )
                continue
            out.extend(row for row in rows if row.id not in seen)
        return out

    def _policy_for(self, cue_type: str) -> CuePolicy | None:
        """The type's policy, with the one config key that predates it applied.

        ``agent.curiosity_seed_resolve_threshold`` has been tuning seed
        resolution since K9 shipped; the registry inherited its default
        rather than replacing it, so a user who moved it keeps their value.
        """
        policy = policy_for(cue_type)
        if policy is None or cue_type != "curiosity_seed":
            return policy
        settings = getattr(self, "_settings", None)
        agent = getattr(settings, "agent", None)
        raw = getattr(agent, "curiosity_seed_resolve_threshold", None)
        if raw is None:
            return policy
        try:
            return replace(policy, match_threshold=float(raw))
        except Exception:
            return policy

    def _match_cue(
        self,
        row: "CueRow",
        policy: CuePolicy,
        *,
        tokens: set[str],
        text_vec: Any,
    ) -> echo_detector.EchoVerdict:
        """Lexical against the subject, with cosine only where it means something.

        The cosine is asked for even when the policy will not act on it, so
        it lands in ``used_evidence`` on every verdict. That is the
        calibration data: once a few hundred have accumulated, comparing
        the distribution on turns where lexical fired against turns where
        it did not says where the real floor belongs for the two
        conservative types, and they can be promoted on evidence instead of
        on a guess.
        """
        # An associative wander is about a *pair*, and the half worth
        # matching is the one the conversation was not already on --
        # recorded by the provider when it picked the cue.
        subject = str(row.payload.get("match_subject") or "") or row.subject
        min_cosine = (
            policy.match_threshold
            if policy.match_mode == MATCH_LEXICAL_OR_COSINE
            else _UNREACHABLE_COSINE
        )
        return echo_detector.detect(
            reply_tokens=tokens,
            item_text=subject,
            min_overlap=policy.min_overlap,
            reply_vec=text_vec,
            item_vec=row.embedding,
            min_cosine=min_cosine,
        )

    # ── consumption, stage B: did the user answer? ────────────────────

    def _settle_awaiting_cues(self, *, user_text: str) -> None:
        """Settle questions Aiko asked on an earlier turn.

        The same two-clock shape
        :class:`~app.core.memory.surfacing_outcome_store.SurfacingOutcomeStore`
        uses -- record against turn N, settle at N+1 -- because the thing
        being measured only exists in the next message.
        """
        store = self._cue_pool_store()
        if store is None:
            return
        try:
            from app.core.proactive.cue_store import STATE_AWAITING

            rows = store.in_state(STATE_AWAITING, with_embedding=True)
        except Exception:
            log.debug("awaiting cue read failed", exc_info=True)
            return
        if not rows:
            return
        user_tokens = echo_detector.tokens(user_text or "")
        user_vec = self._user_vec_for(rows, user_text)
        now = timephrase.utcnow()
        for row in rows:
            policy = self._policy_for(row.cue_type)
            if policy is None:
                continue
            verdict = self._match_cue(
                row, policy, tokens=user_tokens, text_vec=user_vec,
            )
            if verdict.echoed:
                self._mark_cue_used(
                    store,
                    row,
                    evidence=f"answered/{verdict.kind}:{verdict.score:.2f}",
                )
                continue
            # The question went by. That is a real signal rather than only
            # a failure: a cue type whose asks keep going unanswered is one
            # the user does not want to be asked about.
            if row.ask_count >= policy.max_asks:
                store.expire(row.id, evidence="max_asks")
                log.info(
                    "cue expired unanswered: type=%s subject=%r asks=%d",
                    row.cue_type, row.subject[:60], row.ask_count,
                )
                continue
            store.release(
                row.id,
                not_before=now
                + timedelta(hours=max(0.0, policy.reask_cooldown_hours)),
                evidence="unanswered",
            )

    def _user_vec_for(self, rows: list["CueRow"], user_text: str) -> Any:
        """Embed the user's message, but only if a cue could use it.

        The one embed in the cue path that post-turn does not already
        have in hand: stage A rides the reply and turn vectors, and this
        is a different text. It is affordable because ``awaiting`` rows
        only exist on turns after Aiko actually asked something, so the
        common case skips it entirely -- and among those, only the types
        whose policy trusts cosine make it worth the round-trip.
        """
        text = (user_text or "").strip()
        if len(text) < 4:
            return None
        wants_cosine = any(
            (policy_for(row.cue_type) or CuePolicy(name="")).match_mode
            == MATCH_LEXICAL_OR_COSINE
            and row.embedding is not None
            for row in rows
        )
        if not wants_cosine:
            return None
        embedder = getattr(self, "_embedder", None)
        if embedder is None:
            return None
        try:
            return embedder.embed(text)
        except Exception:
            log.debug("awaiting cue user embed failed", exc_info=True)
            return None

    # ── shared transitions ────────────────────────────────────────────

    def _retire_or_retry(
        self,
        store: "CueStore",
        row: "CueRow",
        policy: CuePolicy,
        *,
        evidence: str,
    ) -> None:
        """She did not raise it. One more chance, then it goes."""
        if row.surfaced_count + 1 >= policy.max_surfacings:
            store.expire(row.id, evidence=f"max_surfacings/{evidence}")
            return
        store.release(row.id, evidence=evidence)

    def _mark_cue_used(
        self, store: "CueStore", row: "CueRow", *, evidence: str,
    ) -> None:
        if not store.mark_used(row.id, evidence=evidence):
            return
        log.info(
            "cue used: type=%s subject=%r after %d surfacing(s) [%s]",
            row.cue_type, row.subject[:60], row.surfaced_count, evidence,
        )
        self._broadcast_cue_pool_update(row)

    # ── the Cues panel ────────────────────────────────────────────────

    def list_cue_pool(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        cue_type: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Paginated pool page plus the per-type scoreboard.

        Both halves in one response because the panel needs them
        together and they come from the same table: the rows say what
        Aiko is holding, ``stats`` says whether holding it is working.
        """
        store = self._cue_pool_store()
        if store is None:
            return {
                "cues": [],
                "count": 0,
                "total": 0,
                "stats": [],
                "types": sorted(CUE_POLICIES),
                "enabled": False,
            }
        rows = store.list_for_user(
            cue_type=cue_type, state=state, limit=limit, offset=offset,
        )
        return {
            "cues": [row.as_dict() for row in rows],
            "count": len(rows),
            "total": store.count_for_user(cue_type=cue_type, state=state),
            "stats": store.stats(),
            "types": sorted(CUE_POLICIES),
            "enabled": True,
        }

    def cue_pool_cadence(self, cue_type: str) -> dict[str, Any]:
        """Where a type stands against its surfacing cooldown.

        For the per-feature MCP state tools, which used to read a
        ``last_fired_at`` watermark of their own. That watermark is gone
        for the types whose pacing moved onto ``CuePolicy``, so this is
        the replacement answer to the question those tools were really
        asking: may Aiko open a new one of these right now, and if not,
        what is she waiting on.
        """
        policy = self._policy_for(cue_type)
        store = self._cue_pool_store()
        last: str | None = None
        if store is not None:
            try:
                last = store.last_surfaced_at(cue_type)
            except Exception:
                log.debug(
                    "cue cadence read failed: type=%s", cue_type, exc_info=True,
                )
        return {
            "surface_cooldown_hours": float(
                getattr(policy, "surface_cooldown_hours", 0.0) or 0.0
            ),
            "last_surfaced_at": last,
            "blocked": self._cadence_blocked(cue_type),
        }

    def discard_cues(self, cue_type: str | None = None) -> int:
        """Retire every live cue of a type, returning how many.

        For the debug surfaces that used to wipe a feature's private kv
        key. Rows are marked ``expired`` rather than deleted, so the
        scoreboard still shows they existed and a subject Aiko already
        spent stays spent -- clearing the shelf should not also hand her
        permission to re-raise everything that was on it.
        """
        store = self._cue_pool_store()
        if store is None:
            return 0
        cleared = 0
        try:
            for state in ("pending", "surfaced", "awaiting"):
                for row in store.in_state(state, cue_type=cue_type, limit=500):
                    if store.expire(row.id, evidence="discarded"):
                        cleared += 1
        except Exception:
            log.debug("cue discard failed: type=%s", cue_type, exc_info=True)
        return cleared

    def cue_pool_stats(self) -> list[dict[str, Any]] | None:
        """The per-type scoreboard on its own, for the MCP debug view.

        Separate from :meth:`list_cue_pool` because the debug tool wants
        the aggregate without paging a single row. ``None`` means there
        is no pool at all, which an empty list would not distinguish
        from a pool nobody has written to yet.
        """
        store = self._cue_pool_store()
        if store is None:
            return None
        return store.stats()

    # ── live updates for the Cues panel ───────────────────────────────

    def add_cue_pool_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        listeners = getattr(self, "_cue_pool_listeners", None)
        if listeners is None:
            listeners = []
            self._cue_pool_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def _broadcast_cue_pool_update(self, row: "CueRow") -> None:
        """Tell the UI a cue was just spent.

        The only live event the pool sends from the turn path --
        everything else is fetch-on-open, since churn is low and workers
        only write while the user is away. This one earns its wire
        because watching a cue flip to ``used`` in the same beat Aiko
        uses it is the clearest possible demonstration that the
        mechanism works.
        """
        payload = dict(row.as_dict())
        payload["state"] = "used"
        self._notify_cue_pool_added(payload)

    def _notify_cue_pool_added(self, payload: dict[str, Any]) -> None:
        """Fan one cue-shaped payload out to the panel listeners."""
        for listener in list(getattr(self, "_cue_pool_listeners", None) or []):
            try:
                listener(dict(payload))
            except Exception:
                log.debug("cue pool listener raised", exc_info=True)


__all__ = ["CuePoolMixin"]
