"""One text-filter predicate for the debug list views.

Both the Concepts and Memories panels want the same thing -- *"did she
record anything about X?"* -- over different rows, and the two would
drift apart within a release if each grew its own matcher (the
predicate-copied-into-N-call-sites shape). So the rule lives here once
and both callers pass their own haystacks in.

The rule, in the order a user discovers it:

* **Case-insensitive.** Nobody typing into a search box means case.
* **Whitespace splits terms, and every term must match** -- somewhere,
  in any order. ``bottle cap`` finds *"the cap of a bottle"*, which a
  literal substring search would miss. This is the choice that makes
  the box useful for recall rather than only for verification: you are
  searching for a thing you half-remember, so word order is exactly
  what you are least sure of.
* **``*`` and ``?`` turn a term into a glob**, matched anywhere in the
  text rather than anchored, so ``collect*`` finds *collecting* and
  *collection*. A term with no wildcard is a plain substring, which is
  already an "anywhere" match -- so the two behave consistently and the
  wildcard only adds reach *inside* a word.

Deliberately not supported: quoted phrases. Term-AND already subsumes
the common case more forgivingly than a phrase would, and quote parsing
has an unbalanced-quote failure mode that reads as "search is broken".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

# A term is a glob only if it carries one of these. ``[`` is deliberately
# excluded: bracket expressions are not a thing anyone types into a
# search box on purpose, and treating them as globs makes a stray
# bracket silently match nothing.
_WILDCARD_RE = re.compile(r"[*?]")


@dataclass(frozen=True, slots=True)
class TextQuery:
    """A compiled search box. Use :func:`compile_query` to build one."""

    raw: str
    plain: tuple[str, ...]
    globs: tuple[str, ...]

    def matches(self, *texts: str | None) -> bool:
        """True when every term appears in at least one of ``texts``.

        Terms are checked against the concatenation, not per-field, so a
        two-word query still matches when one word is in a concept's
        label and the other is in its rationale. That is what a user
        means by "search this row".
        """
        hay = " ".join(t for t in texts if t).lower()
        if not hay:
            return False
        for term in self.plain:
            if term not in hay:
                return False
        for pattern in self.globs:
            if not fnmatchcase(hay, pattern):
                return False
        return True


def compile_query(raw: str | None) -> "TextQuery | None":
    """Compile a search string, or ``None`` when there is nothing to do.

    Returning ``None`` for blank input is the load-bearing part: callers
    use it to skip the filter entirely, so an empty box costs nothing and
    cannot accidentally match zero rows.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    plain: list[str] = []
    globs: list[str] = []
    for term in text.split():
        if not term:
            continue
        if _WILDCARD_RE.search(term):
            # Unanchored, so a wildcard term reaches the same rows a
            # plain substring would plus the ones the wildcard adds.
            # Doubling an existing leading/trailing ``*`` is harmless.
            globs.append(f"*{term}*")
        else:
            plain.append(term)
    if not plain and not globs:
        return None
    return TextQuery(raw=text, plain=tuple(plain), globs=tuple(globs))


__all__ = ["TextQuery", "compile_query"]
