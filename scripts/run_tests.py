#!/usr/bin/env python
"""Run explicit pytest targets with automatic temporary-data cleanup.

Usage: python scripts/run_tests.py tests/test_example.py -q
Scratch lives under the project's .tmp-tests/ directory. The parent removes
each run's scratch after pytest exits, even when tests fail.
Forced process termination or power loss can leave the printed run directory.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.affected_tests import _run_tests


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return _run_tests([sys.executable, "-m", "pytest", *args])


if __name__ == "__main__":
    raise SystemExit(main())
