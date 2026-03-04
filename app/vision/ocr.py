from __future__ import annotations

import numpy as np

from app.core.settings import ScreenSettings


class OcrService:
    def __init__(self, settings: ScreenSettings) -> None:
        self._settings = settings
        self._engine = None
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        except Exception:
            self._engine = None

    def extract_text(self, image) -> str | None:
        details = self.extract_details(image)
        if not details:
            return None
        return str(details.get("text") or "") or None

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
                    score = float(item[2])
                    confidences.append(score)
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

        stride = max(1, int(np.ceil(float(longest) / float(max_side))))
        if stride <= 1:
            return array
        return array[::stride, ::stride]
