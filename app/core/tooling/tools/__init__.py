from app.core.conversation_memory import ConversationMemoryStore
from app.core.settings import AppSettings
from app.core.tooling.config_loader import ToolingConfig
from app.core.tooling.contracts import Tool
from app.core.tooling.tools.history_tools import (
    HistoryCompactSummaryTool,
    HistoryReadEntriesTool,
    HistoryReadMessagesTool,
    HistoryReadSummaryTool,
    HistoryRuntime,
)
def build_default_tools(
    settings: AppSettings,
    tooling_config: ToolingConfig | None = None,
    memory_store: ConversationMemoryStore | None = None,
) -> list[Tool]:
    config = tooling_config or ToolingConfig()
    history_cfg = config.tool_settings("history")

    history_runtime = HistoryRuntime(
        memory_store or ConversationMemoryStore(),
        default_limit=int(history_cfg.get("default_limit", 50)),
        max_limit=int(history_cfg.get("max_limit", 400)),
    )
    return [
        HistoryReadMessagesTool(history_runtime),
        HistoryReadEntriesTool(history_runtime),
        HistoryReadSummaryTool(history_runtime),
        HistoryCompactSummaryTool(history_runtime),
    ]


__all__ = [
    "build_default_tools",
]
