"""Normalized accessibility-tree schema — the server-agnostic contract.

Every MCP browser server renders its accessibility snapshot a little
differently (indented YAML-ish tree, JSON, custom text). A per-server
adapter parses that into a flat list of :class:`A11yNode`, and the rest
of the perception pipeline only ever sees ``A11yNode`` — so swapping the
MCP server means writing one adapter, nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


# Roles that are directly actionable / clickable. Kept broad so a new
# server's role vocabulary still classifies sensibly; ranking handles
# fine-grained priority.
INTERACTIVE_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "switch",
        "slider",
        "spinbutton",
        "textarea",
        "input",
        "select",
    }
)

# Roles that hold text the user types into (drives form grouping).
INPUT_ROLES: frozenset[str] = frozenset(
    {"textbox", "searchbox", "combobox", "textarea", "input", "spinbutton"}
)

# Heading roles used for heading-based context injection.
HEADING_ROLES: frozenset[str] = frozenset({"heading", "h1", "h2", "h3", "h4", "h5", "h6"})


@dataclass(frozen=True, slots=True)
class A11yNode:
    """One node from a parsed accessibility snapshot.

    ``dom_order`` is the node's sequence index in the snapshot (used for
    stable sort + position scoring). ``depth`` is its nesting depth in the
    tree (used for heading-context attachment). ``bbox`` is ``(x, y, w, h)``
    when the server reports geometry, else ``None``.
    """

    ref: str
    role: str
    name: str = ""
    value: str = ""
    depth: int = 0
    dom_order: int = 0
    visible: bool = True
    disabled: bool = False
    level: int = 0  # heading level when role is a heading, else 0
    bbox: tuple[int, int, int, int] | None = None
    attrs: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_interactive(self) -> bool:
        return self.role in INTERACTIVE_ROLES

    @property
    def is_input(self) -> bool:
        return self.role in INPUT_ROLES

    @property
    def is_heading(self) -> bool:
        return self.role in HEADING_ROLES

    @property
    def is_submit(self) -> bool:
        """Heuristic: a button whose name reads like a form submission."""
        if self.role != "button":
            return False
        name = self.name.strip().lower()
        return any(
            kw in name
            for kw in (
                "submit",
                "search",
                "log in",
                "login",
                "sign in",
                "sign up",
                "continue",
                "next",
                "save",
                "send",
                "place order",
                "checkout",
                "apply",
                "confirm",
            )
        )

    def dedup_key(self) -> tuple[str, str]:
        """Identity for near-duplicate collapsing (role + normalized name)."""
        return (self.role, " ".join(self.name.lower().split()))


__all__ = [
    "A11yNode",
    "INTERACTIVE_ROLES",
    "INPUT_ROLES",
    "HEADING_ROLES",
]
