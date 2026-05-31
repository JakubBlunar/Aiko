"""LLM YES/NO gate for the K29 opinion-injection borderline path.

Thin wrapper around :meth:`OllamaClient.chat_stream` that asks the
model to decide whether the user's latest message contradicts one of
Aiko's stored ``kind="self"`` stance memories. Lives in its own
module (instead of inside the detector) so the detector can stay a
pure function with no I/O dependencies -- the borderline branch
plugs in any ``llm_gate`` callable, and this is just the one we
ship.

Mirrors :mod:`app.core.memory.memory_conflict_worker`'s
``_verify_with_llm`` shape (same JSON schema, same parse path) so
the same Ollama instance + cancel event plumbing works without
adapter glue. The K29 prompt is narrower: it's specifically about
"is the user's claim contradicting Aiko's stored stance" rather
than the more general "do these two memory rows contradict".

Output contract: ``"YES"`` / ``"NO"`` / ``"UNRELATED"`` or ``None``
on any failure (network, parse, cancel, malformed response). The
caller only fires the cue on ``"YES"`` -- anything else (including
``None``) stays silent. That keeps the contrarianism guardrail
working even when the LLM is having a bad day.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.opinion_injection_llm")


# Cap the response so a runaway model can't blow the budget. The
# real reply is one JSON object on one line; 80 tokens is enough
# room for the verdict + a short reason string.
_VERIFY_MAX_TOKENS = 80


_SYSTEM_PROMPT = (
    "You decide if Aiko's stored personal stance contradicts what the "
    "user just said. The stance is one of Aiko's own opinions written "
    "in her voice; the user message is what the user just typed. "
    "Answer with ONE JSON object on a single line and nothing else. "
    'Schema: {"verdict": "YES" | "NO" | "UNRELATED", '
    '"reason": "<= 80 chars"}. '
    "YES = the user's claim and Aiko's stance cannot both be true; "
    "Aiko has a genuinely different read on this topic. "
    "NO = both can be true (no contradiction; Aiko's stance and the "
    "user's claim sit alongside each other fine). "
    "UNRELATED = the stance and the user's claim are about different "
    "topics. "
    "Be strict: prefer NO or UNRELATED when uncertain. We're "
    "deliberately conservative to avoid making Aiko contrarian."
)


_USER_TEMPLATE = "user said: {user_text}\nAiko's stance: {stance_text}"


_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _preview(text: str | None, *, limit: int = 200) -> str:
    if not text:
        return ""
    cleaned = " ".join(text.split())
    return cleaned[:limit] + ("\u2026" if len(cleaned) > limit else "")


def verify(
    ollama: "OllamaClient",
    *,
    model: str,
    user_text: str,
    stance_text: str,
    cancel_event: threading.Event | None = None,
) -> str | None:
    """Run the borderline-stance LLM gate and return the bare verdict.

    Returns:
        ``"YES"`` / ``"NO"`` / ``"UNRELATED"`` on a successful parse;
        ``None`` on network failure, cancel, empty response, or
        anything the JSON parser refuses.
    """
    if not user_text or not stance_text:
        return None
    user_content = _USER_TEMPLATE.format(
        user_text=user_text.strip(),
        stance_text=stance_text.strip(),
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "opinion-injection verify prompt: model=%s prompt_chars=%d "
            "user_payload=%r stance_payload=%r",
            model,
            len(user_content) + len(_SYSTEM_PROMPT),
            _preview(user_text),
            _preview(stance_text),
        )
    chunks: list[str] = []
    try:
        stream = ollama.chat_stream(
            messages,
            options={"num_predict": _VERIFY_MAX_TOKENS},
            model=model,
            stop_event=cancel_event,
            format_json=True,
            surface="opinion_injection_llm",
        )
        for chunk in stream:
            chunks.append(chunk)
    except Exception:
        log.warning(
            "opinion-injection verify call raised", exc_info=True,
        )
        return None
    if cancel_event is not None and cancel_event.is_set():
        return None
    raw = "".join(chunks).strip()
    if not raw:
        return None
    log.debug(
        "opinion-injection verify raw: chars=%d preview=%r",
        len(raw),
        _preview(raw),
    )
    return _parse_verdict(raw)


def _parse_verdict(raw: str) -> str | None:
    match = _JSON_OBJECT_RE.search(raw or "")
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    verdict = str(parsed.get("verdict", "")).strip().upper()
    if verdict not in {"YES", "NO", "UNRELATED"}:
        return None
    return verdict
