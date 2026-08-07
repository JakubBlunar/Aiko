"""The L30 hypothesis facade: what Aiko is unsure about, plus a way to poke it.

Two audiences, one file, and the split between them is the point:

* **the read Aiko gets** -- :meth:`open_hypotheses` backs the
  ``recall_hypotheses`` tool and ``GET /api/concepts/hypotheses``. It is
  deliberately narrow: live rows only, linked rows dropped, the two
  origins unified in shape and distinguished by an ``origin`` field.
* **the read a debugger gets** -- :meth:`hypothesis_shelf` is the
  opposite. Every status including the closed ones, linked rows
  included, every lifecycle field exposed. The rows this layer hides
  from Aiko are exactly the ones that explain why nothing is happening.

The write half (:meth:`force_hypothesis_verdict`) exists because the
lifecycle is otherwise untestable by hand: walking a guess to graduation
needs two adjudicated confirmations across two conversations. It routes
through ``_apply_invented_answer`` -- the *live* post-turn writer -- rather
than reimplementing the credence math, so a forced verdict cannot drift
from a real one. That is the whole design constraint; see
``docs/hypotheses.md``.

State ownership (``self._hypothesis_store``, ``self._concept_store``,
``self._memory_settings``, ``self._agent_settings``) lives in
``SessionController.__init__`` -- do not move it here.
"""
from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("app.session")

#: Verdicts a human may force. ``unclear`` is missing on purpose: it is
#: the "writes nothing" outcome, so offering it as a button would be a
#: control that does nothing by design.
FORCEABLE_VERDICTS: tuple[str, ...] = ("confirm", "correct", "deny")


class HypothesisDebugMixin:
    """L30 reads for Aiko and for whoever is debugging her."""

    # ── L30 Phase B: the open guesses ────────────────────────────────────

    def open_hypotheses(
        self,
        *,
        subject: str | None = None,
        kind: str | None = None,
        origin: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """What Aiko is still unsure about, invented and grounded both.

        Backs ``GET /api/concepts/hypotheses`` and the
        ``recall_hypotheses`` tool. Living here rather than in the tool is
        what keeps the private-reach guard satisfied: the tool holds a
        bound method, not a store.

        The two origins are unified in the *shape* and kept distinct in
        the ``origin`` field. ``invented`` rows come from the
        ``hypotheses`` table; ``grounded`` ones are candidate concepts the
        L30a lane would surface, which have no row of their own and are
        derived on read. Never raises.
        """
        rows: list[dict[str, Any]] = []
        want = (origin or "").strip().lower() or None
        if want in (None, "invented"):
            rows += self._invented_hypothesis_rows(subject, kind)
        if want in (None, "grounded"):
            rows += self._grounded_hypothesis_rows(subject, kind)
        # Least settled first: the point of looking is to find what is
        # most open, not what is nearly decided.
        rows.sort(key=lambda r: -float(r.get("unsettled") or 0.0))
        return {
            "enabled": bool(rows) or self._hypothesis_layer_live(),
            "total": len(rows),
            "hypotheses": rows[: max(1, int(limit))],
        }

    def hypothesis_state(self) -> dict[str, Any]:
        """Shelf stock and the caps around it, for debugging the layer.

        Answers the two questions an empty lane raises: is the shelf bare
        (``live`` at zero) or is it full of rows nothing will surface
        (``live`` at ``max_open`` with everything ``linked``)?
        """
        ms = getattr(self, "_memory_settings", None)
        out: dict[str, Any] = {
            "invention_enabled": bool(
                getattr(
                    getattr(self, "_agent_settings", None),
                    "hypothesis_invention_enabled",
                    False,
                )
            ),
            "ask_enabled": bool(
                getattr(
                    getattr(self, "_agent_settings", None),
                    "concept_hypothesis_ask_enabled",
                    False,
                )
            ),
            "max_open": getattr(ms, "hypothesis_max_open", None),
            "ttl_hours": getattr(ms, "hypothesis_ttl_hours", None),
            "graduate_min_support": getattr(
                ms, "hypothesis_graduate_min_support", None
            ),
            "graduate_min_credence": getattr(
                ms, "hypothesis_graduate_min_credence", None
            ),
        }
        store = getattr(self, "_hypothesis_store", None)
        if store is None:
            out.update({"store": False, "live": 0, "by_status": {}})
            return out
        try:
            out.update(
                {
                    "store": True,
                    "live": int(store.count_live()),
                    "by_status": store.counts_by_status(),
                    "linked": len(store.list_by(linked=True)),
                }
            )
        except Exception:
            log.debug("hypothesis state read failed", exc_info=True)
            out.update({"store": True, "live": 0, "by_status": {}})
        return out

    def _hypothesis_layer_live(self) -> bool:
        return (
            getattr(self, "_hypothesis_store", None) is not None
            or getattr(self, "_concept_store", None) is not None
        )

    def _invented_hypothesis_rows(
        self, subject: str | None, kind: str | None,
    ) -> list[dict[str, Any]]:
        store = getattr(self, "_hypothesis_store", None)
        if store is None:
            return []
        try:
            from app.core.concepts.concept_hypothesis import unsettledness
            from app.core.concepts.hypothesis_lane import ORIGIN_INVENTED

            rows = store.list_by(live=True, subject=subject, kind=kind)
        except Exception:
            log.debug("invented hypothesis read failed", exc_info=True)
            return []
        return [
            {
                "origin": ORIGIN_INVENTED,
                "hypothesis_id": int(row.hypothesis_id),
                "statement": str(row.statement),
                "kind": str(row.kind),
                "subject": str(row.subject),
                "rationale": str(row.rationale or ""),
                "credence": round(float(row.credence), 3),
                "support_count": int(row.support_count),
                "asked_count": int(row.asked_count),
                "unsettled": round(float(unsettledness(row)), 3),
                "linked_concept_id": row.linked_concept_id,
                "created_at": row.created_at,
            }
            for row in rows
            # A linked row's belief is already spoken for by a concept, so
            # listing it would show one thing twice with two different
            # confidence stories attached.
            if row.linked_concept_id is None
        ]

    def _grounded_hypothesis_rows(
        self, subject: str | None, kind: str | None,
    ) -> list[dict[str, Any]]:
        store = getattr(self, "_concept_store", None)
        if store is None:
            return []
        try:
            from app.core.concepts.concept_hypothesis import unsettledness
            from app.core.concepts.hypothesis_lane import ORIGIN_GROUNDED

            rows = store.list_by(
                status="candidate", subject=subject, kind=kind,
            )
        except Exception:
            log.debug("grounded hypothesis read failed", exc_info=True)
            return []
        ms = getattr(self, "_memory_settings", None)
        min_unsettled = float(
            getattr(ms, "hypothesis_min_unsettled", 0.22) if ms else 0.22
        )
        min_sources = int(
            getattr(ms, "hypothesis_min_sources", 1) if ms else 1
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            if int(getattr(row, "distinct_source_count", 0) or 0) < min_sources:
                continue
            unsettled = float(unsettledness(row))
            if unsettled < min_unsettled:
                continue
            out.append(
                {
                    "origin": ORIGIN_GROUNDED,
                    "concept_id": int(row.concept_id),
                    "statement": str(row.label),
                    "kind": str(row.kind),
                    "subject": str(row.subject),
                    "rationale": str(getattr(row, "rationale", "") or ""),
                    "confidence": round(float(row.confidence), 3),
                    "distinct_source_count": int(row.distinct_source_count),
                    "unsettled": round(unsettled, 3),
                    "created_at": row.created_at,
                }
            )
        return out

    # ── the debug surface ────────────────────────────────────────────────

    def hypothesis_shelf(
        self, *, subject: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        """Everything on the shelf, including what Aiko is never shown.

        The inverse of :meth:`open_hypotheses` by design. That read hides
        closed and linked rows because Aiko should not muse about a guess
        that is finished or already spoken for by a concept -- but those
        are precisely the rows that explain a quiet lane, so a debugger
        needs them and gets the full lifecycle with them.

        ``grounded`` comes back unchanged from the Aiko-facing helper:
        candidate concepts have no row of their own, so there is no
        hidden state to reveal. Never raises.
        """
        out: dict[str, Any] = {
            "state": self.hypothesis_state(),
            "invented": self._shelf_rows(subject, status),
            "grounded": (
                [] if status else self._grounded_hypothesis_rows(subject, None)
            ),
        }
        out["forceable_verdicts"] = list(FORCEABLE_VERDICTS)
        return out

    def _shelf_rows(
        self, subject: str | None, status: str | None,
    ) -> list[dict[str, Any]]:
        store = getattr(self, "_hypothesis_store", None)
        if store is None:
            return []
        try:
            from app.core.concepts.concept_hypothesis import unsettledness

            rows = store.list_by(subject=subject, status=status)
        except Exception:
            log.debug("hypothesis shelf read failed", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                unsettled = round(float(unsettledness(row)), 3)
            except Exception:
                unsettled = 0.0
            out.append(
                {
                    "hypothesis_id": int(row.hypothesis_id),
                    "statement": str(row.statement),
                    "kind": str(row.kind),
                    "subject": str(row.subject),
                    "rationale": str(row.rationale or ""),
                    "origin": str(row.origin),
                    "origin_refs": list(row.origin_refs or []),
                    "status": str(row.status),
                    "credence": round(float(row.credence), 3),
                    "support_count": int(row.support_count),
                    "refute_count": int(row.refute_count),
                    "asked_count": int(row.asked_count),
                    "unsettled": unsettled,
                    "live": bool(row.is_live),
                    "linked_concept_id": row.linked_concept_id,
                    "graduated_concept_id": row.graduated_concept_id,
                    "graduated_memory_id": row.graduated_memory_id,
                    "answer_memory_ids": list(row.answer_memory_ids or []),
                    "created_at": row.created_at,
                    "last_tested_at": row.last_tested_at,
                    "closed_at": row.closed_at,
                }
            )
        return out

    def force_hypothesis_verdict(
        self, hypothesis_id: int, verdict: str, text: str = "",
    ) -> dict[str, Any]:
        """Answer one of Aiko's guesses by hand, as if the user had.

        Goes through ``_apply_invented_answer`` -- the same writer the
        post-turn resolver uses -- rather than reimplementing any of it.
        A forced confirm therefore stamps ``linked_concept_id``, and
        graduates the row when it reaches the bar, exactly as a real
        answer would. Reimplementing the credence math here would give a
        debug path that slowly stopped telling the truth about the real
        one.

        ``text`` stands in for what the user would have said, and is not
        cosmetic: it is stored as the ordinary ``fact`` memory the live
        path stores, which is the *only* evidence a graduated concept
        inherits. Confirming with no text mints a concept with zero
        evidence edges that L3 then demotes straight back, so a confirm
        without text is refused rather than quietly producing a
        misleading result.

        Returns a before/after diff -- ``_apply_invented_answer`` returns
        nothing, so the row is re-read afterwards instead of changing the
        live path's signature for a debug caller's benefit.
        """
        wanted = (verdict or "").strip().lower()
        if wanted not in FORCEABLE_VERDICTS:
            raise ValueError(
                f"verdict must be one of {', '.join(FORCEABLE_VERDICTS)}"
            )
        store = getattr(self, "_hypothesis_store", None)
        if store is None:
            raise RuntimeError("the hypotheses table is not available")
        row = store.get(int(hypothesis_id))
        if row is None:
            raise LookupError(f"no hypothesis {hypothesis_id}")
        body = " ".join(str(text or "").split())
        if wanted == "confirm" and not body:
            raise ValueError(
                "a forced confirm needs the answer text: it becomes the "
                "memory a graduated concept rests on, and without it "
                "graduation mints a concept with no evidence"
            )
        before = _row_summary(row)

        from app.core.concepts.answer_adjudicator import AnswerVerdict

        memory_id = self._store_hypothesis_answer(
            str(row.statement), body, confirming=wanted == "confirm",
        )
        self._apply_invented_answer(
            row,
            AnswerVerdict(verdict=wanted, reason="forced from the debug panel"),
            memory_id,
            body,
        )
        after_row = store.get(int(hypothesis_id))
        log.info(
            "hypothesis verdict forced: hid=%s verdict=%s memory=%s",
            hypothesis_id,
            wanted,
            memory_id,
        )
        return {
            "verdict": wanted,
            "answer_memory_id": memory_id,
            "before": before,
            "after": _row_summary(after_row) if after_row else None,
        }

    def delete_hypothesis(self, hypothesis_id: int) -> bool:
        """Drop one row outright. Did anything go?

        Unlike closing it as ``refuted``, this leaves nothing behind for
        the novelty gate to see, so the same guess can be invented again
        -- which is what makes it the right button for clearing out test
        rows and the wrong one for "the user said no".
        """
        store = getattr(self, "_hypothesis_store", None)
        if store is None or int(hypothesis_id) <= 0:
            return False
        if store.get(int(hypothesis_id)) is None:
            return False
        store.delete(int(hypothesis_id))
        return store.get(int(hypothesis_id)) is None


def _row_summary(row: Any) -> dict[str, Any]:
    """The fields a verdict can move, for the before/after diff."""
    return {
        "status": str(getattr(row, "status", "")),
        "credence": round(float(getattr(row, "credence", 0.0) or 0.0), 3),
        "support_count": int(getattr(row, "support_count", 0) or 0),
        "refute_count": int(getattr(row, "refute_count", 0) or 0),
        "statement": str(getattr(row, "statement", "")),
        "linked_concept_id": getattr(row, "linked_concept_id", None),
        "graduated_concept_id": getattr(row, "graduated_concept_id", None),
        "graduated_memory_id": getattr(row, "graduated_memory_id", None),
    }


__all__ = ["FORCEABLE_VERDICTS", "HypothesisDebugMixin"]
