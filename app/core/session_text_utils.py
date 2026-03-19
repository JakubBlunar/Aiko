from __future__ import annotations

import json
import re
import unicodedata


def extract_json_object(raw_text: str) -> dict | None:
    try:
        direct = json.loads(raw_text)
        return direct if isinstance(direct, dict) else None
    except Exception:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start < 0 or end <= start:
        return None

    fragment = raw_text[start : end + 1]
    try:
        nested = json.loads(fragment)
        return nested if isinstance(nested, dict) else None
    except Exception:
        return None


def sanitize_user_text(text: str) -> str:
    cleaned = str(text or "")
    if not cleaned:
        return ""

    cleaned = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", " ", cleaned)

    out_chars: list[str] = []
    for ch in cleaned:
        category = unicodedata.category(ch)
        if category.startswith("C"):
            continue
        out_chars.append(ch)

    cleaned = "".join(out_chars)
    cleaned = re.sub(r"[^\w\s\.,!?;:'\"()\-]", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


def sanitize_assistant_text(
    text: str,
    *,
    preserve_newlines: bool = True,
    trim: bool = True,
) -> str:
    cleaned = unicodedata.normalize("NFKC", str(text or ""))
    if not cleaned:
        return ""

    cleaned = (
        cleaned.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )

    cleaned = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", cleaned)
    cleaned = re.sub(
        r"(?<![\w])([:;=8Xx][-o*']?[)DPpOo03{}\[\]|/\\]|[)DPp][:;=]|\^[_-]?\^|>_<|<3|:\*|;\*)(?![\w])",
        "",
        cleaned,
    )

    out_chars: list[str] = []
    for ch in cleaned:
        code = ord(ch)
        if ch == "\n" and preserve_newlines:
            out_chars.append(ch)
            continue
        if ch == "\n" and not preserve_newlines:
            out_chars.append(" ")
            continue
        if ch == "\t":
            out_chars.append(" ")
            continue
        if 32 <= code <= 126:
            out_chars.append(ch)

    cleaned = "".join(out_chars)
    if preserve_newlines:
        cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    else:
        cleaned = re.sub(r" {2,}", " ", cleaned)

    if trim:
        return cleaned.strip()
    return cleaned


def prepare_tts_text(text: str) -> str:
    """Clean text for TTS playback (audio path only; transcript is untouched)."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    # Remove fenced code blocks entirely
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    # Remove inline code
    cleaned = cleaned.replace("`", "")
    # Remove markdown headers (e.g. "## Title" -> "Title")
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.replace("#", "")
    # Remove URLs
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    # Remove bullet markers at line start
    cleaned = re.sub(r"^[\-\*]\s+", "", cleaned, flags=re.MULTILINE)
    # Strip bold / italic markdown
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"_(.+?)_", r"\1", cleaned)
    # Remove brackets
    cleaned = cleaned.replace("[", "").replace("]", "")
    # Replace very long numbers with a speakable placeholder
    cleaned = re.sub(r"\d{7,}", "a large number", cleaned)
    # Strip tildes -- Kokoro reads them literally
    cleaned = cleaned.replace("~", "")
    cleaned = " ".join(cleaned.split())
    return cleaned


def infer_tts_reaction(text: str) -> str:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return "neutral"

    if "[action]" in lowered:
        return "excited"
    if any(token in lowered for token in ("!", "wow", "amazing", "great", "awesome")):
        return "excited"
    if any(token in lowered for token in ("surprised", "unexpected", "didn't expect", "whoa")):
        return "surprised"
    if any(token in lowered for token in ("sorry", "unfortunately", "sad", "regret")):
        return "sad"
    if any(token in lowered for token in ("angry", "frustrated", "annoyed", "this is wrong")):
        return "angry"
    if any(token in lowered for token in ("calm", "let's slow", "take it step", "no rush")):
        return "calm"
    return "neutral"


def drain_tts_stream_chunks(buffer: str, *, flush: bool) -> tuple[list[str], str]:
    text = str(buffer or "")
    if not text:
        return [], ""

    chunks: list[str] = []
    start = 0
    for index, ch in enumerate(text):
        if ch not in ".!?\n":
            continue

        candidate = text[start : index + 1].strip()
        if not candidate:
            start = index + 1
            continue

        if len(candidate) >= 24 or candidate.count(" ") >= 4 or ch == "\n":
            chunks.append(candidate)
            start = index + 1

    remainder = text[start:]
    if flush and remainder.strip():
        chunks.append(remainder.strip())
        remainder = ""

    return chunks, remainder
