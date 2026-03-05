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
        screen_width: int = 0,
        screen_height: int = 0,
    ) -> list[dict]:
        """Run OCR and return one dict per detected text region with screen-space
        coordinates.

        Each dict has:
          ``text``       – the recognised string
          ``cx``, ``cy`` – screen-pixel centre of the bounding box (logical pixels)
          ``w``, ``h``   – bounding-box width / height in logical screen pixels
          ``confidence`` – recognition confidence 0–1

        ``screen_width`` / ``screen_height`` are the *logical* pixel dimensions of
        the captured region (from the mss monitor dict).  Providing them enables
        correct DPI-scale compensation when running on a HiDPI / scaled display
        where mss returns a physically larger image than the logical monitor size.
        """
        if self._engine is None or image is None:
            return []

        try:
            array = np.asarray(image)
        except Exception:
            return []

        if array.ndim < 2:
            return []

        # Compute the scale that _prepare_image applies so we can map
        # OCR coordinates (in the down-sampled image) back to screen pixels.
        # _prepare_image now uses proper antialiased resize, so we track a float
        # scale_x / scale_y rather than an integer stride.
        max_side = max(0, int(getattr(self._settings, "ocr_max_side_px", 1600)))
        height = int(array.shape[0])
        width = int(array.shape[1])
        longest = max(height, width)
        if max_side > 0 and longest > max_side:
            resize_scale = float(max_side) / float(longest)
        else:
            resize_scale = 1.0

        # Combined scale: OCR-image pixel → physical capture pixel → logical screen pixel.
        # DPI scale: mss captures at physical resolution but monitor dict uses
        # logical pixels.  Compute scale so final coords are in logical pixels.
        # If screen_width/height weren't supplied, assume no DPI scaling (1.0).
        if screen_width > 0 and width > 0:
            dpi_scale_x = float(width) / float(screen_width)
        else:
            dpi_scale_x = 1.0
        if screen_height > 0 and height > 0:
            dpi_scale_y = float(height) / float(screen_height)
        else:
            dpi_scale_y = 1.0

        # Final multipliers: OCR coord × (1/resize_scale) / dpi_scale → logical pixel
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
                "cx": int(cx_img * coord_scale_x) + screen_left,
                "cy": int(cy_img * coord_scale_y) + screen_top,
                "w": max(1, int(w_img * coord_scale_x)),
                "h": max(1, int(h_img * coord_scale_y)),
                "confidence": round(confidence, 3),
            })

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

        # Use proper antialiased resize (area interpolation) so OCR quality is
        # not degraded by the aliasing artifacts of stride-based subsampling.
        try:
            import cv2
            return cv2.resize(array, (new_w, new_h), interpolation=cv2.INTER_AREA)
        except Exception:
            pass

        # Fallback: box-average downsampling via numpy if cv2 is unavailable.
        try:
            block_h = height // new_h
            block_w = width // new_w
            if block_h >= 1 and block_w >= 1:
                crop_h = new_h * block_h
                crop_w = new_w * block_w
                arr = array[:crop_h, :crop_w]
                if arr.ndim == 3:
                    return arr.reshape(new_h, block_h, new_w, block_w, arr.shape[2]).mean(axis=(1, 3)).astype(np.uint8)
                else:
                    return arr.reshape(new_h, block_h, new_w, block_w).mean(axis=(1, 3)).astype(np.uint8)
        except Exception:
            pass

        # Last resort: stride subsampling.
        stride = max(1, int(np.ceil(float(longest) / float(max_side))))
        return array[::stride, ::stride]
