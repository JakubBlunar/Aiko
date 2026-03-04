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
        details = self.extract_details(image)
        if not details:
            return None
        return str(details.get("text") or "") or None

    def extract_details(self, image) -> dict[str, object] | None:
        if self._engine is None or image is None:
            return None

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
