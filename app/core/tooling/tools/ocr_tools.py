from __future__ import annotations

from collections.abc import Callable
import numpy as np

from app.core.settings import ScreenSettings
from app.core.tooling.types import ToolContext, ToolError, ToolResult, ToolSpec


class OcrRuntime:
    def __init__(self, settings: ScreenSettings) -> None:
        self._settings = settings
        self._engine = None
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        except Exception:
            self._engine = None

    def extract_details(self, image) -> dict[str, object] | None:
        if self._engine is None or image is None:
            return None

        image = self._prepare_image(image)
        try:
            result, _ = self._engine(image)
        except Exception:
            return None
        if not result:
            return None

        chunks: list[str] = []
        confidences: list[float] = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            text = str(item[1]).strip()
            if text:
                chunks.append(text)
            if len(item) > 2:
                try:
                    confidences.append(float(item[2]))
                except Exception:
                    pass

        if not chunks:
            return None

        avg_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0
        return {
            "text": " ".join(chunks),
            "line_count": len(chunks),
            "avg_confidence": avg_confidence,
        }

    def extract_elements(
        self,
        image,
        *,
        screen_left: int = 0,
        screen_top: int = 0,
        screen_width: int = 0,
        screen_height: int = 0,
    ) -> list[dict]:
        if self._engine is None or image is None:
            return []

        try:
            array = np.asarray(image)
        except Exception:
            return []

        if array.ndim < 2:
            return []

        max_side = max(0, int(getattr(self._settings, "ocr_max_side_px", 1600)))
        height = int(array.shape[0])
        width = int(array.shape[1])
        longest = max(height, width)
        if max_side > 0 and longest > max_side:
            resize_scale = float(max_side) / float(longest)
        else:
            resize_scale = 1.0

        dpi_scale_x = (float(width) / float(screen_width)) if screen_width > 0 and width > 0 else 1.0
        dpi_scale_y = (float(height) / float(screen_height)) if screen_height > 0 and height > 0 else 1.0
        coord_scale_x = 1.0 / (resize_scale * dpi_scale_x)
        coord_scale_y = 1.0 / (resize_scale * dpi_scale_y)

        prepared = self._prepare_image(array)

        try:
            result, _ = self._engine(prepared)
        except Exception:
            return []
        if not result:
            return []

        elements: list[dict] = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue

            text = str(item[1]).strip()
            if not text:
                continue

            confidence = 0.0
            if len(item) > 2:
                try:
                    confidence = float(item[2])
                except Exception:
                    pass

            try:
                pts = np.asarray(item[0], dtype=float)
                xs = pts[:, 0]
                ys = pts[:, 1]
                cx_img = float(np.mean(xs))
                cy_img = float(np.mean(ys))
                w_img = float(np.max(xs) - np.min(xs))
                h_img = float(np.max(ys) - np.min(ys))
            except Exception:
                continue

            elements.append(
                {
                    "text": text,
                    "cx": int(cx_img * coord_scale_x) + screen_left,
                    "cy": int(cy_img * coord_scale_y) + screen_top,
                    "w": max(1, int(w_img * coord_scale_x)),
                    "h": max(1, int(h_img * coord_scale_y)),
                    "confidence": round(confidence, 3),
                }
            )
        return elements

    def _prepare_image(self, image):
        try:
            array = np.asarray(image)
        except Exception:
            return image

        if array.ndim < 2:
            return image

        max_side = max(0, int(getattr(self._settings, "ocr_max_side_px", 1600)))
        if max_side <= 0:
            return array

        height = int(array.shape[0])
        width = int(array.shape[1])
        longest = max(height, width)
        if longest <= max_side:
            return array

        scale = float(max_side) / float(longest)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))

        try:
            import cv2

            return cv2.resize(array, (new_w, new_h), interpolation=cv2.INTER_AREA)
        except Exception:
            pass

        try:
            block_h = height // new_h
            block_w = width // new_w
            if block_h >= 1 and block_w >= 1:
                crop_h = new_h * block_h
                crop_w = new_w * block_w
                arr = array[:crop_h, :crop_w]
                if arr.ndim == 3:
                    return (
                        arr.reshape(new_h, block_h, new_w, block_w, arr.shape[2])
                        .mean(axis=(1, 3))
                        .astype(np.uint8)
                    )
                return arr.reshape(new_h, block_h, new_w, block_w).mean(axis=(1, 3)).astype(np.uint8)
        except Exception:
            pass

        stride = max(1, int(np.ceil(float(longest) / float(max_side))))
        return array[::stride, ::stride]


class OcrExtractElementsTool:
    def __init__(self, runtime: OcrRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="ocr.extract_elements",
            description="Extract OCR text elements with screen coordinates.",
            is_mutating=False,
            input_schema={
                "required": ["image"],
                "properties": {
                    "screen_left": "int",
                    "screen_top": "int",
                    "screen_width": "int",
                    "screen_height": "int",
                },
            },
            output_schema={"elements": "list[dict]"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))
        image = args.get("image")
        if image is None:
            return ToolResult(success=False, error=ToolError(code="missing_image", message="'image' is required."))

        elements = self._runtime.extract_elements(
            image,
            screen_left=int(args.get("screen_left", 0) or 0),
            screen_top=int(args.get("screen_top", 0) or 0),
            screen_width=int(args.get("screen_width", 0) or 0),
            screen_height=int(args.get("screen_height", 0) or 0),
        )
        return ToolResult(success=True, data={"elements": elements})


class OcrExtractDetailsTool:
    def __init__(self, runtime: OcrRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="ocr.extract_details",
            description="Extract OCR summary details from an image.",
            is_mutating=False,
            input_schema={"required": ["image"]},
            output_schema={"details": "dict|None"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))
        image = args.get("image")
        if image is None:
            return ToolResult(success=False, error=ToolError(code="missing_image", message="'image' is required."))
        details = self._runtime.extract_details(image)
        return ToolResult(success=True, data={"details": details})
