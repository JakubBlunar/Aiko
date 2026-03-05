from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time


@dataclass(slots=True)
class ScreenCaptureResult:
    text: str | None
    elements: list[dict]
    foreground_window_title: str
    open_windows: list[dict]
    all_windows: list[dict]


class ScreenContextService:
    def __init__(
        self,
        *,
        screen_settings: object,
        planner_chat: Callable[[list[dict[str, str]]], str],
        history_messages: Callable[[int], list[dict[str, str]]],
        ocr_extract_elements: Callable[..., list[dict]],
        ocr_extract_details: Callable[..., dict[str, object] | None],
        uia_get_foreground_elements: Callable[[], tuple[str, list[dict]]],
        uia_list_visible_windows: Callable[[], list[dict]],
        uia_list_all_windows: Callable[[], list[dict]],
        trace: Callable[[str, str], None],
        screen_capture_once_with_region: Callable[..., tuple[object | None, dict | None]],
        screen_capture_once: Callable[..., object | None],
    ) -> None:
        self._screen_settings = screen_settings
        self._planner_chat = planner_chat
        self._history_messages = history_messages
        self._ocr_extract_elements = ocr_extract_elements
        self._ocr_extract_details = ocr_extract_details
        self._uia_get_foreground_elements = uia_get_foreground_elements
        self._uia_list_visible_windows = uia_list_visible_windows
        self._uia_list_all_windows = uia_list_all_windows
        self._trace = trace
        self._capture_once_with_region = screen_capture_once_with_region
        self._capture_once = screen_capture_once

        self._last_screen_decision_at = 0.0
        self._last_screen_text = ""
        self._last_screen_text_at = 0.0

    @staticmethod
    def is_screen_intent(user_text: str) -> bool:
        normalized = (user_text or "").lower()
        triggers = (
            "screen",
            "on my screen",
            "look at",
            "what do you see",
            "what can you see",
            "see this",
            "read this",
            "from the screen",
        )
        return any(token in normalized for token in triggers)

    def should_capture_screen(self, *, user_text: str, screen_enabled: bool) -> tuple[bool, str]:
        normalized = (user_text or "").lower()
        if not screen_enabled:
            return False, "disabled"

        keyword_triggers = (
            "screen",
            "on my screen",
            "look at",
            "look on",
            "check screen",
            "check my screen",
            "what do you see",
            "what can you see",
            "what's on my screen",
            "whats on my screen",
            "this page",
            "this window",
            "this code",
            "here",
            "shown",
            "read this",
        )
        if any(token in normalized for token in keyword_triggers):
            return True, "keyword"

        now = time.monotonic()
        decision_mode = (getattr(self._screen_settings, "decision_mode", "model") or "model").lower().strip()
        if decision_mode == "keywords":
            return False, "keywords-only"

        cooldown = max(1, int(getattr(self._screen_settings, "decision_cooldown_seconds", 8)))
        if (now - self._last_screen_decision_at) < cooldown:
            return False, "decision-cooldown"
        self._last_screen_decision_at = now

        recent_memory = self._history_messages(4)
        recent_lines: list[str] = []
        for item in recent_memory:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                recent_lines.append(f"{role}: {content}")
        recent_text = "\n".join(recent_lines)

        try:
            decision = self._planner_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Decide whether checking the user's current screen is needed to answer the user's latest message. "
                            "Use latest message plus recent conversation. "
                            "Reply with only YES or NO."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Latest message:\n{user_text.strip()}\n\n"
                            f"Recent conversation:\n{recent_text or '[none]'}"
                        ),
                    },
                ]
            )
        except Exception:
            self._trace("screen.decision", "Screen decision unavailable (model error), skipping capture.")
            return False, "model-error"

        capture = decision.strip().lower().startswith("y")
        self._trace(
            "screen.decision",
            f"Decision={'YES' if capture else 'NO'} for latest message: {(user_text or '').strip()[:140]}",
        )
        return capture, "model"

    def capture_screen_text(self, *, decision_source: str) -> ScreenCaptureResult:
        frame, region = self._capture_once_with_region()
        if frame is None:
            self._trace("screen.capture", "Screen capture unavailable.")
            return ScreenCaptureResult(
                text=None,
                elements=[],
                foreground_window_title="",
                open_windows=[],
                all_windows=[],
            )

        screen_left = int((region or {}).get("left", 0))
        screen_top = int((region or {}).get("top", 0))
        screen_width = int((region or {}).get("width", 0))
        screen_height = int((region or {}).get("height", 0))
        elements = list(
            self._ocr_extract_elements(
                frame,
                screen_left=screen_left,
                screen_top=screen_top,
                screen_width=screen_width,
                screen_height=screen_height,
            )
            or []
        )
        used_fallback = False

        if not elements and bool(getattr(self._screen_settings, "capture_active_window_only", False)):
            self._trace("screen.capture", "Active-window OCR returned no elements; retrying full monitor capture.")
            fb_frame, fb_region = self._capture_once_with_region(active_window_only=False)
            if fb_frame is not None:
                fb_left = int((fb_region or {}).get("left", 0))
                fb_top = int((fb_region or {}).get("top", 0))
                fb_width = int((fb_region or {}).get("width", 0))
                fb_height = int((fb_region or {}).get("height", 0))
                elements = list(
                    self._ocr_extract_elements(
                        fb_frame,
                        screen_left=fb_left,
                        screen_top=fb_top,
                        screen_width=fb_width,
                        screen_height=fb_height,
                    )
                    or []
                )
                used_fallback = True

        foreground_window_title = ""
        open_windows: list[dict] = []
        all_windows: list[dict] = []
        if bool(getattr(self._screen_settings, "enable_uia", True)):
            try:
                foreground_window_title, uia_els = self._uia_get_foreground_elements()
                open_windows = list(self._uia_list_visible_windows() or [])
                all_windows = list(self._uia_list_all_windows() or [])
                if uia_els:
                    uia_dicts = [
                        {
                            "text": f"[{e['type']}] {e['name']}" if e.get("name") else f"[{e['type']}]",
                            "type": e["type"],
                            "name": e.get("name", ""),
                            "cx": e["cx"],
                            "cy": e["cy"],
                            "w": e["w"],
                            "h": e["h"],
                            "enabled": e.get("enabled", True),
                            "source": "uia",
                            "window_title": foreground_window_title,
                        }
                        for e in uia_els
                    ]

                    def _ocr_overlaps(ocr_el: dict, uia_list: list[dict]) -> bool:
                        cx, cy = ocr_el["cx"], ocr_el["cy"]
                        for u in uia_list:
                            ux1 = u["cx"] - u["w"] // 2
                            ux2 = u["cx"] + u["w"] // 2
                            uy1 = u["cy"] - u["h"] // 2
                            uy2 = u["cy"] + u["h"] // 2
                            if ux1 <= cx <= ux2 and uy1 <= cy <= uy2:
                                return True
                        return False

                    ocr_only = [e for e in elements if not _ocr_overlaps(e, uia_dicts)]
                    elements = uia_dicts + ocr_only
            except Exception as exc:
                self._trace("screen.uia", f"UIA enrichment failed: {exc}")

        text = " ".join(e["text"] for e in elements).strip()
        text = " ".join(text.split())
        if not text:
            self._trace("screen.capture", "OCR returned no text.")
            return ScreenCaptureResult(
                text=None,
                elements=elements,
                foreground_window_title=foreground_window_title,
                open_windows=open_windows,
                all_windows=all_windows,
            )

        min_chars = max(0, int(getattr(self._screen_settings, "min_ocr_chars", 0)))
        if len(text) < min_chars:
            self._trace(
                "screen.capture",
                f"OCR text below minimum length: {len(text)} < {min_chars}. Ignoring capture.",
            )
            return ScreenCaptureResult(
                text=None,
                elements=elements,
                foreground_window_title=foreground_window_title,
                open_windows=open_windows,
                all_windows=all_windows,
            )

        now = time.monotonic()
        reuse_window = max(0, int(getattr(self._screen_settings, "unchanged_reuse_seconds", 0)))
        is_unchanged = text == self._last_screen_text
        within_window = (now - self._last_screen_text_at) <= reuse_window if self._last_screen_text_at else False

        if is_unchanged and within_window and decision_source != "keyword":
            self._trace(
                "screen.capture",
                f"Screen OCR unchanged within reuse window ({reuse_window}s). Skipping repeated context.",
            )
            return ScreenCaptureResult(
                text=None,
                elements=elements,
                foreground_window_title=foreground_window_title,
                open_windows=open_windows,
                all_windows=all_windows,
            )

        self._last_screen_text = text
        self._last_screen_text_at = now
        self._trace(
            "screen.capture",
            (
                f"Captured screen context ({len(text)} chars, {len(elements)} elements, source={decision_source}"
                f", fallback={'yes' if used_fallback else 'no'})."
            ),
        )
        return ScreenCaptureResult(
            text=text,
            elements=elements,
            foreground_window_title=foreground_window_title,
            open_windows=open_windows,
            all_windows=all_windows,
        )

    def run_ocr_diagnostic(self) -> dict[str, object]:
        frame = self._capture_once()
        if frame is None:
            return {
                "ok": False,
                "reason": "capture-unavailable",
                "message": "Screen capture unavailable.",
            }

        used_fallback = False
        details = self._ocr_extract_details(frame)
        if not details and bool(getattr(self._screen_settings, "capture_active_window_only", False)):
            fallback_frame = self._capture_once(active_window_only=False)
            if fallback_frame is not None:
                details = self._ocr_extract_details(fallback_frame)
                frame = fallback_frame
                used_fallback = True

        if not details:
            return {
                "ok": False,
                "reason": "ocr-empty",
                "message": "OCR returned no text.",
                "capture_mode": (
                    "active-window"
                    if bool(getattr(self._screen_settings, "capture_active_window_only", False))
                    else "monitor"
                ),
                "retried_full_monitor": used_fallback,
            }

        text = str(details.get("text") or "").strip()
        text = " ".join(text.split())
        if not text:
            return {
                "ok": False,
                "reason": "ocr-empty",
                "message": "OCR returned no readable text.",
                "capture_mode": (
                    "active-window"
                    if bool(getattr(self._screen_settings, "capture_active_window_only", False))
                    else "monitor"
                ),
                "retried_full_monitor": used_fallback,
            }

        min_chars = max(0, int(getattr(self._screen_settings, "min_ocr_chars", 0)))
        frame_height = (
            int(frame.shape[0])
            if getattr(frame, "shape", None) is not None and len(frame.shape) >= 2
            else 0
        )
        frame_width = (
            int(frame.shape[1])
            if getattr(frame, "shape", None) is not None and len(frame.shape) >= 2
            else 0
        )
        return {
            "ok": True,
            "reason": "ok",
            "message": "OCR diagnostic captured text.",
            "chars": len(text),
            "min_chars": min_chars,
            "passes_min_chars": len(text) >= min_chars,
            "line_count": int(details.get("line_count") or 0),
            "avg_confidence": float(details.get("avg_confidence") or 0.0),
            "capture_mode": (
                "active-window"
                if bool(getattr(self._screen_settings, "capture_active_window_only", False))
                else "monitor"
            ),
            "retried_full_monitor": used_fallback,
            "frame_width": frame_width,
            "frame_height": frame_height,
            "text": text,
        }
