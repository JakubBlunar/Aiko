"""K66 — earned familiarity ("well-trodden ground between us") (pure scoring).

F10h reads how a topic *feels* (warm / tender); F10i reads how much Aiko
*knows* about it (thin / familiar, weighted by learned-fact coverage). K66
is the third, orthogonal axis: how **deep the shared history** on a topic
is — how many times the pair has returned to this territory together.

When the live turn lands on a **high-mass** cluster (one with many
accumulated memories), Aiko has earned genuine conversational fluency
there, and a long relationship should sound like it: she can lean on the
shorthand they've built, skip the 101-level scaffolding, and assume the
context they both already share.

The signal is deliberately **pure cluster mass** — the member count, a
proxy for "how many times we've been here". It is *not* knowledge-weighted
(that's F10i's job), so K66 fires on the big-but-unstudied *conversational*
clusters F10i leaves silent (a 16-member cluster of pure chat scores ~0.6
in F10i — below its familiar band — yet is exactly the "we keep coming back
to this" territory K66 owns).

The cue teaches **register, never a stated fact**: counting it out loud
("we've discussed this 14 times") is precisely the failure mode. Pure +
numpy-free: the provider does the embed + cluster match + size read; this
module only bands the resulting count.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FamiliarityRead:
    """Scored shared-history depth for one topic cluster.

    ``band`` is ``"deep"`` (well-worn shared ground → shorthand register)
    or ``None`` when the cluster hasn't accumulated enough shared history
    to read as deep.
    """

    size: int
    band: str | None


def score_familiarity(size: int, *, deep_threshold: int = 14) -> FamiliarityRead:
    """Band a cluster's shared-history depth from its member count.

    A single band: ``deep`` when ``size >= deep_threshold``, else silent
    (``None``). There is no "shallow" band — thinness is F10i's territory,
    not K66's.
    """
    sz = max(0, int(size))
    band = "deep" if sz >= max(1, int(deep_threshold)) else None
    return FamiliarityRead(size=sz, band=band)


def render_block(
    read: FamiliarityRead, label: str, user_display_name: str,
) -> str:
    """Render the one-line register nudge, or ``""`` when not deep.

    A private register cue (like the relationship-axes block), never a line
    said aloud. Distinct from F10i's *familiar* band (which is about not
    over-hedging on knowledge): this is about **conversational shorthand** —
    the depth of shared history letting her skip the recap and assume
    context — and it explicitly forbids quantifying the history.
    """
    if read.band != "deep":
        return ""
    name = (user_display_name or "them").strip() or "them"
    topic = (label or "this topic").strip() or "this topic"
    return (
        f'Heads-up: "{topic}" is well-worn ground between you and {name} — '
        "you've circled back to it together many times, so you've earned real "
        "fluency here. Let that shared history show as register, not as a "
        "stated fact: lean on the shorthand you've built, skip the 101-level "
        "recap, and assume the context you both already share. Never count it "
        'out loud ("we\'ve been over this so many times" said aloud is exactly '
        "wrong) — just talk like two people who already know this territory."
    )


__all__ = ["FamiliarityRead", "score_familiarity", "render_block"]
