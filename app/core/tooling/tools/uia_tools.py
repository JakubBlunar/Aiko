from __future__ import annotations

from collections.abc import Callable
import ctypes
import ctypes.wintypes as wintypes
import time

from app.core.tooling.types import ToolContext, ToolError, ToolResult, ToolSpec

_user32 = ctypes.windll.user32  # type: ignore[attr-defined]

_GWL_STYLE = -16
_WS_DISABLED = 0x08000000
_SW_RESTORE = 9

_CLASS_TYPE: dict[str, str] = {
    "Button": "Button",
    "Edit": "TextInput",
    "ComboBox": "Dropdown",
    "Static": "Label",
    "ListBox": "ListBox",
    "ScrollBar": "Scrollbar",
    "ToolbarWindow32": "Toolbar",
    "ReBarWindow32": "Toolbar",
    "SysTreeView32": "TreeView",
    "SysListView32": "ListView",
    "SysTabControl32": "TabControl",
    "msctls_progress32": "ProgressBar",
    "msctls_statusbar32": "StatusBar",
    "msctls_trackbar32": "Slider",
    "SysDateTimePick32": "DatePicker",
    "#32770": "Dialog",
    "RICHEDIT": "TextInput",
    "RichEdit20W": "TextInput",
    "RichEdit50W": "TextInput",
    "RICHEDIT60W": "TextInput",
    "QWidget": "Widget",
    "Qt5QWindowIcon": "Widget",
    "Qt6QWindowIcon": "Widget",
}

_INTERACTIVE_TYPES = {
    "Button",
    "TextInput",
    "Dropdown",
    "ListBox",
    "TreeView",
    "ListView",
    "TabControl",
    "Slider",
    "DatePicker",
}

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _get_text(hwnd: int, max_len: int = 512) -> str:
    buf = ctypes.create_unicode_buffer(max_len)
    _user32.GetWindowTextW(hwnd, buf, max_len)
    return buf.value.strip()


def _get_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value.strip()


def _get_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        if rect.right > rect.left and rect.bottom > rect.top:
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    return None


class UiaRuntime:
    def list_visible_windows(self) -> list[dict]:
        results: list[dict] = []
        foreground = int(_user32.GetForegroundWindow())

        def _cb(hwnd: int, _: int) -> bool:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _get_text(hwnd)
            if not title:
                return True
            rect = _get_rect(hwnd)
            if rect is None:
                return True
            left, top, right, bottom = rect
            if (right - left) < 50 or (bottom - top) < 20:
                return True
            if left <= -30000 or top <= -30000:
                return True
            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "left": left,
                    "top": top,
                    "w": right - left,
                    "h": bottom - top,
                    "is_foreground": int(hwnd) == foreground,
                }
            )
            return True

        _user32.EnumWindows(_EnumProc(_cb), 0)
        return results

    def list_all_windows(self) -> list[dict]:
        results: list[dict] = []
        foreground = int(_user32.GetForegroundWindow())

        def _cb(hwnd: int, _: int) -> bool:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _get_text(hwnd)
            if not title:
                return True
            rect = _get_rect(hwnd)
            if rect is None:
                return True
            left, top, right, bottom = rect
            if (right - left) < 50 or (bottom - top) < 20:
                return True
            is_minimized = left <= -30000 or top <= -30000
            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "left": left,
                    "top": top,
                    "w": right - left,
                    "h": bottom - top,
                    "is_foreground": int(hwnd) == foreground,
                    "is_minimized": is_minimized,
                }
            )
            return True

        _user32.EnumWindows(_EnumProc(_cb), 0)
        return results

    def focus_window(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.3)
        return bool(_user32.SetForegroundWindow(hwnd))

    def get_foreground_elements(self) -> tuple[str, list[dict]]:
        hwnd = int(_user32.GetForegroundWindow())
        if not hwnd:
            return "", []
        title = _get_text(hwnd)
        return title, self._get_child_elements(hwnd)

    def _get_child_elements(self, parent: int) -> list[dict]:
        results: list[dict] = []

        def _cb(hwnd: int, _: int) -> bool:
            if not _user32.IsWindowVisible(hwnd):
                return True
            rect = _get_rect(hwnd)
            if rect is None:
                return True
            left, top, right, bottom = rect
            width = right - left
            height = bottom - top
            if width < 8 or height < 8:
                return True

            cls = _get_class(hwnd)
            ctrl_type = _CLASS_TYPE.get(cls, cls or "Control")
            name = _get_text(hwnd)
            if ctrl_type not in _INTERACTIVE_TYPES and not name:
                return True

            style = _user32.GetWindowLongW(hwnd, _GWL_STYLE)
            enabled = not bool(style & _WS_DISABLED)

            cx = left + width // 2
            cy = top + height // 2
            text = f"[{ctrl_type}] {name}" if name else f"[{ctrl_type}]"
            results.append(
                {
                    "type": ctrl_type,
                    "name": name,
                    "text": text,
                    "cx": cx,
                    "cy": cy,
                    "w": width,
                    "h": height,
                    "enabled": enabled,
                    "source": "uia",
                }
            )
            return True

        _user32.EnumChildWindows(parent, _EnumProc(_cb), 0)
        return results[:250]


class UiaForegroundElementsTool:
    def __init__(self, runtime: UiaRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="uia.get_foreground_elements",
            description="Get foreground window title and UI elements.",
            is_mutating=False,
            output_schema={"title": "str", "elements": "list[dict]"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))
        title, elements = self._runtime.get_foreground_elements()
        return ToolResult(success=True, data={"title": title, "elements": elements})


class UiaListVisibleWindowsTool:
    def __init__(self, runtime: UiaRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="uia.list_visible_windows",
            description="List currently visible windows.",
            is_mutating=False,
            output_schema={"windows": "list[dict]"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))
        return ToolResult(success=True, data={"windows": self._runtime.list_visible_windows()})


class UiaListAllWindowsTool:
    def __init__(self, runtime: UiaRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="uia.list_all_windows",
            description="List visible and minimized windows.",
            is_mutating=False,
            output_schema={"windows": "list[dict]"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))
        return ToolResult(success=True, data={"windows": self._runtime.list_all_windows()})


class UiaFocusWindowTool:
    def __init__(self, runtime: UiaRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="uia.focus_window",
            description="Restore and focus a window by hwnd.",
            is_mutating=True,
            input_schema={"required": ["hwnd"]},
            output_schema={"focused": "bool"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))
        if "hwnd" not in args:
            return ToolResult(success=False, error=ToolError(code="missing_hwnd", message="'hwnd' is required."))
        focused = self._runtime.focus_window(int(args["hwnd"]))
        return ToolResult(success=focused, data={"focused": focused})
