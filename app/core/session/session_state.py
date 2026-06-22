"""Small session value types.

Extracted from :mod:`app.core.session.session_controller` into a leaf
module so mixins (e.g. ``lifecycle_mixin``) can reference ``SessionState``
without importing the controller (which would be circular). Leaf-only
dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SessionState:
    mic_enabled: bool
    session_type: str
