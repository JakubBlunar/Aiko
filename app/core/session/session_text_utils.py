from __future__ import annotations

import json
import re
import unicodedata
from typing import Callable


# ── Identity helpers ────────────────────────────────────────────────────


def resolve_user_name(
    provider: Callable[[], str] | None,
    *,
    fallback: str = "the user",
) -> str:
    """Best-effort resolve a user display name from an optional callable.

    Returns ``fallback`` whenever the provider is missing, raises, or
    returns an empty/whitespace value. Workers that cache the resolved
    name in a per-run system prompt route through this so a rename via
    onboarding propagates without per-worker exception handling.
    """
    if provider is None:
        return fallback
    try:
        name = (provider() or "").strip()
    except Exception:
        return fallback
    return name or fallback


def speaker_label(
    role: str,
    user_display_name: str,
    *,
    assistant_name: str = "Aiko",
) -> str:
    """Map a transcript role to a human-readable speaker label.

    Mirrors the ``"Jacob" if role == "user" else "Aiko"`` pattern that
    used to live inline in ~8 worker modules. ``role`` is matched
    case-insensitively; any non-``"user"`` role (assistant, system, …)
    collapses to ``assistant_name``.
    """
    name = (user_display_name or "the user").strip() or "the user"
    if (role or "").strip().lower() == "user":
        return name
    return assistant_name


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
    # Phase 3c: drop ``[[correct]]old[[/correct]]`` blocks entirely so
    # TTS only speaks the corrected text (the ``new`` half lives
    # *outside* the block). Done before the generic ``[[...]]`` strip
    # below so the inner ``old`` text doesn't slip through.
    cleaned = re.sub(
        r"\[\[correct\]\][\s\S]*?\[\[/correct\]\]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Remove any remaining [[...]] tags (reaction, spoken, detail, etc.)
    cleaned = re.sub(r"\[\[[^\]]*\]\]", "", cleaned)
    # Remove brackets
    cleaned = cleaned.replace("[", "").replace("]", "")
    # Replace very long numbers with a speakable placeholder
    cleaned = re.sub(r"\d{7,}", "a large number", cleaned)
    # Strip tildes -- Kokoro reads them literally
    cleaned = cleaned.replace("~", "")
    # Strip double quotes -- the TTS model occasionally vocalises a stray
    # or empty pair ('""') as a glitchy artifact. Apostrophes (single
    # quotes) are kept so contractions ("don't") survive.
    cleaned = cleaned.replace('"', "")
    # Speak filename / extension dots so the model doesn't read ".ext" as
    # a sentence terminator and insert a pause ("report.txt" -> "report
    # dot txt"). Only fires when a letter directly follows the dot, so
    # decimals (3.14) and version numbers (v2.0) are left for the model
    # to read normally.
    cleaned = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z])", " dot ", cleaned)
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
        if ch == "\n":
            pass  # a newline is always a hard boundary
        elif ch in ".!?":
            nxt = text[index + 1] if index + 1 < len(text) else ""
            # A terminator glued to a word char on the right is *inside*
            # a token, not a sentence end: file.ext, 3.14, U.S.A,
            # Yahoo!Inc. An empty ``nxt`` means the terminator is the
            # last char streamed so far -- wait for the next delta to
            # reveal whether it's "done. " or "report.txt" (the flush
            # path emits any trailing remainder regardless).
            if nxt == "" or nxt.isalnum():
                continue
        else:
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
