"""Check the personality-backlog docs for broken internal links.

The backlog is a web of cross-references between open entries, their shipped
write-ups, and the code they touch, and every migration of a shipped entry out
of an open file is an opportunity to leave a link pointing at nothing. Anchors
are the failure that actually happens: a heading gets reworded on the way into
``shipped/`` and the ``#l39-...`` fragment silently stops resolving, which no
Markdown renderer complains about.

Usage::

    python scripts/check_backlog_links.py            # backlog docs
    python scripts/check_backlog_links.py docs rules # any roots

Exits non-zero when anything is unresolved.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = ("docs/personality-backlog",)
#: Vendored markdown is not ours to fix and drowns the signal.
SKIP_DIRS = frozenset(
    {".git", ".venv", "node_modules", "site-packages", "target", "dist", "build"}
)

#: ``[text](target)`` where the target is not a URL. Nested brackets in the
#: text (``[`foo.py`](...)``) are fine; nested parens in the target are not,
#: and do not occur.
_LINK = re.compile(r"\[(?:[^\]]|\](?=[^(]))*\]\((?!https?:|mailto:)([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_NOT_SLUG = re.compile(r"[^a-z0-9 \-_]")
#: An explicit ``<a id="...">`` is how a section that outlives its own wording
#: gets a stable anchor; see health.md's recurring-shapes list.
_EXPLICIT_ANCHOR = re.compile(r"""<a\s+(?:id|name)=["']([^"']+)["']""")


def slugify(heading: str) -> str:
    """Reproduce GitHub's heading-anchor algorithm closely enough.

    Punctuation is dropped rather than folded — GitHub does not normalise, so
    a superscript in a heading vanishes from the anchor instead of becoming a
    digit, and headings that rely on that are worth rewording.
    """
    text = _INLINE_CODE.sub(r"\1", heading).lower()
    text = _NOT_SLUG.sub("", text)
    return text.strip().replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEADING.match(line)
        if m:
            found.add(slugify(m.group(2).strip()))
        found.update(a.lower() for a in _EXPLICIT_ANCHOR.findall(line))
    return found


def main(argv: list[str]) -> int:
    roots = [REPO / r for r in (argv or DEFAULT_ROOTS)]
    files = sorted(
        {
            p
            for root in roots
            for p in root.rglob("*.md")
            if SKIP_DIRS.isdisjoint(p.parts)
        }
    )
    if not files:
        print("no markdown found under: " + ", ".join(str(r) for r in roots))
        return 1

    anchor_cache: dict[Path, set[str]] = {}
    problems: list[str] = []

    for path in files:
        text = path.read_text(encoding="utf-8")
        own = anchors_of(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for target in _LINK.findall(line):
                frag = ""
                if "#" in target:
                    target, frag = target.split("#", 1)

                if target:
                    dest = (path.parent / target).resolve()
                    if not dest.exists():
                        problems.append(
                            f"{path.relative_to(REPO)}:{lineno}: "
                            f"missing path {target}"
                        )
                        continue
                else:
                    dest = path

                if not frag or dest.suffix != ".md":
                    continue
                if dest == path:
                    have = own
                else:
                    have = anchor_cache.setdefault(dest, anchors_of(dest))
                if frag.lower() not in have:
                    problems.append(
                        f"{path.relative_to(REPO)}:{lineno}: "
                        f"missing anchor #{frag} in "
                        f"{dest.relative_to(REPO).as_posix()}"
                    )

    for p in problems:
        print(p)
    print(f"\n{len(files)} files checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
