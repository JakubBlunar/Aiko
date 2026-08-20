"""Structural checks on the voice studio's single-page front end.

Worth having because the failure mode is silent. The page is one inline
script served as a Python string, so a typo in it does not raise
anywhere: the markup renders, the layout looks right, and every button
does nothing. Likewise a ``$('foo')`` whose element was renamed returns
null and the handler dies on first click. Neither shows up in a linter
run over Python, and neither shows up in an import.

These are cheap invariants rather than behaviour tests -- driving the
real UI would need a browser and the engines it talks to. The point is
to catch the class of mistake that editing a 450-line string literal
actually produces.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tools.tts_lab.page import INDEX_HTML

SCRIPT = re.search(r"<script>(.*)</script>", INDEX_HTML, re.S)


def test_page_has_one_inline_script() -> None:
    assert SCRIPT is not None
    assert INDEX_HTML.count("<script>") == 1


@pytest.mark.parametrize(
    "tag", ["div", "table", "select", "textarea", "style", "script"]
)
def test_tags_balance(tag: str) -> None:
    opens = len(re.findall(rf"<{tag}[\s>]", INDEX_HTML))
    closes = len(re.findall(rf"</{tag}>", INDEX_HTML))
    assert opens == closes, f"<{tag}> is unbalanced"


def test_every_lookup_has_an_element() -> None:
    """``$('x')`` returning null is the most common way this page breaks."""
    assert SCRIPT is not None
    ids = set(re.findall(r"\bid=\"([\w-]+)\"", INDEX_HTML))
    used = set(re.findall(r"\$\('([\w-]+)'\)", SCRIPT.group(1)))
    assert not (used - ids), f"no such element(s): {sorted(used - ids)}"


def test_script_parses() -> None:
    """A syntax error here disables the whole page without a trace."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    assert SCRIPT is not None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.mjs"
        # Wrapped so top-level await parses; --check never executes it,
        # so the missing DOM is irrelevant.
        path.write_text(
            "async function _wrapped() {\n" + SCRIPT.group(1) + "\n}\n",
            encoding="utf-8",
        )
        done = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True
        )
    assert done.returncode == 0, done.stderr[:2000]
