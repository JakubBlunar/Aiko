from __future__ import annotations


class OcrService:
    def __init__(self) -> None:
        self._engine = None
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        except Exception:
            self._engine = None

    def extract_text(self, image) -> str | None:
        if self._engine is None or image is None:
            return None

        result, _ = self._engine(image)
        if not result:
            return None
        return " ".join(item[1] for item in result if len(item) > 1)
