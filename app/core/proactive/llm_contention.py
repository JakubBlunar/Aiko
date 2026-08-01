"""How badly background LLM work fights the chat path for a GPU (P36).

``memory.idle_worker_tick_budget_ms`` (6000) was sized when one local
Ollama served both the chat path and the background workers: a worker
running mid-conversation stole the GPU, so the budget was really a
*contention* limit wearing a *time* limit's clothes. Split the worker
route onto a different backend and that constraint evaporates, but the
budget could not tell the difference.

This module derives the difference from configuration the app already
has -- comparing the ``main_chat`` and ``worker_default`` routes -- so
the LLM lane can be sized by actual contention instead of by the
strictest case.

Grades
------
``none``
    Different backends, or either side is not local Ollama. Nothing to
    protect.
``queueing``
    Same local Ollama endpoint, *same* model. Worker calls queue behind
    chat inside Ollama. Annoying, not destructive.
``swapping``
    Same local Ollama endpoint, *different* model. The worst case:
    Ollama evicts the chat model to load the worker model, so a
    background call can cost the *next* turn a full model reload even
    though ``keep_alive`` is 30m.

``none`` and ``queueing`` currently produce the same lane multiplier --
only ``swapping`` restricts. They are kept as distinct grades because
they are diagnostically different (``get_idle_workers_status`` reports
which one you are in) and because inventing a behavioural difference
just to justify a third name would be worse than admitting there
isn't one yet.
"""
from __future__ import annotations

import logging

from app.core.infra.settings import (
    LLM_ROLE_MAIN_CHAT,
    LLM_ROLE_WORKER_DEFAULT,
    LlmSettings,
    find_provider,
)


log = logging.getLogger("app.idle_worker_scheduler")


CONTENTION_NONE = "none"
CONTENTION_QUEUEING = "queueing"
CONTENTION_SWAPPING = "swapping"

CONTENTION_GRADES = (
    CONTENTION_NONE,
    CONTENTION_QUEUEING,
    CONTENTION_SWAPPING,
)

CONTENTION_AUTO = "auto"

# Depth tiers at or below this index are "the user could be back any
# second". Under ``swapping`` the LLM lane stays pinned at its base
# budget through them, because being caught mid-call costs a model
# reload on top of the queueing delay.
SWAPPING_SHALLOW_TIER_MAX = 1

_OLLAMA_KIND = "ollama"

# ``localhost`` and ``127.0.0.1`` are the same GPU. Normalising them
# keeps a cosmetic config difference from reading as "split backends"
# and quietly removing the protection.
_LOOPBACK_ALIASES = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")


def _normalise_base_url(raw: str) -> str:
    url = (raw or "").strip().lower().rstrip("/")
    for alias in _LOOPBACK_ALIASES:
        url = url.replace("//" + alias + ":", "//127.0.0.1:")
        if url.endswith("//" + alias):
            url = url[: -len(alias)] + "127.0.0.1"
    return url


def classify_contention(
    llm: LlmSettings,
    *,
    override: str = CONTENTION_AUTO,
) -> str:
    """Grade chat-vs-worker GPU contention from the route topology.

    ``override`` short-circuits the comparison when the topology lies --
    a "remote" endpoint that is really the same GPU box, or a shared
    machine. Anything other than a known grade means auto-detect.

    Auto-detection errs toward the stricter grade whenever the
    comparison is ambiguous (a missing route, an unknown provider): a
    wrong guess in that direction costs some background throughput,
    while the opposite mistake costs the user's first token.
    """
    wanted = (override or "").strip().lower()
    if wanted in CONTENTION_GRADES:
        return wanted

    chat_route = llm.routes.get(LLM_ROLE_MAIN_CHAT)
    worker_route = llm.routes.get(LLM_ROLE_WORKER_DEFAULT)

    if chat_route is None:
        return CONTENTION_SWAPPING
    if worker_route is None:
        # SessionController serves workers from the chat client itself
        # when the worker route is absent, so it is literally the same
        # model on the same endpoint.
        return CONTENTION_QUEUEING

    chat_provider = find_provider(llm, chat_route.provider_id)
    worker_provider = find_provider(llm, worker_route.provider_id)
    if chat_provider is None or worker_provider is None:
        return CONTENTION_SWAPPING

    chat_kind = (chat_provider.kind or "").strip().lower()
    worker_kind = (worker_provider.kind or "").strip().lower()
    if chat_kind != _OLLAMA_KIND or worker_kind != _OLLAMA_KIND:
        # At least one side is not a local Ollama process, so there is
        # no shared VRAM to fight over.
        return CONTENTION_NONE

    if _normalise_base_url(chat_provider.base_url) != _normalise_base_url(
        worker_provider.base_url
    ):
        return CONTENTION_NONE

    chat_model = (chat_route.model or "").strip().lower()
    worker_model = (worker_route.model or "").strip().lower()
    if chat_model and chat_model == worker_model:
        return CONTENTION_QUEUEING
    return CONTENTION_SWAPPING


def llm_lane_multiplier(
    grade: str,
    *,
    tier_index: int,
    depth_multiplier: float,
) -> float:
    """Scale the LLM lane by idle depth, restricted by contention grade.

    ``none`` and ``queueing`` take the depth multiplier unchanged --
    the same treatment the compute lane gets. ``swapping`` is pinned to
    1x through the two shallowest tiers, so a background generation
    cannot cost a returning user a model reload; it opens up from
    ``long_away`` on, where a reload amortises against a long absence.
    """
    mult = max(0.0, float(depth_multiplier))
    if grade == CONTENTION_SWAPPING and int(tier_index) <= SWAPPING_SHALLOW_TIER_MAX:
        return min(mult, 1.0)
    return mult


__all__ = [
    "CONTENTION_AUTO",
    "CONTENTION_GRADES",
    "CONTENTION_NONE",
    "CONTENTION_QUEUEING",
    "CONTENTION_SWAPPING",
    "classify_contention",
    "llm_lane_multiplier",
]
