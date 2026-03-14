"""Screen tools: capture + OCR for the agent."""

from __future__ import annotations

from collections.abc import Callable

from app.core.tooling.tools.ocr_tools import OcrRuntime
from app.core.tooling.tools.screen_capture import ScreenCaptureService
from app.core.tooling.types import ToolContext, ToolError, ToolResult, ToolSpec


class ScreenReadTool:
    """Capture current screen and run OCR; return text and elements. Agent uses this to 'read the screen'."""

    def __init__(
        self,
        capture: ScreenCaptureService,
        ocr_runtime: OcrRuntime,
        trace: Callable[[str, str], None] | None = None,
    ) -> None:
        self._capture = capture
        self._ocr = ocr_runtime
        self._trace = trace or (lambda _s, _m: None)
        self.spec = ToolSpec(
            name="screen.read",
            description="Capture the current screen and extract visible text and UI elements (OCR). Use when the user asks what is on screen, to read content, or to find clickable elements.",
            is_mutating=False,
            input_schema={},
            output_schema={"text": "str", "elements": "list[dict]", "foreground_window": "str"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))
        frame, region = self._capture.capture_once_with_region()
        if frame is None:
            self._trace("screen.read", "Capture failed.")
            return ToolResult(
                success=False,
                error=ToolError(code="capture_failed", message="Screen capture unavailable."),
            )
        left = int((region or {}).get("left", 0))
        top = int((region or {}).get("top", 0))
        width = int((region or {}).get("width", 0))
        height = int((region or {}).get("height", 0))
        elements = self._ocr.extract_elements(
            frame,
            screen_left=left,
            screen_top=top,
            screen_width=width,
            screen_height=height,
        )
        text = " ".join(e.get("text", "") for e in elements if e.get("text")).strip()
        text = " ".join(text.split())
        return ToolResult(
            success=True,
            data={
                "text": text or "",
                "elements": elements,
                "foreground_window": "",
            },
        )
