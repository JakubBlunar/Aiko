"""Post-turn reflection in the speaking window (Phase 2c).

After each user turn, we submit a low-priority job to the
:class:`SpeakingWindowScheduler` that asks the LLM for a brief structured
journal of the turn. The output is parsed into:

  - a one-line inner observation (persisted as ``kind="reflection"``),
  - any open questions Aiko found herself wondering about
    (persisted as ``kind="open_question"``),
  - any callbacks she'd like to come back to later
    (persisted as ``kind="callback"``).

Both ``open_question`` and ``callback`` memories surface naturally
through the existing RAG retriever and serve as fuel for ProactiveDirector.

Throttling:

* Don't run twice within ``min_seconds_between`` (default 8s).
* Skip when the emotional delta is too small to be interesting (default
  0.05) — a flat-affect "ok, sounds good" exchange has nothing to say.

Failure modes are best-effort: a malformed JSON response, a network
hiccup, or any exception inside the worker is logged and dropped. The
hot path never sees it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from app.core.session_text_utils import resolve_user_name

if TYPE_CHECKING:
    from app.core.affect_state import AffectState
    from app.core.memory_store import MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.reflection_worker")


def _build_reflection_prompt(user_display_name: str = "the user") -> str:
    name = user_display_name or "the user"
    return (
        "You are Aiko, in a quiet introspective moment between conversation "
        "turns. Look back at the most recent exchange and journal it briefly.\n"
        "\n"
        "Respond with ONE JSON object on a single line:\n"
        "{\n"
        "  \"observation\": \"<one short sentence: what stood out to you about this exchange>\",\n"
        "  \"open_questions\": [\"<question you found yourself wondering about>\", ...],\n"
        "  \"callbacks\": [\"<a thread you'd like to bring back up later>\", ...]\n"
        "}\n"
        "\n"
        "Rules:\n"
        "- Keep \"observation\" under 25 words.\n"
        "- 0-3 items per array. Empty arrays are fine.\n"
        f"- Each item: short, first-person, plain text. No quoting {name} verbatim.\n"
        "- If nothing notable happened, return empty arrays and a one-line observation.\n"
        "- Do NOT include any prose outside the JSON object."
    )


_REFLECTION_PROMPT = _build_reflection_prompt()


@dataclass(slots=True)
class Reflection:
    """Structured output of a single reflection pass."""

    observation: str = ""
    open_questions: list[str] = field(default_factory=list)
    callbacks: list[str] = field(default_factory=list)
    persisted_memory_ids: list[int] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.observation or self.open_questions or self.callbacks)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _parse_reflection_payload(raw: str) -> Reflection:
    """Best-effort parse of an LLM response into a :class:`Reflection`.

    Tolerates code fences, leading/trailing prose, and extra fields. Any
    parse failure returns an empty :class:`Reflection`.
    """
    text = (raw or "").strip()
    if not text:
        return Reflection()
    # Allow fenced ```json ... ``` blocks.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        m = _JSON_BLOCK_RE.search(text)
        candidate = m.group(0) if m else None
    if not candidate:
        return Reflection()
    try:
        data: Any = json.loads(candidate)
    except Exception:
        log.debug("reflection JSON parse failed", exc_info=True)
        return Reflection()
    if not isinstance(data, dict):
        return Reflection()
    observation = str(data.get("observation") or "").strip()
    open_qs = data.get("open_questions") or []
    callbacks = data.get("callbacks") or []

    def _clean_list(items: Any, *, max_len: int = 3) -> list[str]:
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if len(out) >= max_len:
                break
            txt = str(item or "").strip()
            if not txt or len(txt) < 4:
                continue
            # Strip surrounding quotes the model sometimes emits.
            if txt[0] in {'"', "'"} and txt[-1] == txt[0]:
                txt = txt[1:-1].strip()
            if txt and len(txt) >= 4:
                out.append(txt)
        return out

    return Reflection(
        observation=observation[:240],
        open_questions=_clean_list(open_qs),
        callbacks=_clean_list(callbacks),
    )


class ReflectionWorker:
    """LLM-driven reflection that runs inside the speaking window.

    The owner (:class:`SessionController`) builds a fresh
    :class:`Reflection` at the end of every turn by calling
    :meth:`maybe_run`. Returns ``None`` when throttled / disabled / no
    affect change to reflect on.
    """

    def __init__(
        self,
        *,
        ollama: "OllamaClient",
        memory_store: "MemoryStore | None",
        embedder: "Embedder | None",
        model: str,
        min_seconds_between: float = 8.0,
        emotional_delta_threshold: float = 0.05,
        max_tokens: int = 220,
        salience_open_question: float = 0.55,
        salience_callback: float = 0.5,
        salience_reflection: float = 0.4,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._memory_store = memory_store
        self._embedder = embedder
        self._model = model
        self._min_seconds_between = max(0.0, float(min_seconds_between))
        self._delta_threshold = max(0.0, float(emotional_delta_threshold))
        self._max_tokens = max(64, int(max_tokens))
        self._sal_q = max(0.0, min(1.0, float(salience_open_question)))
        self._sal_c = max(0.0, min(1.0, float(salience_callback)))
        self._sal_r = max(0.0, min(1.0, float(salience_reflection)))
        self._user_display_name_provider = user_display_name_provider
        self._last_run_at = 0.0
        self._stats = {
            "scheduled": 0,
            "skipped_recent": 0,
            "skipped_flat": 0,
            "completed": 0,
            "failed": 0,
            "memories_written": 0,
        }

    # ── public ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(
        self,
        *,
        model: str | None = None,
        min_seconds_between: float | None = None,
        emotional_delta_threshold: float | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if min_seconds_between is not None:
            self._min_seconds_between = max(0.0, float(min_seconds_between))
        if emotional_delta_threshold is not None:
            self._delta_threshold = max(0.0, float(emotional_delta_threshold))

    def maybe_run(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str,
        reaction: str,
        affect_before: "AffectState | None" = None,
        affect_after: "AffectState | None" = None,
        on_memory_added: Callable[[Any], None] | None = None,
    ) -> Reflection | None:
        """Run a reflection pass if not throttled. Returns ``None`` on skip."""
        now = time.monotonic()
        if now - self._last_run_at < self._min_seconds_between:
            self._stats["skipped_recent"] += 1
            return None
        if affect_before is not None and affect_after is not None:
            delta = max(
                abs(affect_after.valence - affect_before.valence),
                abs(affect_after.arousal - affect_before.arousal),
            )
            if delta < self._delta_threshold:
                self._stats["skipped_flat"] += 1
                return None
        # Reserve our turn (do this before the LLM call so a failure still
        # counts toward throttling — otherwise a flaky model would make us
        # spin).
        self._last_run_at = now
        self._stats["scheduled"] += 1

        return self._run(
            session_key=session_key,
            user_text=user_text,
            assistant_text=assistant_text,
            reaction=reaction,
            affect=affect_after,
            on_memory_added=on_memory_added,
        )

    # ── internals ───────────────────────────────────────────────────────

    def _run(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str,
        reaction: str,
        affect: "AffectState | None",
        on_memory_added: Callable[[Any], None] | None,
    ) -> Reflection | None:
        try:
            messages = [
                {
                    "role": "system",
                    "content": _build_reflection_prompt(
                        resolve_user_name(self._user_display_name_provider),
                    ),
                },
                {
                    "role": "user",
                    "content": _format_turn_block(
                        user_text=user_text,
                        assistant_text=assistant_text,
                        reaction=reaction,
                        affect=affect,
                        user_display_name=resolve_user_name(
                            self._user_display_name_provider,
                        ),
                    ),
                },
            ]
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.4,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                surface="reflection_worker",
            )
        except Exception:
            log.debug("reflection LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None

        reflection = _parse_reflection_payload(raw)
        if reflection.is_empty():
            self._stats["completed"] += 1
            return reflection

        # Persist memories. Each is best-effort; we never raise.
        self._persist_memories(
            reflection,
            session_key=session_key,
            on_memory_added=on_memory_added,
        )
        self._stats["completed"] += 1
        return reflection

    def _persist_memories(
        self,
        reflection: Reflection,
        *,
        session_key: str,
        on_memory_added: Callable[[Any], None] | None,
    ) -> None:
        if self._memory_store is None or self._embedder is None:
            return

        def _write(content: str, kind: str, salience: float) -> None:
            content = (content or "").strip()
            if not content or len(content) < 4:
                return
            try:
                emb = self._embedder.embed(content)
            except Exception:
                log.debug("reflection embed failed", exc_info=True)
                return
            try:
                memory = self._memory_store.add(
                    content=content,
                    kind=kind,
                    embedding=emb,
                    salience=salience,
                    source_session=session_key,
                    source_message_id=None,
                    # Schema v8: reflections / open questions /
                    # callbacks are speculative LLM-journal output.
                    # Scratchpad so they decay fast unless they prove
                    # useful via retrieval / revival.
                    tier="scratchpad",
                )
            except Exception:
                log.debug("reflection memory insert failed", exc_info=True)
                return
            if memory is None:
                return
            reflection.persisted_memory_ids.append(int(memory.id))
            self._stats["memories_written"] += 1
            if on_memory_added is not None:
                try:
                    on_memory_added(memory)
                except Exception:
                    log.debug("reflection on_memory_added raised", exc_info=True)

        if reflection.observation:
            _write(reflection.observation, "reflection", self._sal_r)
        for q in reflection.open_questions:
            _write(q, "open_question", self._sal_q)
        for c in reflection.callbacks:
            _write(c, "callback", self._sal_c)


def _format_turn_block(
    *,
    user_text: str,
    assistant_text: str,
    reaction: str,
    affect: "AffectState | None",
    user_display_name: str = "Jacob",
) -> str:
    """Compact turn dump for the reflection prompt."""
    lines = []
    if affect is not None:
        lines.append(
            f"(your current mood: {affect.mood_label}, "
            f"valence={affect.valence:+.2f}, arousal={affect.arousal:.2f})"
        )
    name = user_display_name or "the user"
    lines.append(f"{name}: {(user_text or '').strip()[:1200]}")
    lines.append(f"You ({reaction or 'neutral'}): {(assistant_text or '').strip()[:1200]}")
    return "\n".join(lines)


__all__ = ["ReflectionWorker", "Reflection", "_parse_reflection_payload"]
