"""Heuristic auto-click ranking for interactive elements.

Pure, deterministic, no embeddings. Each interactive element gets an
``interaction_likelihood`` score combining six weighted signals:

* semantic role  — submit/button/link/input weighting
* visibility     — hidden / disabled elements sink
* position       — earlier in document order (and top-left bbox) ranks up
* text meaning   — accessible-name salience + action keywords
* page context   — sits under a heading breadcrumb
"""
from __future__ import annotations

from dataclasses import dataclass

from .accessibility import A11yNode


@dataclass(frozen=True, slots=True)
class RankingWeights:
    role: float = 1.0
    visibility: float = 1.0
    position: float = 1.0
    text: float = 1.0
    context: float = 1.0


@dataclass(frozen=True, slots=True)
class RankedElement:
    node: A11yNode
    score: float
    context: str


# Base desirability by role (pre-weight). Submit handled separately.
_ROLE_BASE: dict[str, float] = {
    "button": 0.8,
    "link": 0.6,
    "textbox": 0.7,
    "searchbox": 0.75,
    "combobox": 0.65,
    "checkbox": 0.55,
    "radio": 0.55,
    "switch": 0.55,
    "tab": 0.6,
    "menuitem": 0.55,
    "option": 0.4,
    "slider": 0.45,
    "select": 0.65,
    "textarea": 0.65,
    "input": 0.65,
}

_ACTION_KEYWORDS = (
    "search",
    "submit",
    "login",
    "log in",
    "sign in",
    "sign up",
    "continue",
    "next",
    "buy",
    "add to cart",
    "checkout",
    "place order",
    "save",
    "send",
    "download",
    "apply",
    "confirm",
    "accept",
    "subscribe",
)


def _role_score(node: A11yNode) -> float:
    if node.is_submit:
        return 1.0
    return _ROLE_BASE.get(node.role, 0.3)


def _visibility_score(node: A11yNode) -> float:
    if not node.visible:
        return 0.0
    if node.disabled:
        return 0.2
    return 1.0


def _position_score(node: A11yNode, total: int) -> float:
    order_score = 1.0 - (node.dom_order / total) if total > 0 else 0.5
    if node.bbox is not None:
        x, y, _w, _h = node.bbox
        # Above-the-fold and leftward elements get a mild lift.
        top = 1.0 if y < 800 else max(0.0, 1.0 - (y - 800) / 4000.0)
        left = 1.0 if x < 1000 else 0.6
        return max(0.0, min(1.0, 0.5 * order_score + 0.35 * top + 0.15 * left))
    return order_score


def _text_score(node: A11yNode) -> float:
    name = node.name.strip().lower()
    if not name:
        return 0.1
    tokens = [t for t in name.split() if len(t) > 1]
    salience = min(1.0, len(tokens) / 4.0)
    if any(kw in name for kw in _ACTION_KEYWORDS):
        salience = min(1.0, salience + 0.4)
    return max(0.15, salience)


def _context_score(context: str) -> float:
    return 1.0 if context.strip() else 0.3


def score_element(
    node: A11yNode, context: str, total: int, weights: RankingWeights
) -> float:
    """Weighted sum of the six signals, normalized by total weight."""
    parts = (
        (weights.role, _role_score(node)),
        (weights.visibility, _visibility_score(node)),
        (weights.position, _position_score(node, total)),
        (weights.text, _text_score(node)),
        (weights.context, _context_score(context)),
    )
    weight_sum = sum(w for w, _ in parts)
    if weight_sum <= 0:
        return 0.0
    raw = sum(w * s for w, s in parts) / weight_sum
    # Invisible elements are effectively unusable regardless of weights.
    if not node.visible:
        raw *= 0.1
    return round(raw, 4)


def rank_elements(
    nodes: list[A11yNode],
    context_map: dict[int, str],
    weights: RankingWeights,
    max_elements: int,
) -> list[RankedElement]:
    """Score every interactive node, sort by score (then doc order), cap."""
    total = len(nodes)
    ranked: list[RankedElement] = []
    for node in nodes:
        if not node.is_interactive:
            continue
        ctx = context_map.get(node.dom_order, "")
        ranked.append(
            RankedElement(
                node=node,
                score=score_element(node, ctx, total, weights),
                context=ctx,
            )
        )
    ranked.sort(key=lambda r: (-r.score, r.node.dom_order))
    if max_elements > 0:
        ranked = ranked[:max_elements]
    return ranked


__all__ = [
    "RankingWeights",
    "RankedElement",
    "score_element",
    "rank_elements",
]
