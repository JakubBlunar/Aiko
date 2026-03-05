from pathlib import Path

from app.core.settings import AppSettings
from app.core.tooling.config_loader import ToolingConfig
from app.core.tooling.contracts import Tool
from app.core.tooling.tools.action_tools import ActionExecutePlanTool
from app.core.tooling.tools.ocr_tools import OcrExtractDetailsTool, OcrExtractElementsTool, OcrRuntime
from app.core.tooling.tools.persona_tools import PersonaProfileRuntime, PersonaReadSnapshotTool, PersonaUpdateFromTextTool
from app.core.tooling.tools.uia_tools import (
    UiaFocusWindowTool,
    UiaForegroundElementsTool,
    UiaListAllWindowsTool,
    UiaListVisibleWindowsTool,
    UiaRuntime,
)


def build_default_tools(settings: AppSettings, tooling_config: ToolingConfig | None = None) -> list[Tool]:
    config = tooling_config or ToolingConfig()
    persona_cfg = config.tool_settings("persona")
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
    return [
        OcrExtractElementsTool(ocr_runtime),
        OcrExtractDetailsTool(ocr_runtime),
        UiaForegroundElementsTool(uia_runtime),
        UiaListVisibleWindowsTool(uia_runtime),
        UiaListAllWindowsTool(uia_runtime),
        UiaFocusWindowTool(uia_runtime),
        PersonaUpdateFromTextTool(persona_runtime),
        PersonaReadSnapshotTool(persona_runtime),
    ]


__all__ = [
    "ActionExecutePlanTool",
    "build_default_tools",
]
