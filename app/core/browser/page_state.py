"""In-process memory of previous page state, for change diffs.

Ephemeral (per running session), bounded LRU. Element identity for the
diff is ``(role, normalized-name)`` rather than the server's ``ref`` —
refs are frequently regenerated between snapshots, names are stable.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from app.core.browser.accessibility import A11yNode


@dataclass(frozen=True, slots=True)
class PageDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def _label(node: A11yNode) -> str:
    name = " ".join(node.name.split()) or "(unnamed)"
    return f"{node.role} \"{name}\""


def _identity(node: A11yNode) -> tuple[str, str]:
    return node.dedup_key()


class PageStateMemory:
    """Bounded LRU of the last interactive-element fingerprint per page."""

    def __init__(self, max_pages: int = 8) -> None:
        self._max = max(1, int(max_pages))
        # page_key -> { identity -> (label, value) }
        self._pages: "OrderedDict[str, dict[tuple[str, str], tuple[str, str]]]" = (
            OrderedDict()
        )

    def __len__(self) -> int:
        return len(self._pages)

    @staticmethod
    def _fingerprint(
        nodes: list[A11yNode],
    ) -> dict[tuple[str, str], tuple[str, str]]:
        fp: dict[tuple[str, str], tuple[str, str]] = {}
        for node in nodes:
            if node.is_interactive:
                fp[_identity(node)] = (_label(node), node.value)
        return fp

    def update_and_diff(
        self, page_key: str, nodes: list[A11yNode]
    ) -> PageDiff | None:
        """Diff ``nodes`` against the stored state for ``page_key``, then
        store the new fingerprint. Returns ``None`` on first visit."""
        key = page_key or "active"
        new_fp = self._fingerprint(nodes)
        prior = self._pages.get(key)
        diff: PageDiff | None
        if prior is None:
            diff = None
        else:
            added = [lbl for ident, (lbl, _v) in new_fp.items() if ident not in prior]
            removed = [
                lbl for ident, (lbl, _v) in prior.items() if ident not in new_fp
            ]
            changed = [
                new_fp[ident][0]
                for ident in new_fp
                if ident in prior and new_fp[ident][1] != prior[ident][1]
            ]
            diff = PageDiff(
                added=tuple(added[:20]),
                removed=tuple(removed[:20]),
                changed=tuple(changed[:20]),
            )
        self._pages[key] = new_fp
        self._pages.move_to_end(key)
        while len(self._pages) > self._max:
            self._pages.popitem(last=False)
        return diff

    def clear(self) -> None:
        self._pages.clear()


__all__ = ["PageDiff", "PageStateMemory"]
