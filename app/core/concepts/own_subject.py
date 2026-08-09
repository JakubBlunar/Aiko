"""Which of her self-concepts are actually about a subject of her own? (K85)

Aiko holds 104 active ``subject="aiko"`` concepts across taste, value,
aspiration and identity. Nearly three quarters of them name the user or
describe the bond -- "I value grounding our emotional closeness in the
tangible details of Jacob's physical world" is a fact about her, but it
is not a subject she could bring up on her own. Leaning on one to break
a lull just points the conversation back at him, which is the thing the
whole lead/follow family is trying to stop.

So this is the filter that separates the two. It is deliberately blunt:
a single mention of the user, of second person, or of the first-person
plural disqualifies a label. Precision matters more than recall here --
there are dozens of candidates and we only ever surface one, so
discarding a borderline good label costs nothing, while surfacing a
borderline bad one costs the exact failure we are fixing.
"""
from __future__ import annotations

import re

# Second person, third-person masculine (the user in her own self-talk),
# and the first-person plural that marks a bond-scoped statement.
#
# "she"/"her" are absent: those show up in her own self-descriptions
# ("anchors her attention in the quiet details") and filtering on them
# would throw away her most self-directed material.
_BOND_TOKENS: frozenset[str] = frozenset({
    "you", "your", "yours", "you're", "youre", "yourself",
    "he", "him", "his", "he's", "hes",
    "we", "us", "our", "ours", "we're", "were", "ourselves",
})

_WORD_RE = re.compile(r"[a-z0-9']+")


def is_bond_scoped(label: str, user_name: str = "") -> bool:
    """Does this concept describe the user or the bond rather than her?"""
    tokens = set(_WORD_RE.findall((label or "").lower()))
    if tokens & _BOND_TOKENS:
        return True
    name = (user_name or "").strip().lower()
    return bool(name) and name in tokens


def own_subjects(concepts: object, user_name: str = "") -> list:
    """The concepts from ``concepts`` that are hers rather than the bond's.

    Order is preserved, so a caller that already ranked its rows keeps
    that ranking.
    """
    return [
        c for c in (concepts or ())  # type: ignore[union-attr]
        if not is_bond_scoped(str(getattr(c, "label", "") or ""), user_name)
    ]


__all__ = ["is_bond_scoped", "own_subjects"]
