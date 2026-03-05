"""Windows UI Automation via pure ctypes — no extra packages required.

Enumerates:
- All visible top-level windows (title, position)
- All child controls of a given window (type, name, position, enabled state)

Control class names are mapped to friendly type labels so the LLM gets
descriptive names like "Button", "TextInput", "Dropdown" rather than raw
Win32 class names like "SysListView32".
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time

_user32 = ctypes.windll.user32  # type: ignore[attr-defined]

_GWL_STYLE = -16
_WS_DISABLED = 0x08000000
_SW_RESTORE = 9

# Win32 class name → friendly control type
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

# Types that are interactive — include them even without visible text
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
    r = wintypes.RECT()
    if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        if r.right > r.left and r.bottom > r.top:
            return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return None


class UiaService:
    """Enumerate Windows UI controls via Win32 API (no COM / no extra packages)."""

    def list_visible_windows(self) -> list[dict]:
        """Return info about all visible top-level windows.

        Each dict::

            {hwnd, title, left, top, w, h, is_foreground}
        """
        results: list[dict] = []
        foreground = int(_user32.GetForegroundWindow())

        def _cb(hwnd: int, _: int) -> bool:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _get_text(hwnd)
            if not title:
                return True
            r = _get_rect(hwnd)
            if r is None:
                return True
            l, t, rr, b = r
            # Skip tiny utility / tray windows and minimized windows (Windows parks them at -32000)
            if (rr - l) < 50 or (b - t) < 20:
                return True
            if l <= -30000 or t <= -30000:
                return True
            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "left": l,
                    "top": t,
                    "w": rr - l,
                    "h": b - t,
                    "is_foreground": int(hwnd) == foreground,
                }
            )
            return True

        _user32.EnumWindows(_EnumProc(_cb), 0)
        return results

    def list_all_windows(self) -> list[dict]:
        """Return all titled top-level windows, including minimized ones.

        Same as :meth:`list_visible_windows` but includes minimized windows
        and marks them with ``is_minimized: True``.  Use this when the action
        planner needs hwnd handles to restore background / minimized windows.

        Each dict::

            {hwnd, title, left, top, w, h, is_foreground, is_minimized}
        """
        results: list[dict] = []
        foreground = int(_user32.GetForegroundWindow())

        def _cb(hwnd: int, _: int) -> bool:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _get_text(hwnd)
            if not title:
                return True
            r = _get_rect(hwnd)
            if r is None:
                return True
            l, t, rr, b = r
            if (rr - l) < 50 or (b - t) < 20:
                return True
            is_minimized = l <= -30000 or t <= -30000
            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "left": l,
                    "top": t,
                    "w": rr - l,
                    "h": b - t,
                    "is_foreground": int(hwnd) == foreground,
                    "is_minimized": is_minimized,
                }
            )
            return True

        _user32.EnumWindows(_EnumProc(_cb), 0)
        return results

    def focus_window(self, hwnd: int) -> bool:
        """Restore a minimized window and bring it to the foreground.

        Returns True if ``SetForegroundWindow`` succeeded.
        """
        if not hwnd:
            return False
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.3)
        return bool(_user32.SetForegroundWindow(hwnd))

    def get_foreground_elements(self) -> tuple[str, list[dict]]:
        """Return ``(window_title, elements)`` for the current foreground window.

        Each element dict::

            {type, name, text, cx, cy, w, h, enabled, source='uia'}

        ``text`` is ``"[Type] name"`` for backward compatibility with the OCR
        element format used elsewhere.
        """
        hwnd = int(_user32.GetForegroundWindow())
        if not hwnd:
            return "", []
        title = _get_text(hwnd)
        elements = self._get_child_elements(hwnd)
        return title, elements

    def get_window_elements(self, hwnd: int) -> list[dict]:
        """Return elements for an arbitrary window by handle."""
        return self._get_child_elements(hwnd)

    def _get_child_elements(self, parent: int) -> list[dict]:
        results: list[dict] = []

        def _cb(hwnd: int, _: int) -> bool:
            if not _user32.IsWindowVisible(hwnd):
                return True
            r = _get_rect(hwnd)
            if r is None:
                return True
            l, t, rr, b = r
            w, h = rr - l, b - t
            if w < 8 or h < 8:
                return True

            cls = _get_class(hwnd)
            ctrl_type = _CLASS_TYPE.get(cls, cls or "Control")
            name = _get_text(hwnd)

            # Keep interactive controls always; keep others only if they have text
            if ctrl_type not in _INTERACTIVE_TYPES and not name:
                return True

            style = _user32.GetWindowLongW(hwnd, _GWL_STYLE)
            enabled = not bool(style & _WS_DISABLED)

            cx = l + w // 2
            cy = t + h // 2
            # Build a "text" field compatible with the OCR element format
            if name:
                text = f"[{ctrl_type}] {name}"
            else:
                text = f"[{ctrl_type}]"

            results.append(
                {
                    "type": ctrl_type,
                    "name": name,
                    "text": text,
                    "cx": cx,
                    "cy": cy,
                    "w": w,
                    "h": h,
                    "enabled": enabled,
                    "source": "uia",
                }
            )
            return True

        _user32.EnumChildWindows(parent, _EnumProc(_cb), 0)
        # Cap to avoid flooding the context
        return results[:250]

    @staticmethod
    def merge_with_ocr(
        uia_elements: list[dict],
        ocr_elements: list[dict],
    ) -> list[dict]:
        """Merge UIA and OCR element lists.

        UIA elements take priority (exact positions, type annotations).
        OCR elements that are spatially far from all UIA elements are appended —
        they capture custom-rendered / web content that Win32 doesn't expose.
        """
        merged = list(uia_elements)

        for ocr in ocr_elements:
            cx_o, cy_o = ocr.get("cx", 0), ocr.get("cy", 0)
            overlaps = False
            for u in uia_elements:
                ux1 = u["cx"] - u["w"] // 2
                ux2 = u["cx"] + u["w"] // 2
                uy1 = u["cy"] - u["h"] // 2
                uy2 = u["cy"] + u["h"] // 2
                if ux1 <= cx_o <= ux2 and uy1 <= cy_o <= uy2:
                    overlaps = True
                    break
            if not overlaps:
                merged.append(ocr)

        return merged
