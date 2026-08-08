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

**The re-exports below are resolved lazily**, and that is load-bearing
rather than a micro-optimisation. Importing *any* module in this package
runs this file first, so when the re-exports were eager, a leaf utility
like ``session_text_utils`` — which depends on nothing in here — dragged
in the entire controller. That closed a cycle: ``app.core.voice.tts_queue``
imports ``session_text_utils``, this file imported ``voice_mixin``, and
``voice_mixin`` imports ``TtsQueue`` straight back out of a module still
part-way through line 19. It only failed when ``tts_queue`` happened to be
imported first, which is why the app was fine and one test file was not.
Resolving on attribute access keeps ``from app.core.session import Mixin``
working while importing a sibling module costs nothing.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

# Exported name -> the submodule that defines it.
_LAZY_EXPORTS: dict[str, str] = {
    "AvatarMixin": "avatar_mixin",
    "ChatTurnMixin": "chat_turn_mixin",
    "CuePoolMixin": "cue_pool_mixin",
    "DetectorsInitMixin": "detectors_init_mixin",
    "HypothesisDebugMixin": "hypothesis_debug_mixin",
    "IdleWorkersInitMixin": "idle_workers_init_mixin",
    "InnerLifeProvidersMixin": "inner_life_providers_mixin",
    "LifecycleMixin": "lifecycle_mixin",
    "ListenersMetricsMixin": "listeners_metrics_mixin",
    "LlmClientsMixin": "llm_clients_mixin",
    "LlmSettingsMixin": "llm_settings_mixin",
    "MemoryFacadeMixin": "memory_facade_mixin",
    "PersonaRegressionMixin": "persona_regression_mixin",
    "PostTurnMixin": "post_turn_mixin",
    "ProactivePresenceMixin": "proactive_presence_mixin",
    "SearchProviderMixin": "search_provider_mixin",
    "WeatherMixin": "weather_mixin",
    "SpeakingWindowJobsMixin": "speaking_window_jobs_mixin",
    "SpeakingWorkersInitMixin": "speaking_workers_init_mixin",
    "TaskOrchestrationMixin": "task_orchestration_mixin",
    "ToolsRegistryMixin": "tools_registry_mixin",
    "VoiceCaptureMixin": "voice_capture_mixin",
    "KNOWN_OVERRIDES": "debug_overrides",
    "DebugOverrides": "debug_overrides",
    "UnknownOverride": "debug_overrides",
    "VoiceMixin": "voice_mixin",
    "TaskHandles": "web_facade_mixin",
    "WebFacadeMixin": "web_facade_mixin",
    "WorkerUnavailable": "web_facade_mixin",
    "WorldMixin": "world_mixin",
}


def __getattr__(name: str) -> Any:
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # resolve once, then it's a normal global
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # import-time cost only for type checkers
    from app.core.session.avatar_mixin import AvatarMixin
    from app.core.session.chat_turn_mixin import ChatTurnMixin
    from app.core.session.cue_pool_mixin import CuePoolMixin
    from app.core.session.debug_overrides import (
        KNOWN_OVERRIDES,
        DebugOverrides,
        UnknownOverride,
    )
    from app.core.session.detectors_init_mixin import DetectorsInitMixin
    from app.core.session.hypothesis_debug_mixin import HypothesisDebugMixin
    from app.core.session.idle_workers_init_mixin import IdleWorkersInitMixin
    from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
    from app.core.session.lifecycle_mixin import LifecycleMixin
    from app.core.session.listeners_metrics_mixin import ListenersMetricsMixin
    from app.core.session.llm_clients_mixin import LlmClientsMixin
    from app.core.session.llm_settings_mixin import LlmSettingsMixin
    from app.core.session.memory_facade_mixin import MemoryFacadeMixin
    from app.core.session.persona_regression_mixin import PersonaRegressionMixin
    from app.core.session.post_turn_mixin import PostTurnMixin
    from app.core.session.proactive_presence_mixin import ProactivePresenceMixin
    from app.core.session.search_provider_mixin import SearchProviderMixin
    from app.core.session.speaking_window_jobs_mixin import SpeakingWindowJobsMixin
    from app.core.session.speaking_workers_init_mixin import SpeakingWorkersInitMixin
    from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
    from app.core.session.tools_registry_mixin import ToolsRegistryMixin
    from app.core.session.voice_capture_mixin import VoiceCaptureMixin
    from app.core.session.voice_mixin import VoiceMixin
    from app.core.session.weather_mixin import WeatherMixin
    from app.core.session.web_facade_mixin import (
        TaskHandles,
        WebFacadeMixin,
        WorkerUnavailable,
    )
    from app.core.session.world_mixin import WorldMixin

__all__ = [
    "KNOWN_OVERRIDES",
    "AvatarMixin",
    "ChatTurnMixin",
    "CuePoolMixin",
    "DebugOverrides",
    "DetectorsInitMixin",
    "HypothesisDebugMixin",
    "IdleWorkersInitMixin",
    "InnerLifeProvidersMixin",
    "LifecycleMixin",
    "ListenersMetricsMixin",
    "LlmClientsMixin",
    "LlmSettingsMixin",
    "MemoryFacadeMixin",
    "PersonaRegressionMixin",
    "PostTurnMixin",
    "ProactivePresenceMixin",
    "SearchProviderMixin",
    "WeatherMixin",
    "SpeakingWindowJobsMixin",
    "SpeakingWorkersInitMixin",
    "TaskHandles",
    "TaskOrchestrationMixin",
    "ToolsRegistryMixin",
    "UnknownOverride",
    "VoiceCaptureMixin",
    "VoiceMixin",
    "WebFacadeMixin",
    "WorkerUnavailable",
    "WorldMixin",
]
