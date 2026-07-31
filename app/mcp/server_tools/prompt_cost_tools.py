"""P31a -- per-block prompt token costs, ranked by what they actually cost.

The resting system prompt is the dominant term in context occupancy, and
until now the only visible breakdown was a handful of coarse buckets
(``persona_tokens``, ``ambient_tokens``, …). This reports every block the
prompt-cache tier ladder knows about, with its token cost and its tier.

Why the tier matters more than the raw size: OpenAI-style prompt caching
discounts the *stable prefix*, so 2,000 tokens sitting in T0 are paid
once and then discounted, while 200 tokens in T6 are paid at full price
on every single turn. Ranking by size alone would send you trimming the
persona (mostly cached) and ignore a chatty detector block (never
cached). The ``effective`` ranking applies a per-tier weight so the list
reads in cost order rather than byte order.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")

# Rough "fraction of turns where this block's tokens are NOT served from
# the cached prefix". T0 changes on a persona edit; T6 changes every
# turn. These are deliberately coarse -- the point is to stop a 20k
# cached persona from outranking a 300-token per-turn block, not to
# predict a bill.
_TIER_WEIGHT: dict[str, float] = {
    "T0_stable": 0.05,
    "T1_semi_stable": 0.2,
    "T2_summary": 0.2,
    "T3_rag": 0.6,
    "T4_ambient": 0.8,
    "T5_affect_style": 1.0,
    "T6_detectors": 1.0,
}


def build_report(session: "SessionController", *, top: int = 25) -> dict[str, Any]:
    from app.core.session.prompt_assembler import _BLOCK_TIER_OF
    from app.llm.token_utils import chars_per_token

    snapshot = session.get_last_system_prompt()
    chars: dict[str, int] = dict(snapshot.get("block_chars") or {})
    if not chars:
        return {
            "available": False,
            "reason": (
                "no assembly recorded yet -- send a message first "
                "(send_message with skip_tts=true is enough)"
            ),
        }

    # Same ratio ``estimate_tokens`` uses, applied to the stored char
    # counts directly (the block text itself is long gone by now).
    ratio = max(0.1, chars_per_token())
    rows: list[dict[str, Any]] = []
    for name, size in chars.items():
        tier = _BLOCK_TIER_OF.get(name, "unregistered")
        weight = _TIER_WEIGHT.get(tier, 1.0)
        tokens = max(1, int(size / ratio)) if size else 0
        rows.append({
            "block": name,
            "tier": tier,
            "chars": int(size),
            "tokens": int(tokens),
            "effective_tokens": round(tokens * weight, 1),
        })

    by_tier: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_tier.setdefault(
            row["tier"], {"blocks": 0, "rendered": 0, "tokens": 0},
        )
        bucket["blocks"] += 1
        bucket["tokens"] += int(row["tokens"])
        if row["chars"]:
            bucket["rendered"] += 1

    rendered = [r for r in rows if r["chars"]]
    ranked = sorted(
        rendered, key=lambda r: r["effective_tokens"], reverse=True,
    )[: max(1, int(top))]
    # Blocks that ran and produced nothing are the cheapest possible
    # win when there are many of them: each one is a provider call and a
    # branch per turn, and a *large* count here means the prompt is
    # mostly conditional (which is healthy) or that gating is happening
    # too late (which is P31's lazy-render idea).
    silent = sorted(r["block"] for r in rows if not r["chars"])
    total_tokens = sum(int(r["tokens"]) for r in rows)
    return {
        "available": True,
        "captured_at": snapshot.get("captured_at"),
        "mode": snapshot.get("mode"),
        # The assembler's own system-token count, for cross-checking the
        # estimate: block tokens + inter-block separators should land near
        # it. A large gap means content is reaching the prompt from
        # somewhere the ladder doesn't know about.
        "system_tokens_reported": snapshot.get("system_tokens"),
        "block_tokens_total": total_tokens,
        "chars_per_token": round(ratio, 3),
        "blocks_registered": len(rows),
        "blocks_rendered": len(rendered),
        "by_tier": by_tier,
        "top_by_effective_cost": ranked,
        "silent_blocks": silent,
        "reading_guide": (
            "effective_tokens = tokens x per-tier cache-miss weight, so a "
            "T6 block outranks an equally large T0 block. Trim the top of "
            "top_by_effective_cost first. tokens are estimates from the "
            "live chars/token EMA, not a tokenizer."
        ),
    }


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_prompt_block_costs(top: int = 25) -> str:
        """P31a -- rank the last turn's prompt blocks by token cost x tier.

        Returns every block in the prompt-cache tier ladder with its
        character count, estimated tokens, and an ``effective_tokens``
        figure that weights the raw size by how often that tier falls
        outside the cached prefix. Use it to answer "what is the resting
        system prompt actually spent on, and which parts do I pay for on
        every turn?" -- the raw sizes alone will send you after the
        persona, which is mostly cache-discounted.

        Also reports ``silent_blocks`` (registered blocks that rendered
        empty this turn) and a per-tier rollup. Needs at least one
        completed turn: call ``send_message("hi", skip_tts=true)`` first.
        """
        try:
            return json.dumps(build_report(session, top=top), indent=2, default=str)
        except Exception as exc:
            return f"get_prompt_block_costs failed: {exc}"
