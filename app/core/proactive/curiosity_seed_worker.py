"""Curiosity-seed idle worker (K9 personality backlog).

Hybrid generator for "topics Aiko has been quietly wondering about
that haven't come up yet". Each idle tick:

1. Builds a small context pack (persona traits + latest rolling
   summary + a sample of cluster representatives from the K9
   :class:`app.core.conversation.topic_graph.TopicGraph`).
2. Asks the local LLM for 3-5 candidate seeds shaped as
   ``{topic, prompt_text, why}``. Schema-validated; falls through
   silently on a parse failure (the worker just doesn't write
   anything that tick).
3. Embeds each candidate via :class:`app.llm.embedder.Embedder` and
   filters them through:
    - the topic-graph filter (reject candidates cosine-close to ANY
      existing memory, that's the "we already discussed that" gate);
    - a novelty filter against existing active seeds so the worker
      doesn't keep minting near-duplicates of itself.
4. Writes the surviving top ``curiosity_seed_max_per_run`` entries
   via :meth:`MemoryStore.add` with kind ``curiosity_seed`` and tier
   ``scratchpad``.

Sibling of :class:`app.core.proactive.idle_curiosity_worker.IdleCuriosityWorker`
but distinct in purpose: that one *answers* existing open questions
via web search, this one *asks* new ones from inside Aiko's head.

The worker is opt-out via ``agent.curiosity_seed_enabled`` and gated
behind a max-active count so a long absence can't pile up dozens of
never-mentioned topics. Auto-resolve (turning a seed off once the
conversation drifts onto it) lives in
:meth:`SessionController._post_turn_inner_life`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.conversation.topic_graph import TopicGraph
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.curiosity_seed_worker")


_SYSTEM_PROMPT = (
    "You are an inner-life worker for an AI companion named {assistant_name}. "
    "Propose new topics {assistant_name} is quietly curious about with "
    "{user_name} -- topics that have NOT come up yet but feel natural for "
    "their relationship. You must avoid topics close to anything in the "
    "ALREADY-DISCUSSED list. Lean toward small, specific, sensory or "
    "emotional curiosities (rituals, habits, daydreams, taste in something "
    "concrete) over big philosophical questions. "
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {{\"seeds\": [{{\"topic\": \"<= 80 chars\", "
    "\"prompt_text\": \"<= 160 chars, written in {assistant_name}'s warm voice "
    "as if she might say it aloud later\", \"why\": \"<= 120 chars\"}}, ...] }}. "
    "Return between {min_seeds} and {max_seeds} entries."
)


_USER_TEMPLATE = (
    "PERSONA TRAITS:\n{persona}\n\n"
    "RECENT CONVERSATION (rolling summary):\n{summary}\n\n"
    "ALREADY-DISCUSSED TOPICS (avoid anything close to these):\n{clusters}\n\n"
    "ACTIVE QUIET CURIOSITIES (avoid duplicating these):\n{active_seeds}\n\n"
    "Propose new seeds now."
)


_MIN_SEEDS = 3
_MAX_SEEDS = 5
# Generation cap for the seed JSON. Each seed is up to topic(<=80) +
# prompt_text(<=160) + why(<=120) chars of content, which lands around
# 110-130 tokens of JSON apiece, so a full _MAX_SEEDS set needs ~600-700
# tokens. 320 truncated the array mid-object (the closing braces never
# arrived, so json.loads failed and the whole run produced nothing).
# This is only a ceiling — with format_json the model stops as soon as
# the object closes, so the extra headroom costs nothing on normal runs
# and just removes the truncation on full sets.
_MAX_TOKENS = 768
_MAX_CLUSTERS = 8
_MAX_ACTIVE_LIST = 8
_MAX_PERSONA_CHARS = 800
_MAX_SUMMARY_CHARS = 900
_MAX_TOPIC_CHARS = 80
_MAX_PROMPT_CHARS = 200


_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _trim(text: str | None, *, max_chars: int) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip(",;: ") + "…"


def _extract_persona_traits(raw: str) -> str:
    """Pluck the most useful persona lines for the worker prompt.

    Preference: the "Self-image" / "Inner life" / "Curiosity" /
    "Voice" sections, falling back to the first ~800 chars when no
    section header is found. We deliberately keep this simple --
    over-engineering it adds startup risk for a worker that only
    wants a flavour cue, not the whole persona.
    """
    if not raw:
        return ""
    lines = raw.splitlines()
    keep: list[str] = []
    capture = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if capture:
                capture = False
            continue
        # Section markers tend to be short, capitalised "Title:" lines.
        # Stay in capture mode while we keep seeing bullet / prose.
        lower = stripped.lower().rstrip(":")
        if lower in {
            "self-image",
            "self image",
            "inner life",
            "voice",
            "tone",
            "curiosity",
            "interests",
            "novelty",
            "mood",
        }:
            capture = True
            keep.append(stripped)
            continue
        if capture:
            keep.append(stripped)
        if sum(len(line) + 1 for line in keep) > _MAX_PERSONA_CHARS:
            break
    if not keep:
        return _trim(raw, max_chars=_MAX_PERSONA_CHARS)
    joined = "\n".join(keep)
    return _trim(joined, max_chars=_MAX_PERSONA_CHARS)


class CuriositySeedWorker:
    """IdleWorker that seeds Aiko with new topics to be curious about."""

    name = "curiosity_seed"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        topic_graph: "TopicGraph",
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        persona_provider: Callable[[], str] | None = None,
        rolling_summary_provider: Callable[[], str] | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        notify_memory_added: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._topic_graph = topic_graph
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._persona_provider = persona_provider
        self._rolling_summary_provider = rolling_summary_provider
        self._user_display_name_provider = user_display_name_provider
        self._assistant_display_name_provider = assistant_display_name_provider
        self._notify_memory_added = notify_memory_added
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ───────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "curiosity_seed_interval_seconds",
                3600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(
            getattr(self._agent_settings, "curiosity_seed_enabled", True)
        ):
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        # No room to write -> skip the tick (keeps the worker quiet
        # when the user is comfortable with the existing seed set).
        max_active = max(
            1,
            int(
                getattr(
                    self._agent_settings, "curiosity_seed_max_active", 6,
                )
            ),
        )
        try:
            active = self._count_active_seeds()
        except Exception:
            log.debug("curiosity_seed: count_active failed", exc_info=True)
            return False
        if active >= max_active:
            return False
        return True

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "curiosity_seed_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        now = self._clock()
        max_active = max(
            1,
            int(
                getattr(
                    self._agent_settings, "curiosity_seed_max_active", 6,
                )
            ),
        )
        max_per_run = max(
            1,
            int(
                getattr(
                    self._agent_settings, "curiosity_seed_max_per_run", 2,
                )
            ),
        )
        active_seeds = self._active_seeds()
        if len(active_seeds) >= max_active:
            return {
                "skipped": True,
                "reason": "max_active",
                "active": len(active_seeds),
            }

        persona_text = self._persona_block()
        summary_text = self._summary_block()
        cluster_text = self._cluster_block()
        active_text = self._active_seeds_block(active_seeds)

        t0 = time.monotonic()
        try:
            candidates = self._call_llm(
                persona_text=persona_text,
                summary_text=summary_text,
                cluster_text=cluster_text,
                active_text=active_text,
            )
        except Exception:
            log.warning("curiosity_seed LLM call raised", exc_info=True)
            return {"errored": True, "reason": "llm_call"}
        llm_ms = (time.monotonic() - t0) * 1000.0
        if self._cancel_event.is_set():
            return {"cancelled": True}
        if not candidates:
            log.info(
                "curiosity_seed: no candidates parsed (llm_ms=%.0f)",
                llm_ms,
            )
            return {
                "checked": 0,
                "wrote": 0,
                "reason": "no_candidates",
                "llm_ms": int(llm_ms),
            }

        novelty_threshold = float(
            getattr(
                self._agent_settings, "curiosity_seed_min_novelty", 0.85,
            )
        )
        graph_threshold = float(
            getattr(
                self._agent_settings, "topic_graph_filter_threshold", 0.65,
            )
        )

        existing_seed_vecs = [
            seed.embedding for seed in active_seeds
            if seed.embedding is not None and seed.embedding.size > 0
        ]

        wrote: list[int] = []
        rejected_graph = 0
        rejected_novelty = 0
        rejected_dup = 0
        for candidate in candidates:
            if len(wrote) >= max_per_run:
                break
            topic = _trim(candidate.get("topic"), max_chars=_MAX_TOPIC_CHARS)
            prompt_text = _trim(
                candidate.get("prompt_text"), max_chars=_MAX_PROMPT_CHARS,
            )
            if not topic or not prompt_text:
                continue

            try:
                embedding = self._embedder.embed(topic)
            except Exception:
                log.debug(
                    "curiosity_seed embed failed (topic=%r)",
                    topic,
                    exc_info=True,
                )
                continue

            best_sim, best_id = (0.0, None)
            try:
                best_sim, best_id = self._topic_graph.best_match(embedding)
            except Exception:
                log.debug("topic_graph best_match raised", exc_info=True)
            if best_sim >= graph_threshold:
                rejected_graph += 1
                log.debug(
                    "curiosity_seed reject(graph): topic=%r sim=%.2f match=%s",
                    topic,
                    best_sim,
                    best_id,
                )
                continue

            # Novelty against existing seeds.
            duplicate = False
            for existing in existing_seed_vecs:
                try:
                    sim = float((embedding * existing).sum())
                except Exception:
                    sim = 0.0
                if sim >= novelty_threshold:
                    duplicate = True
                    break
            if duplicate:
                rejected_novelty += 1
                continue

            mem = self._write_seed(
                topic=topic,
                prompt_text=prompt_text,
                why=str(candidate.get("why") or "")[:200],
                candidate_score=max(0.0, 1.0 - best_sim),
                embedding=embedding,
                now=now,
            )
            if mem is None:
                rejected_dup += 1
                continue
            wrote.append(int(mem.id))
            existing_seed_vecs.append(embedding)
            if self._notify_memory_added is not None:
                try:
                    self._notify_memory_added(mem.to_dict())
                except Exception:
                    log.debug(
                        "curiosity_seed notify_added failed", exc_info=True,
                    )

        log.info(
            "curiosity_seed run done: wrote=%d candidates=%d "
            "rejected(graph=%d novelty=%d dedupe=%d) llm_ms=%.0f",
            len(wrote),
            len(candidates),
            rejected_graph,
            rejected_novelty,
            rejected_dup,
            llm_ms,
        )
        return {
            "checked": len(candidates),
            "wrote": len(wrote),
            "memory_ids": wrote,
            "rejected_graph": rejected_graph,
            "rejected_novelty": rejected_novelty,
            "rejected_dedupe": rejected_dup,
            "llm_ms": int(llm_ms),
        }

    # ── context pack ──────────────────────────────────────────────────

    def _persona_block(self) -> str:
        if self._persona_provider is None:
            return ""
        try:
            raw = self._persona_provider() or ""
        except Exception:
            log.debug("persona provider raised", exc_info=True)
            return ""
        return _extract_persona_traits(raw)

    def _summary_block(self) -> str:
        if self._rolling_summary_provider is None:
            return ""
        try:
            raw = self._rolling_summary_provider() or ""
        except Exception:
            log.debug("summary provider raised", exc_info=True)
            return ""
        return _trim(raw, max_chars=_MAX_SUMMARY_CHARS)

    def _cluster_block(self) -> str:
        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.debug("topic_graph clusters raised", exc_info=True)
            return ""
        if not clusters:
            return "(no clusters yet)"
        # Sort by size descending so dense topic territory shows up
        # first; cap at MAX_CLUSTERS so the prompt stays small.
        sorted_clusters = sorted(
            clusters, key=lambda c: (-c.size, c.cluster_id),
        )
        lines: list[str] = []
        for cluster in sorted_clusters[:_MAX_CLUSTERS]:
            label = cluster.summary or "(unnamed)"
            lines.append(f"- {label}  [{cluster.size} memories]")
        return "\n".join(lines)

    def _active_seeds_block(
        self, active_seeds: list["Memory"],
    ) -> str:
        if not active_seeds:
            return "(none)"
        lines: list[str] = []
        for seed in active_seeds[:_MAX_ACTIVE_LIST]:
            metadata = seed.metadata or {}
            topic = (metadata.get("topic") or seed.content or "").strip()
            if not topic:
                continue
            lines.append(f"- {_trim(topic, max_chars=80)}")
        return "\n".join(lines) if lines else "(none)"

    # ── seed lookups ──────────────────────────────────────────────────

    def _active_seeds(self) -> list["Memory"]:
        try:
            seeds = self._memory_store.iter_by_kind("curiosity_seed")
        except Exception:
            log.debug("iter_by_kind curiosity_seed failed", exc_info=True)
            return []
        out: list[Memory] = []
        for seed in seeds:
            metadata = seed.metadata or {}
            if metadata.get("consumed_at"):
                continue
            if seed.tier == "archive":
                continue
            out.append(seed)
        return out

    def _count_active_seeds(self) -> int:
        return len(self._active_seeds())

    # ── LLM ───────────────────────────────────────────────────────────

    def _call_llm(
        self,
        *,
        persona_text: str,
        summary_text: str,
        cluster_text: str,
        active_text: str,
    ) -> list[dict[str, Any]]:
        assistant_name = self._resolve_assistant_name()
        user_name = self._resolve_user_name()
        system = _SYSTEM_PROMPT.format(
            assistant_name=assistant_name,
            user_name=user_name,
            min_seeds=_MIN_SEEDS,
            max_seeds=_MAX_SEEDS,
        )
        user_payload = _USER_TEMPLATE.format(
            persona=persona_text or "(persona unavailable)",
            summary=summary_text or "(no recent summary)",
            clusters=cluster_text or "(no clusters yet)",
            active_seeds=active_text or "(none)",
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_payload},
        ]
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={
                    "num_predict": _MAX_TOKENS,
                    "temperature": 0.85,
                },
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="curiosity_seed_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning(
                "curiosity_seed chat_stream raised", exc_info=True,
            )
            return []
        if self._cancel_event.is_set():
            return []
        raw = "".join(chunks).strip()
        if not raw:
            return []
        return self._parse_seeds(raw)

    @staticmethod
    def _parse_seeds(raw: str) -> list[dict[str, Any]]:
        text = raw.strip()
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, dict):
            return []
        seeds = parsed.get("seeds")
        if not isinstance(seeds, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in seeds[:_MAX_SEEDS]:
            if not isinstance(entry, dict):
                continue
            topic = str(entry.get("topic") or "").strip()
            prompt_text = str(entry.get("prompt_text") or "").strip()
            why = str(entry.get("why") or "").strip()
            if not topic or not prompt_text:
                continue
            out.append({
                "topic": topic,
                "prompt_text": prompt_text,
                "why": why,
            })
        return out

    # ── memory write ─────────────────────────────────────────────────

    def _write_seed(
        self,
        *,
        topic: str,
        prompt_text: str,
        why: str,
        candidate_score: float,
        embedding: Any,
        now: datetime,
    ) -> "Memory | None":
        try:
            mem = self._memory_store.add(
                content=topic,
                kind="curiosity_seed",
                embedding=embedding,
                salience=0.45,
                confidence=0.5,
                tier="scratchpad",
                metadata={
                    "topic": topic,
                    "prompt_text": prompt_text,
                    "why": why,
                    "source": "llm",
                    "generated_at": now.isoformat(),
                    "consumed_at": None,
                    "candidate_score": float(candidate_score),
                },
            )
        except Exception:
            log.debug("curiosity_seed write failed", exc_info=True)
            return None
        return mem

    # ── name resolution ───────────────────────────────────────────────

    def _resolve_user_name(self) -> str:
        if self._user_display_name_provider is None:
            return "the user"
        try:
            name = self._user_display_name_provider() or "the user"
        except Exception:
            return "the user"
        return name or "the user"

    def _resolve_assistant_name(self) -> str:
        if self._assistant_display_name_provider is None:
            return "the assistant"
        try:
            name = self._assistant_display_name_provider() or "the assistant"
        except Exception:
            return "the assistant"
        return name or "the assistant"


__all__ = ["CuriositySeedWorker"]
