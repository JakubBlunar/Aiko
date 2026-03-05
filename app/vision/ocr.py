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

    def extract_elements(
        self,
        image,
        *,
        screen_left: int = 0,
        screen_top: int = 0,
    ) -> list[dict]:
        """Run OCR and return one dict per detected text region with screen-space
        coordinates.

        Each dict has:
          ``text``       – the recognised string
          ``cx``, ``cy`` – screen-pixel centre of the bounding box
          ``w``, ``h``   – bounding-box width / height in screen pixels
          ``confidence`` – recognition confidence 0–1
        """
        if self._engine is None or image is None:
            return []

        try:
            array = np.asarray(image)
        except Exception:
            return []

        if array.ndim < 2:
            return []

        # Compute the stride that _prepare_image applies so we can map
        # OCR coordinates (in the down-sampled image) back to screen pixels.
        max_side = max(0, int(getattr(self._settings, "ocr_max_side_px", 1600)))
        height = int(array.shape[0])
        width = int(array.shape[1])
        longest = max(height, width)
        if max_side > 0 and longest > max_side:
            stride = max(1, int(np.ceil(float(longest) / float(max_side))))
        else:
            stride = 1

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

            # item[0] is the bounding box: [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            # in coordinates of the *prepared* (possibly strided) image.
            try:
                pts = np.asarray(item[0], dtype=float)  # shape (4, 2)
                xs = pts[:, 0]
                ys = pts[:, 1]
                cx_img = float(np.mean(xs))
                cy_img = float(np.mean(ys))
                w_img = float(np.max(xs) - np.min(xs))
                h_img = float(np.max(ys) - np.min(ys))
            except Exception:
                continue

            elements.append({
                "text": text,
                "cx": int(cx_img * stride) + screen_left,
                "cy": int(cy_img * stride) + screen_top,
                "w": max(1, int(w_img * stride)),
                "h": max(1, int(h_img * stride)),
                "confidence": round(confidence, 3),
            })

        return elements
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
