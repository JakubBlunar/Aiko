"""K1 follow-up — first-run onboarding goal seed.

When Jacob (or whoever) completes onboarding by setting their
``user_display_name`` for the first time, this module seeds exactly
one curated, pinned ``goal`` row into Aiko's long-term-goal ring:

> Get to know {user_name}. Pay attention to what they care about
> — what they're building lately, what wears them down, what makes
> them laugh, the rhythms of their weeks. Not by interrogating,
> but by noticing across many small turns. This goal never
> finishes; the point is to keep listening.

That single seeded goal:

- **Tripwires the K1 LLM bootstrap.** ``GoalWorker._run_bootstrap``
  short-circuits when ``GoalStore.has_any_active()`` is ``True``,
  so this one row prevents the empty-store bootstrap from
  ever firing. Aiko picks up additional goals organically through
  ``[[goal:...]]`` self-tags during real conversation instead of
  from a cold-start LLM pass with no signal.
- **Is pinned by default.** Survives ``prune_overflow``, doesn't
  count against ``memory.goal_max_active=5``, durable presence in
  the prompt's "Long-term goals" block.
- **Carries ``metadata.source="onboarding_seed"``** so it's
  distinguishable from ``self_tag`` / ``worker_bootstrap`` /
  manual REST writes in tests, MCP debug output, and the Memory
  drawer.
- **Is one-shot via ``kv_meta``.** Once seeded, the
  ``goals.onboarding_goal_seeded`` row is set; even if Jacob
  deletes the goal afterwards it never re-seeds. Respecting user
  agency is more important than guaranteeing the goal exists.

Reflection cadence is unchanged: ``GoalWorker._run_reflection``
picks the seeded goal up on its hourly tick like any other goal
and writes ``goal_progress`` notes against it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING


log = logging.getLogger("app.onboarding_goal")


# ── Constants ───────────────────────────────────────────────────────


_ONBOARDING_GOAL_KV_KEY = "goals.onboarding_goal_seeded"

# Curated wording. Neutral pronouns to stay consistent with the
# persona file's existing "they / you" treatment. ``{user_name}`` is
# the only template token. The leading "Get to know {user_name}."
# acts as the goal title under the existing summary-to-title
# convention in :mod:`app.core.goals.goal_store`.
_ONBOARDING_GOAL_TEMPLATE = (
    "Get to know {user_name}. Pay attention to what they care "
    "about — what they're building lately, what wears them down, "
    "what makes them laugh, the rhythms of their weeks. Not by "
    "interrogating, but by noticing across many small turns. This "
    "goal never finishes; the point is to keep listening."
)

# Fallback name used when the caller passes empty / whitespace.
# Matches :func:`app.core.infra.settings.resolve_user_display_name`'s
# fallback so a misconfigured boot still produces a usable goal,
# although the real onboarding gate (``not is_onboarding_needed``)
# already prevents this path in the controller.
_FALLBACK_NAME = "friend"


if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.goals.goal_store import GoalStore
    from app.core.memory.memory_store import Memory, MemoryStore


# ── Public API ──────────────────────────────────────────────────────


def is_onboarding_goal_seeded(chat_db: "ChatDatabase") -> bool:
    """Cheap read-only check: has the onboarding seed already run?

    Reads the ``goals.onboarding_goal_seeded`` row from ``kv_meta``.
    Returns ``False`` on missing row / SQL error (fail-open so a
    fresh boot with a broken kv_meta table still gets the seed).
    """
    try:
        return chat_db.kv_get(_ONBOARDING_GOAL_KV_KEY) is not None
    except Exception:
        log.debug("kv_meta read failed; treating seed as not-run", exc_info=True)
        return False


def seed_onboarding_goal(
    *,
    goal_store: "GoalStore",
    memory_store: "MemoryStore",
    chat_db: "ChatDatabase",
    user_display_name: str,
    force: bool = False,
) -> "Memory | None":
    """Insert the curated onboarding goal once.

    Returns the inserted :class:`Memory` on success. Returns
    ``None`` when:

    - The ``kv_meta`` flag is already set and ``force`` is ``False``
      (idempotent — the dominant case after the first call).
    - ``goal_store.add_goal()`` returned ``None`` (dedupe collision,
      embed failure, missing embedder — see
      :meth:`app.core.goals.goal_store.GoalStore.add_goal`).
    - Pinning failed silently after insert; the goal still lands
      but the kv_meta flag is set anyway to prevent a retry storm.

    All failures are logged but never raised — this runs in the
    identity-change critical path and must never block the user's
    first message.
    """
    # ── 1. Idempotency gate ─────────────────────────────────────
    if not force and is_onboarding_goal_seeded(chat_db):
        log.debug("onboarding-goal: kv_meta flag set; skipping seed")
        return None

    # ── 2. Resolve the user-name token ──────────────────────────
    name = (user_display_name or "").strip() or _FALLBACK_NAME
    summary = _ONBOARDING_GOAL_TEMPLATE.format(user_name=name)

    # ── 3. Insert the goal row ──────────────────────────────────
    try:
        mem = goal_store.add_goal(
            summary=summary,
            source="onboarding_seed",
        )
    except Exception:
        log.warning("onboarding-goal: add_goal raised", exc_info=True)
        return None
    if mem is None:
        # Cosine-dedupe collision OR embed failure. We still set
        # the kv_meta flag so we don't retry on every identity
        # update — the user's preferences are clearly already
        # being respected even if our insert was a no-op.
        _mark_seeded(chat_db, when=_now_iso())
        log.info(
            "onboarding-goal: add_goal returned None (dedupe/embed); "
            "flag set anyway",
        )
        return None

    # ── 4. Pin the new row ──────────────────────────────────────
    try:
        memory_store.set_pinned(int(mem.id), True)
    except Exception:
        log.debug(
            "onboarding-goal: set_pinned failed for mem id=%s",
            getattr(mem, "id", "?"),
            exc_info=True,
        )

    # ── 5. Stamp the kv_meta flag ───────────────────────────────
    _mark_seeded(chat_db, when=_now_iso())
    log.info(
        "onboarding-goal: seeded mem_id=%s user=%s source=onboarding_seed pinned=True",
        getattr(mem, "id", "?"),
        name,
    )
    return mem


# ── Internals ───────────────────────────────────────────────────────


def _mark_seeded(chat_db: "ChatDatabase", *, when: str) -> None:
    try:
        chat_db.kv_set(_ONBOARDING_GOAL_KV_KEY, when)
    except Exception:
        log.debug(
            "onboarding-goal: kv_set failed; seed will retry next time",
            exc_info=True,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "is_onboarding_goal_seeded",
    "seed_onboarding_goal",
]
