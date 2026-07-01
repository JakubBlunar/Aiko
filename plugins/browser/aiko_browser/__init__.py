"""aiko_browser -- browser accessibility perception for the browser plugin.

Self-contained snapshot-reshaping pipeline (parse -> dedup -> group -> rank
-> diff -> render) that used to live in ``app.core.browser``. It now ships
inside the browser plugin so the perception code is decoupled from app core;
the plugin root is put on ``sys.path`` by the plugin runtime, which makes
``import aiko_browser`` resolve from ``entry.py``.
"""
from __future__ import annotations

from .accessibility import A11yNode
from .perception import BrowserPerception, PerceptionResult


__all__ = ["A11yNode", "BrowserPerception", "PerceptionResult"]
