from pathlib import Path

from app.core.conversation_memory import ConversationMemoryStore
from app.core.settings import AppSettings
from app.core.tooling.config_loader import ToolingConfig
from app.core.tooling.contracts import Tool
from app.core.tooling.tools.action_tools import ActionExecutePlanTool
from app.core.tooling.tools.history_tools import (
    HistoryCompactSummaryTool,
    HistoryReadEntriesTool,
    HistoryReadMessagesTool,
    HistoryReadSummaryTool,
    HistoryRuntime,
)
from app.core.tooling.tools.ocr_tools import OcrExtractDetailsTool, OcrExtractElementsTool, OcrRuntime
from app.core.tooling.tools.persona_tools import (
    PersonaCompactNotesTool,
    PersonaFilterNotesTool,
    PersonaProfileRuntime,
    PersonaReadSnapshotTool,
    PersonaUpdateFromTextTool,
)
from app.core.tooling.tools.uia_tools import (
    UiaFocusWindowTool,
    UiaForegroundElementsTool,
    UiaListAllWindowsTool,
    UiaListVisibleWindowsTool,
    UiaRuntime,
)


def build_default_tools(
    settings: AppSettings,
    tooling_config: ToolingConfig | None = None,
    memory_store: ConversationMemoryStore | None = None,
) -> list[Tool]:
    config = tooling_config or ToolingConfig()
    persona_cfg = config.tool_settings("persona")
    history_cfg = config.tool_settings("history")

    persona_path_raw = str(persona_cfg.get("profile_path", "")).strip()
    persona_path: Path | None = None
    if persona_path_raw:
        candidate = Path(persona_path_raw)
        if not candidate.is_absolute():
            workspace_root = Path(__file__).resolve().parents[4]
            candidate = workspace_root / candidate
        persona_path = candidate

    ocr_runtime = OcrRuntime(settings.screen)
    uia_runtime = UiaRuntime()
    persona_runtime = PersonaProfileRuntime(
        path=persona_path,
        assistant_background=settings.assistant.background,
    )
    history_runtime = HistoryRuntime(
        memory_store or ConversationMemoryStore(),
        default_limit=int(history_cfg.get("default_limit", 50)),
        max_limit=int(history_cfg.get("max_limit", 400)),
    )
    return [
        OcrExtractElementsTool(ocr_runtime),
        OcrExtractDetailsTool(ocr_runtime),
        UiaForegroundElementsTool(uia_runtime),
        UiaListVisibleWindowsTool(uia_runtime),
        UiaListAllWindowsTool(uia_runtime),
        UiaFocusWindowTool(uia_runtime),
        HistoryReadMessagesTool(history_runtime),
        HistoryReadEntriesTool(history_runtime),
        HistoryReadSummaryTool(history_runtime),
        HistoryCompactSummaryTool(history_runtime),
        PersonaUpdateFromTextTool(persona_runtime),
        PersonaCompactNotesTool(persona_runtime),
        PersonaFilterNotesTool(persona_runtime),
        PersonaReadSnapshotTool(persona_runtime),
    ]


__all__ = [
    "ActionExecutePlanTool",
    "build_default_tools",
]
