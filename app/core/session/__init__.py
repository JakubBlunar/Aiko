"""Session controller mixins.

The :class:`app.core.session_controller.SessionController` class became
unwieldy (~5600 lines, ~160 methods) and started causing editor / IDE
heartburn on big edits. To keep individual files small and readable
without changing any public API, cohesive groups of methods are pulled
out into mixin classes that ``SessionController`` inherits from.

Each mixin is *not* a standalone class — it only makes sense in the
context of ``SessionController`` because every method reads or writes
``self.*`` attributes set up in ``SessionController.__init__``. The
mixins exist purely as physical-file boundaries, not logical
encapsulation. Do not instantiate them directly; do not move state
ownership into them.

Public import surface (``from app.core.session_controller import …``)
is unchanged. Tests that patch ``app.core.session_controller.<symbol>``
keep working because the module-level imports stay in the shell.
"""
from __future__ import annotations

from app.core.session.avatar_mixin import AvatarMixin
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.session.world_mixin import WorldMixin

__all__ = ["AvatarMixin", "MemoryFacadeMixin", "WorldMixin"]
