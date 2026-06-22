"""One-shot splitter for inner_life_providers_mixin.py (Phase 2c).

InnerLifeProvidersMixin is one class of ~75 pure self-reading _render_* methods.
We move shared module-level constants/helpers into inner_life_shared.py (leaf),
split the methods into 4 composed sub-mixins, and rebuild
InnerLifeProvidersMixin as their composition (MRO order preserves the two
intentional duplicate-method pairs, which both live in the final group).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path("app/core/session/inner_life_providers_mixin.py")
lines = SRC.read_text(encoding="utf-8").splitlines()
orig_doc = "\n".join(lines[0:22])  # original module docstring (lines 1-22)

# method-def cut points (1-indexed). Final group keeps 3735..EOF so both
# duplicate method pairs (4362/4583, 4435/4622) stay in one class body.
PARTS = [
    ("InnerLifePart1Mixin", "inner_life_part1", 118, 989),
    ("InnerLifePart2Mixin", "inner_life_part2", 990, 2289),
    ("InnerLifePart3Mixin", "inner_life_part3", 2290, 3734),
    ("InnerLifePart4Mixin", "inner_life_part4", 3735, 4812),
]
for _k, _m, a, _b in PARTS:
    assert lines[a - 1].lstrip().startswith("def "), (a, lines[a - 1])

SHARED_NAMES = [
    "_circadian",
    "_MILESTONE_PHRASES",
    "_APPRECIATION_VIBES",
    "_KV_APPRECIATION_AT",
    "_KV_APPRECIATION_ANCHOR",
    "_KV_RECIP_VULN_AT",
    "_format_running_task_line",
]

# --- leaf: inner_life_shared.py (constants + helper, lines 34-112) ---------
shared_body = lines[33:112]
shared_mod = [
    '"""Shared constants + pure helpers for the inner-life provider mixins."""',
    "from __future__ import annotations",
    "",
    "import logging",
    "from typing import Any",
    "",
    "from app.core.affect import circadian as _circadian  # noqa: F401  (re-exported)",
    "",
    'log = logging.getLogger("app.session")',
    "",
    "",
    *shared_body,
    "",
]


def build_part(klass: str, num: int, body: list[str]) -> str:
    text = "\n".join(body)
    need_any = re.search(r"\bAny\b", text) is not None
    need_log = re.search(r"\blog\b", text) is not None
    used_shared = [n for n in SHARED_NAMES if re.search(r"\b" + re.escape(n) + r"\b", text)]
    hdr = ["from __future__ import annotations", ""]
    if need_log:
        hdr.append("import logging")
    if need_any:
        hdr.append("from typing import Any")
    if used_shared:
        hdr.append("from app.core.session.inner_life_shared import (")
        hdr += [f"    {n}," for n in used_shared]
        hdr.append(")")
    hdr.append("")
    if need_log:
        hdr += ["", 'log = logging.getLogger("app.session")']
    hdr += [
        "",
        "",
        f"class {klass}:",
        f'    """Inner-life prompt-block providers (part {num} of 4)."""',
        "",
    ]
    out = "\n".join(hdr + body + [""]) + "\n"
    ast.parse(out)
    return out


# --- rebuilt inner_life_providers_mixin.py --------------------------------
new_main = [
    orig_doc,
    "from __future__ import annotations",
    "",
    "from app.core.session.inner_life_shared import (  # noqa: F401  (re-exported)",
    "    _APPRECIATION_VIBES,",
    "    _KV_APPRECIATION_ANCHOR,",
    "    _KV_APPRECIATION_AT,",
    "    _KV_RECIP_VULN_AT,",
    "    _MILESTONE_PHRASES,",
    "    _circadian,",
    "    _format_running_task_line,",
    ")",
    "from app.core.session.inner_life_part1 import InnerLifePart1Mixin",
    "from app.core.session.inner_life_part2 import InnerLifePart2Mixin",
    "from app.core.session.inner_life_part3 import InnerLifePart3Mixin",
    "from app.core.session.inner_life_part4 import InnerLifePart4Mixin",
    "",
    "",
    "class InnerLifeProvidersMixin(",
    "    InnerLifePart1Mixin,",
    "    InnerLifePart2Mixin,",
    "    InnerLifePart3Mixin,",
    "    InnerLifePart4Mixin,",
    "):",
    '    """Per-turn prompt-block providers, grounding builder, avatar accessors."""',
    "",
]
# orig_doc already opens with `"""` on line 1; we close it with the second
# line above. Strip the original closing `"""` that lived on line 22.
assert lines[21].strip() == '"""', lines[21]

main_text = "\n".join(new_main) + "\n"
ast.parse(main_text)

# write everything
Path("app/core/session/inner_life_shared.py").write_text(
    "\n".join(shared_mod) + "\n", encoding="utf-8"
)
ast.parse("\n".join(shared_mod) + "\n")
for klass, mod, a, b in PARTS:
    body = lines[a - 1 : b]
    Path(f"app/core/session/{mod}.py").write_text(
        build_part(klass, PARTS.index((klass, mod, a, b)) + 1, body), encoding="utf-8"
    )
    print(f"{mod}.py", b - a + 1, "method-lines")
SRC.write_text(main_text, encoding="utf-8")
print("inner_life_shared.py", len(shared_mod), "lines")
print("inner_life_providers_mixin.py", len(new_main), "lines (was", len(lines), ")")
