"""H7/H44: settle a hunch she asked, then keep listening.

Split out of :mod:`app.core.session.post_turn_helpers_mixin` so that
file stays under the size budget. State ownership stays on
``SessionController``.

The two jobs:

* **H7** -- ``_resolve_concept_hypotheses`` owns every awaiting
  ``concept_hypothesis`` cue. An echo miss or a missing classifier does
  **not** burn the one ask; the cue stays awaiting until a later turn
  looks on-subject, the model dodges, or the hold times out. Re-asking
  the same hunch is still forbidden.
* **H44** -- ``_listen_supported_hypotheses`` scores a later turn against
  a guess that already has one asked confirmation. That is the second
  independent event ``is_ready`` needs; it is not a second question.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.infra import timephrase


log = logging.getLogger("app.session")

#: Echo miss / classifier never ran -- keep listening, do not re-ask.
_HOLD_REASONS = frozenset({"off_subject", "no_client", "unparsed"})
#: Off-subject user turns before the hold itself expires.
_AWAITING_HOLD_TURNS = 3
#: Wall-clock bound on the same hold, from ``last_asked_at``.
_AWAITING_HOLD_HOURS = 24.0
_HOLD_COUNT_KEY = "awaiting_holds"


def _embed_or_none(embedder: Any, text: str) -> Any:
    """Vector for ``text``, or ``None`` if that is not possible right now."""
    if embedder is None or not text:
        return None
    try:
        return embedder.embed(text)
    except Exception:
        log.debug("hypothesis resolve embed failed", exc_info=True)
        return None


def _hours_since(stamp: str | None) -> float | None:
    """Age of an ISO stamp in hours, or ``None`` if it will not parse."""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (timephrase.utcnow() - moment).total_seconds() / 3600.0


class HypothesisResolveMixin:
    """Ask-then-learn resolver plus the H44 ambient second confirm."""

    def _resolve_concept_hypotheses(self, *, user_text: str) -> None:
        """L30c: settle every hunch Aiko asked about, before stage B runs.

        Called from ``_post_turn_inner_life`` **immediately before**
        :meth:`_settle_awaiting_cues`, and it owns every
        ``concept_hypothesis`` row in ``awaiting`` for the whole of that
        awaiting life -- including a hold across off-subject turns.
        Generic stage B decides "did they answer?" from topical overlap
        alone, which for this cue type would mark a flat *denial* as a
        successful answer. Stage B therefore skips this type.

        Confirm / correct / deny retire the cue. An LLM dodge expires it
        (do not re-ask). An echo miss or a missing classifier leaves it
        awaiting, bounded by :data:`_AWAITING_HOLD_TURNS` /
        :data:`_AWAITING_HOLD_HOURS`.
        """
        if not bool(
            getattr(
                self._settings.agent, "concept_hypothesis_ask_enabled", True,
            )
        ):
            return
        store = self._cue_pool_store()
        if store is None:
            return
        if (
            getattr(self, "_concept_store", None) is None
            and getattr(self, "_hypothesis_store", None) is None
        ):
            return
        try:
            from app.core.proactive.cue_store import STATE_AWAITING

            rows = store.in_state(
                STATE_AWAITING,
                cue_type="concept_hypothesis",
                with_embedding=True,
            )
        except Exception:
            log.debug("awaiting hypothesis read failed", exc_info=True)
            return
        if not rows:
            return

        from app.core.concepts.answer_adjudicator import UNCLEAR, adjudicate

        body = (user_text or "").strip()
        reply_vec = _embed_or_none(getattr(self, "_embedder", None), body)
        min_cosine = float(
            getattr(
                self._memory_settings,
                "concept_hypothesis_answer_threshold",
                0.45,
            )
        )
        question, question_vec = self._hypothesis_question_echo()
        for row in rows:
            belief = str(row.payload.get("label") or "") or row.subject
            verdict = adjudicate(
                belief=belief,
                reply=body,
                ollama=getattr(self, "_maintenance_client", None),
                model=getattr(self, "_effective_worker_model", "") or "",
                belief_vec=row.embedding,
                reply_vec=reply_vec,
                min_cosine=min_cosine,
                question=question,
                question_vec=question_vec,
                cancel_event=getattr(self, "_fact_check_cancel", None),
            )
            if verdict.verdict == UNCLEAR:
                self._release_unanswered_hypothesis(
                    store, row, verdict.reason,
                )
                continue
            self._write_hypothesis_answer(row, belief, verdict, body)
            self._mark_cue_used(
                store, row, evidence=f"adjudicated/{verdict.verdict}",
            )

    def _hypothesis_question_echo(self) -> tuple[str, Any]:
        """The question she actually asked last turn, for the echo gate.

        Resolve runs before this turn's assistant text is pushed onto
        ``_recent_assistant_turns``, so index 0 is still the previous
        reply. ``_prior_assistant_vec`` is that reply's embedding,
        carried forward at the end of the last post-turn. ``_last_
        assistant_vec`` is already this turn's reply and must not be
        used here.
        """
        question = ""
        ring = getattr(self, "_recent_assistant_turns", None) or ()
        try:
            if ring:
                question = str(ring[0][1] or "")
        except Exception:
            question = ""
        return question, getattr(self, "_prior_assistant_vec", None)

    def _release_unanswered_hypothesis(
        self, store: Any, row: Any, reason: str,
    ) -> None:
        """Hold, release, or retire an unsettled hunch.

        ``off_subject`` / ``no_client`` / ``unparsed`` stay ``awaiting``
        -- she already asked; listening longer is not a second ask. An
        LLM dodge expires at ``max_asks=1``. The release arm is the
        shared shape for a raised ``max_asks``, not live behaviour.
        """
        from datetime import timedelta

        from app.core.proactive.cue_accounting import policy_for

        why = str(reason or "unclear")
        if why in _HOLD_REASONS and not self._hypothesis_hold_timed_out(row):
            self._hold_unanswered_hypothesis(store, row, why)
            return

        policy = policy_for("concept_hypothesis")
        max_asks = int(getattr(policy, "max_asks", 1) or 1)
        cooldown = float(getattr(policy, "reask_cooldown_hours", 24.0) or 0.0)
        evidence = (
            "max_asks/awaiting_timeout"
            if why in _HOLD_REASONS
            else f"max_asks/{why}"
        )
        if int(getattr(row, "ask_count", 0) or 0) >= max_asks:
            store.expire(row.id, evidence=evidence)
            log.info(
                "hypothesis unanswered, retired: subject=%r asks=%d reason=%s",
                row.subject[:60],
                int(getattr(row, "ask_count", 0) or 0),
                why,
            )
            return
        store.release(
            row.id,
            not_before=timephrase.utcnow() + timedelta(hours=cooldown),
            evidence=f"unclear/{why}",
        )

    def _hypothesis_hold_timed_out(self, row: Any) -> bool:
        """True when the off-subject hold has used its turns or its day."""
        holds = int((row.payload or {}).get(_HOLD_COUNT_KEY) or 0)
        if holds + 1 >= _AWAITING_HOLD_TURNS:
            return True
        age_h = _hours_since(getattr(row, "last_asked_at", None))
        return age_h is not None and age_h >= _AWAITING_HOLD_HOURS

    def _hold_unanswered_hypothesis(
        self, store: Any, row: Any, reason: str,
    ) -> None:
        """Leave the cue awaiting and count this off-subject turn."""
        payload = dict(row.payload or {})
        holds = int(payload.get(_HOLD_COUNT_KEY) or 0) + 1
        payload[_HOLD_COUNT_KEY] = holds
        row.payload = payload
        patched = getattr(store, "patch_payload", None)
        if callable(patched):
            patched(row.id, {_HOLD_COUNT_KEY: holds})
        log.info(
            "hypothesis unanswered, still listening: subject=%r "
            "holds=%d reason=%s",
            row.subject[:60],
            holds,
            reason,
        )

    def _listen_supported_hypotheses(self, *, user_text: str) -> None:
        """H44: a second confirmation that is not a second ask.

        After she has put a guess to him once and scored a support, later
        turns that echo the statement can CONFIRM or DENY it. Never
        scores ``asked_count == 0`` -- that is the never-asked ambient
        row H44 refused to graduate. One LLM call per turn, echo-gated.
        """
        if not bool(
            getattr(
                self._settings.agent, "concept_hypothesis_ask_enabled", True,
            )
        ):
            return
        store = getattr(self, "_hypothesis_store", None)
        if store is None:
            return
        body = (user_text or "").strip()
        if not body:
            return
        scored = getattr(self, "_hypothesis_scored_ids", None) or set()
        try:
            candidates = [
                row
                for row in store.list_by(live=True)
                if int(row.asked_count or 0) >= 1
                and int(row.refute_count or 0) == 0
                and int(row.support_count or 0) == 1
                and int(row.hypothesis_id) not in scored
            ]
        except Exception:
            log.debug("supported hypothesis listen read failed", exc_info=True)
            return
        if not candidates:
            return

        from app.core.concepts.answer_adjudicator import (
            adjudicate,
            looks_like_an_answer,
        )

        reply_vec = _embed_or_none(getattr(self, "_embedder", None), body)
        min_cosine = float(
            getattr(
                self._memory_settings,
                "concept_hypothesis_answer_threshold",
                0.45,
            )
        )
        hit = None
        for row in candidates:
            if looks_like_an_answer(
                str(row.statement or ""),
                body,
                belief_vec=getattr(row, "embedding", None),
                reply_vec=reply_vec,
                min_cosine=min_cosine,
            ):
                hit = row
                break
        if hit is None:
            return
        verdict = adjudicate(
            belief=str(hit.statement or ""),
            reply=body,
            ollama=getattr(self, "_maintenance_client", None),
            model=getattr(self, "_effective_worker_model", "") or "",
            belief_vec=getattr(hit, "embedding", None),
            reply_vec=reply_vec,
            min_cosine=min_cosine,
            cancel_event=getattr(self, "_fact_check_cancel", None),
        )
        if not verdict.settles:
            return
        memory_id = self._store_hypothesis_answer(
            str(hit.statement or ""),
            body,
            confirming=verdict.verdict == "confirm",
        )
        self._apply_invented_answer(hit, verdict, memory_id, body)
        log.info(
            "hypothesis ambient verdict: hid=%s verdict=%s",
            hit.hypothesis_id,
            verdict.verdict,
        )

    def _write_hypothesis_answer(
        self, row: Any, belief: str, verdict: Any, user_text: str,
    ) -> None:
        """Store the answer as a memory, then apply it to the target.

        ``target_type`` in the cue payload is what routes this: Phase A's
        grounded candidates take the concept writes, Phase B's inventions
        take the hypothesis-row writes. The adjudicator upstream never
        learns which it was looking at, which is the point -- "did they
        agree?" does not depend on what kind of row the belief lives in.
        """
        from app.core.concepts.answer_adjudicator import CONFIRM
        from app.core.concepts.hypothesis_resolution import apply_verdict

        target_type = str(row.payload.get("target_type") or "concept")
        target_id = int(row.payload.get("target_id") or 0)
        target = self._hypothesis_target(target_type, target_id)
        if target is None:
            log.debug(
                "hypothesis target gone: row=%s type=%s id=%s",
                row.id,
                target_type,
                target_id,
            )
            return
        memory_id = self._store_hypothesis_answer(
            belief, user_text, confirming=verdict.verdict == CONFIRM,
        )
        if target_type == "hypothesis":
            self._apply_invented_answer(target, verdict, memory_id, user_text)
            return

        concept_store = getattr(self, "_concept_store", None)
        apply_verdict(
            store=concept_store,
            concept=target,
            verdict=verdict.verdict,
            memory_id=memory_id,
            penalty=float(
                getattr(
                    self._memory_settings,
                    "concept_hypothesis_deny_penalty",
                    0.25,
                )
            ),
            event_store=getattr(self, "_concept_event_store", None),
            reason=str(getattr(verdict, "reason", "") or ""),
        )

    def _hypothesis_target(self, target_type: str, target_id: int) -> Any:
        """The concept or hypothesis row a cue points at, or ``None``."""
        store = getattr(
            self,
            (
                "_hypothesis_store"
                if target_type == "hypothesis"
                else "_concept_store"
            ),
            None,
        )
        if store is None or target_id <= 0:
            return None
        try:
            return store.get(target_id)
        except Exception:
            return None

    def _apply_invented_answer(
        self, row: Any, verdict: Any, memory_id: int | None, user_text: str,
    ) -> None:
        """The Phase B half: credence, then a graduation check."""
        from app.core.concepts.hypothesis_graduation import graduate, is_ready
        from app.core.concepts.hypothesis_resolution import (
            apply_hypothesis_verdict,
        )

        store = getattr(self, "_hypothesis_store", None)
        if store is None:
            return
        scored = getattr(self, "_hypothesis_scored_ids", None)
        if scored is None:
            scored = set()
            self._hypothesis_scored_ids = scored
        try:
            scored.add(int(row.hypothesis_id))
        except Exception:
            pass
        concept_store = getattr(self, "_concept_store", None)
        embedder = getattr(self, "_embedder", None)
        mem = self._memory_settings
        apply_hypothesis_verdict(
            store=store,
            row=row,
            verdict=verdict.verdict,
            memory_id=memory_id,
            credence_step=float(
                getattr(mem, "hypothesis_credence_step", 0.2)
            ),
            concept_store=concept_store,
            embed=(None if embedder is None else embedder.embed),
            correction_text=user_text,
        )
        if concept_store is None:
            return
        if not is_ready(
            row,
            min_support=int(
                getattr(mem, "hypothesis_graduate_min_support", 2)
            ),
            min_credence=float(
                getattr(mem, "hypothesis_graduate_min_credence", 0.7)
            ),
        ):
            return
        graduate(
            hypothesis_store=store,
            concept_store=concept_store,
            row=row,
            event_store=getattr(self, "_concept_event_store", None),
            memory_writer=self._anchor_world_hypothesis,
            memory_exists=self._answer_memory_exists,
        )

    def _answer_memory_exists(self, memory_id: int) -> bool:
        """Is a remembered answer still there to be cited as evidence?"""
        store = getattr(self, "_memory_store", None)
        if store is None:
            return True
        try:
            return store.get(int(memory_id)) is not None
        except Exception:
            return True

    def _anchor_world_hypothesis(self, statement: str) -> int | None:
        """The ``world`` exit: a proven guess about how something works."""
        memory_store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        body = (statement or "").strip()
        if memory_store is None or embedder is None or not body:
            return None
        try:
            memory = memory_store.add(
                content=body[:1000],
                kind="fact",
                embedding=embedder.embed(body),
                salience=0.6,
                confidence=0.75,
                tier="long_term",
                source_session=getattr(self, "session_key", None),
                metadata={"source": "hypothesis"},
            )
        except Exception:
            log.warning("world hypothesis anchor failed", exc_info=True)
            return None
        if memory is None:
            return None
        try:
            self._notify_memory_added(memory)
        except Exception:
            log.debug("world hypothesis notify failed", exc_info=True)
        return int(memory.id)

    def _store_hypothesis_answer(
        self, belief: str, user_text: str, *, confirming: bool,
    ) -> int | None:
        """Persist what the user actually said, as an ordinary memory."""
        memory_store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        body = (user_text or "").strip()
        if memory_store is None or embedder is None or not body:
            return None
        lead = "confirmed" if confirming else "responded to"
        content = f"Asked about \"{belief}\" -- they {lead}: {body}"[:1000]
        try:
            memory = memory_store.add(
                content=content,
                kind="fact",
                embedding=embedder.embed(content),
                salience=0.65,
                confidence=0.85,
                tier="long_term",
                source_session=getattr(self, "session_key", None),
            )
        except Exception:
            log.warning("hypothesis answer memory write failed", exc_info=True)
            return None
        if memory is None:
            return None
        try:
            self._notify_memory_added(memory)
        except Exception:
            log.debug("hypothesis answer notify failed", exc_info=True)
        return int(memory.id)
