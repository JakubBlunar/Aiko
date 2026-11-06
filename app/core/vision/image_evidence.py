"""A visual note describes a representation, not an observed physical event."""
import json
import re

class ImageMemoryLine(str):
    def __new__(cls, text, details=None):
        value = super().__new__(cls, text)
        value.details = details or {"version": 1, "subject": "unknown",
                                    "depicted_domain": "unknown", "precision": "unknown"}
        return value


def parse_note(raw, *, caption, description, shared_at, max_chars):
    text = (raw or "").strip()
    if not text or text.upper().startswith("SKIP"):
        return ""
    details = {"version": 1, "subject": "unknown", "depicted_domain": "unknown",
               "precision": "unknown", "depicted_time": None}
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return ""
        text = value.get("summary")
        if not isinstance(text, str):
            return ""
        for key, source in (("caption_quote", caption), ("visual_quote", description),
                            ("subject_quote", caption), ("domain_quote", caption)):
            quote = value.get(key, "")
            if not isinstance(quote, str) or len(quote) > 320 or quote and quote not in source:
                return ""
            details[key] = quote
        subject = value.get("subject", "unknown")
        if subject in {"user", "aiko", "relationship", "world"} and details["subject_quote"]:
            details["subject"] = subject
        domain = value.get("depicted_domain", "unknown")
        if domain in {"real", "virtual"} and details["domain_quote"]:
            details["depicted_domain"] = domain
        quote = value.get("depicted_time_quote")
        if isinstance(quote, str) and quote and quote in caption:
            from app.core.memory.promise_evidence import supported_deadline

            # Year/season-only language is not a specific calendar day.
            if not re.search(r"\b(?:year|summer|winter|spring|autumn|fall)\b", quote, re.I):
                when, precision = supported_deadline(quote, caption, shared_at)
                if when is not None:
                    if precision == "day":
                        when = when.replace(hour=0, minute=0)
                    details.update(depicted_time=when.isoformat(), precision=precision,
                                   depicted_time_quote=quote)
        if not (details["caption_quote"] or details["visual_quote"]):
            return ""
    else:
        # Older/plain worker replies remain a visual interpretation of unknown
        # subject. Their wording cannot establish a real event or ownership.
        if len(text) > 1 and text[0] in "\"'" and text[-1] == text[0]:
            text = text[1:-1].strip()
    text = " ".join(text.split())
    if not text or len(text) > max_chars:
        return ""
    return ImageMemoryLine(text, details)


def source_current(receipt, store):
    if receipt is None:
        return True
    from app.core.memory.observation_receipts import receipt_sources_current

    return receipt_sources_current({"version": 1, "sources": [receipt]}, store)
