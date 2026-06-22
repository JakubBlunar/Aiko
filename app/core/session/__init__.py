"""Session controller mixins.

The :class:`app.core.session.session_controller.SessionController` class became
unwieldy (originally ~6300 lines, ~160 methods) and started causing
editor / IDE heartburn on big edits. To keep individual files small
and readable without changing any public API, cohesive groups of
methods are pulled out into mixin classes that ``SessionController``
inherits from.

Each mixin is *not* a standalone class — it only makes sense in the
context of ``SessionController`` because every method reads or writes
``self.*`` attributes set up in ``SessionController.__init__``. The
mixins exist purely as physical-file boundaries, not logical
encapsulation. Do not instantiate them directly; do not move state
ownership into them.

Public import surface is ``from app.core.session.session_controller import
SessionController`` (was ``app.core.session_controller`` before the
``app/core/`` folder reorg). Tests that patch
``app.core.session.session_controller.<symbol>`` keep working because the
module-level imports stay in the shell. Tests that patch a symbol *used
by a method that has since moved* must patch the mixin module instead —
the patch must always target the module where the symbol is *looked
up*. See each mixin's docstring for the exact replacement path.
"""
from __future__ import annotations

from app.core.session.avatar_mixin import AvatarMixin
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.session.persona_regression_mixin import PersonaRegressionMixin
from app.core.session.post_turn_mixin import PostTurnMixin
from app.core.session.search_provider_mixin import SearchProviderMixin
from app.core.session.speaking_window_jobs_mixin import SpeakingWindowJobsMixin
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from app.core.session.world_mixin import WorldMixin

__all__ = [
    "AvatarMixin",
    "InnerLifeProvidersMixin",
    "MemoryFacadeMixin",
    "PersonaRegressionMixin",
    "PostTurnMixin",
    "SearchProviderMixin",
    "SpeakingWindowJobsMixin",
    "TaskOrchestrationMixin",
    "WorldMixin",
]
