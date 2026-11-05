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
4. Writes the surviving top ``curiosity_seed_max_per_run`` entries into
   :class:`~app.core.proactive.cue_store.CueStore` as ``curiosity_seed``
   cues.

Sibling of :class:`app.core.proactive.idle_curiosity_worker.IdleCuriosityWorker`
but distinct in purpose: that one *answers* existing open questions
via web search, this one *asks* new ones from inside Aiko's head.

Seeds used to live in the ``memories`` table, which gave them three
behaviours nobody chose: incidental RAG retrieval (a seed could surface
in T3 as a plain memory bullet), topic-graph clustering into the graph
they were derived from, and the scratchpad TTL / promotion lifecycle.
A seed is not something Aiko remembers -- it is something she has not
said yet -- so it belongs in the cue pool with the other six, and all
three of those stop.

K87 split the output in two. The prompt used to ask for topics she is
curious about *with* the user, so every seed was bond-scoped and the
whole shelf could only be discharged by asking him something. Seeds now
carry an ``about`` label -- ``subject`` for a curiosity about a thing
she would still have alone, ``user`` for the bond-scoped kind -- and
``agent.curiosity_subject_quota`` is enforced against the standing pool
rather than merely requested in the prompt, because a prompt-only quota
is satisfied by relabelling.

The worker is opt-out via ``agent.curiosity_seed_enabled`` and paced by
inventory: ``curiosity_seed_max_active`` is the pool's target stock, and
a full shelf reports no demand rather than being caught by a cap after
the fact. Consumption (retiring a seed once the conversation drifts onto
it) is the pool's ``either_party`` fulfilment, in
:mod:`app.core.session.cue_pool_mixin`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.cue_producer import CueProducer, StoreProvider
from app.core.proactive.curiosity_subject import (
    MODE_PERSON,
    MODE_SUBJECT,
    deficit,
    is_person_directed,
)
from app.core.proactive.idle_worker import WorkSignal
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.proactive.cue_store import CueRow
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.curiosity_seed_worker")


_SYSTEM_PROMPT = (
    "You are an inner-life worker for {assistant_name}. Propose zero to two "
    "specific curiosities worth sharing. A new angle can grow from a familiar "
    "interest; do not jump to unrelated subjects just to be novel. Curiosity "
    "may be playful or about a thing she likes, not only practical work. Name "
    "the connection to the supplied conversation, interests or persona and "
    "what exploring it could add. Do not invent possessions, observations or "
    "hidden motives. Do not restate familiar affection or ask a broad survey. "
    "A lull creates room, not a reason to ask a random question. Zero is normal.\n"
    "Each seed has about=user (a concrete curiosity about {user_name}) or "
    "about=subject (something she would enjoy exploring for its own sake). "
    "When several ideas genuinely fit, include a subject idea; never manufacture "
    "one to fill a quota. Existing topics are a baseline, not forbidden ground: "
    "a useful new angle is welcome, repeating an answered question is not.\n"
    "Reply with ONE JSON object {{\"seeds\": [...]}}. Each entry has topic "
    "(<=80 chars), prompt_text (one specific possible line in her voice, <=200 "
    "chars), why (the actual connection to offered context, 8-240 chars), "
    "useful_because (what this could add, 8-240 chars), inspiration (conversation, "
    "interest or persona), about (user or subject), and optional new_angle "
    "(what remains open within a familiar topic, <=240 chars). Empty output is "
    "better than an unmotivated, generic or already answered curiosity."
)


_USER_TEMPLATE = (
    "PERSONA TRAITS:\n{persona}\n\n"
    "CONVERSATION SUMMARY (historical context; age not supplied, not live state):\n{summary}\n\n"
    "FAMILIAR TOPICS (avoid repetition, allow a concrete new angle):\n{clusters}\n\n"
    "ACTIVE QUIET CURIOSITIES (avoid duplicating these):\n{active_seeds}\n\n"
    "Propose new seeds now."
)


_MIN_SEEDS = 0
_MAX_SEEDS = 5
# Generation cap for the seed JSON. Each seed is up to topic(<=80) +
# prompt_text(<=160) + why(<=120) chars of content, which lands around
# 110-130 tokens of JSON apiece, so a full _MAX_SEEDS set needs ~600-700
# tokens. 320 truncated the array mid-object (the closing braces never
# arrived, so json.loads failed and the whole run produced nothing).
# This is only a ceiling — with format_json the model stops as soon as
# the object closes, so the extra headroom costs nothing on normal runs
# and just removes the truncation on full sets.
_MAX_TOKENS = 1100
_MAX_CLUSTERS = 8
_MAX_ACTIVE_LIST = 8
_MAX_PERSONA_CHARS = 800
_MAX_SUMMARY_CHARS = 900
_MAX_TOPIC_CHARS = 80
_MAX_PROMPT_CHARS = 200


_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return timephrase.utcnow()


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
        cue_store_provider: StoreProvider,
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
        notify_cue_added: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cues = CueProducer(
            "curiosity_seed",
            cue_store_provider,
            inventory_target=max(
                1,
                int(
                    getattr(agent_settings, "curiosity_seed_max_active", 6)
                ),
            ),
        )
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
        self._notify_cue_added = notify_cue_added
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
        return bool(
            getattr(self._agent_settings, "curiosity_seed_enabled", True)
        )

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Pressure from the shortfall against ``curiosity_seed_max_active``.

        That key used to be a ceiling checked after the fact; here it is
        the target stock, which is the same number doing the job it was
        always describing. A comfortable seed set means no demand and the
        worker -- which makes a real LLM call -- is simply not admitted.
        """
        if not bool(
            getattr(self._agent_settings, "curiosity_seed_enabled", True)
        ):
            return WorkSignal(pressure=0.0, reason="disabled")
        from app.core.proactive.cue_quality import inventory_demand, seed_usable

        return inventory_demand(self._cues, seed_usable, needs_llm=True)

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "curiosity_seed_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        now = self._clock()
        max_active = self._cues.inventory_target
        max_per_run = max(
            1,
            int(
                getattr(
                    self._agent_settings, "curiosity_seed_max_per_run", 2,
                )
            ),
        )
        # Earlier seeds did not retain a reason to raise them. Keep their
        # receipts, but do not let them fill the inventory for the new contract.
        store = self._cues.store()
        if store is not None:
            for old in self._cues.stock_rows(limit=200):
                from app.core.proactive.cue_quality import seed_usable

                if not seed_usable(old.payload):
                    store.expire(old.id, evidence="unreviewed_seed_motivation")
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
            if seed.embedding is not None and len(seed.embedding) > 0
        ]

        # K87: order the batch so the subject shortfall against the
        # standing pool is served first. The prompt asks for subject
        # seeds; this is what makes them actually get written, since a
        # ``max_per_run`` of 2 otherwise just takes whatever the model
        # happened to list first.
        user_name = self._resolve_user_name()
        modes = {
            id(c): self._classify(c, user_name) for c in candidates
        }
        candidates = self._order_by_quota(
            candidates, modes, stock=active_seeds, batch=max_per_run,
        )

        wrote: list[int] = []
        wrote_subject = 0
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
            if best_sim >= graph_threshold and not candidate.get("new_angle"):
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

            about = modes.get(id(candidate), MODE_PERSON)
            cue_id = self._write_seed(
                topic=topic,
                prompt_text=prompt_text,
                why=str(candidate.get("why") or "")[:200],
                candidate_score=max(0.0, 1.0 - best_sim),
                embedding=embedding,
                now=now,
                about=about,
                quality={key: candidate.get(key, "") for key in
                         ("inspiration", "useful_because", "new_angle")},
            )
            if not cue_id:
                rejected_dup += 1
                continue
            wrote.append(cue_id)
            if about == MODE_SUBJECT:
                wrote_subject += 1
            existing_seed_vecs.append(embedding)
            self._notify(cue_id, topic=topic, prompt_text=prompt_text)

        log.info(
            "curiosity_seed run done: wrote=%d (subject=%d) candidates=%d "
            "rejected(graph=%d novelty=%d dedupe=%d) llm_ms=%.0f",
            len(wrote),
            wrote_subject,
            len(candidates),
            rejected_graph,
            rejected_novelty,
            rejected_dup,
            llm_ms,
        )
        return {
            "checked": len(candidates),
            "wrote": len(wrote),
            "wrote_subject": wrote_subject,
            "cue_ids": wrote,
            "rejected_graph": rejected_graph,
            "rejected_novelty": rejected_novelty,
            "rejected_dedupe": rejected_dup,
            "llm_ms": int(llm_ms),
        }

    # ── K87 quota ─────────────────────────────────────────────────────

    def _subject_quota(self) -> float:
        return min(
            1.0,
            max(
                0.0,
                float(
                    getattr(
                        self._agent_settings, "curiosity_subject_quota", 0.4,
                    )
                ),
            ),
        )

    def _stock_modes(self, stock: list["CueRow"]) -> list[str]:
        """The subject/person split of the seeds already in the pool.

        Seeds written before K87 carry no ``about`` in their payload;
        they were all bond-scoped by construction, so reading them as
        person-mode is the accurate answer rather than a default.
        """
        out: list[str] = []
        for row in stock:
            payload = getattr(row, "payload", None) or {}
            value = str(payload.get("about") or "") if isinstance(
                payload, dict,
            ) else ""
            out.append(MODE_SUBJECT if value == MODE_SUBJECT else MODE_PERSON)
        return out

    def _order_by_quota(
        self,
        candidates: list[dict[str, Any]],
        modes: dict[int, str],
        *,
        stock: list["CueRow"],
        batch: int,
    ) -> list[dict[str, Any]]:
        """Front-load as many subject seeds as the shortfall calls for.

        A reorder rather than a filter: everything the model proposed
        stays available, because the write loop drops candidates for
        novelty and graph overlap and a filtered batch could leave the
        run writing nothing at all.
        """
        need = deficit(
            self._stock_modes(stock), quota=self._subject_quota(), total=batch,
        )
        if need <= 0:
            return candidates
        head: list[dict[str, Any]] = []
        tail: list[dict[str, Any]] = []
        for candidate in candidates:
            if len(head) < need and modes.get(id(candidate)) == MODE_SUBJECT:
                head.append(candidate)
            else:
                tail.append(candidate)
        return head + tail

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
        self, active_seeds: list["CueRow"],
    ) -> str:
        if not active_seeds:
            return "(none)"
        lines: list[str] = []
        for seed in active_seeds[:_MAX_ACTIVE_LIST]:
            topic = (seed.subject or "").strip()
            if not topic:
                continue
            lines.append(f"- {_trim(topic, max_chars=80)}")
        return "\n".join(lines) if lines else "(none)"

    # ── seed lookups ──────────────────────────────────────────────────

    def _active_seeds(self) -> list["CueRow"]:
        return self._cues.stock_rows(with_embedding=True)

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
            min_subject=max(1, round(self._subject_quota() * _MAX_SEEDS)),
        )
        system = (timephrase.today_anchor(self._clock()) + "\n\n" + system
                  + "\n\n" + timephrase.STORED_TEXT_TIME_RULE)
        placeholders = {"(none)", "(no recent summary)", "(no clusters yet)",
                        "(persona unavailable)"}
        available = {key for key, value in (("persona", persona_text),
                     ("conversation", summary_text), ("interest", cluster_text))
                     if value and value.strip() not in placeholders}
        if not available:
            return []
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
        return [seed for seed in self._parse_seeds(raw) if seed["inspiration"] in available]

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
            caps = {"topic": 80, "prompt_text": 200, "why": 240, "useful_because": 240}
            if any(not isinstance(entry.get(k), str) or not 8 <= len(entry[k].strip()) <= cap
                   or timephrase.has_relative_deictic(entry[k]) for k, cap in caps.items()):
                continue
            if (not isinstance(entry.get("inspiration"), str)
                    or entry["inspiration"] not in {"conversation", "interest", "persona"}):
                continue
            angle = entry.get("new_angle", "")
            if (not isinstance(angle, str) or len(angle) > 240
                    or (angle and len(angle.strip()) < 12)
                    or timephrase.has_relative_deictic(angle)):
                continue
            out.append({**{k: entry[k].strip() for k in caps},
                        "about": entry.get("about", ""),
                        "inspiration": entry["inspiration"], "new_angle": angle.strip()})
        return out

    def _classify(self, candidate: dict[str, Any], user_name: str) -> str:
        """Which side of the K87 quota does this seed fall on?

        The model's own ``about`` label is taken as the claim and the
        text as the evidence: a seed labelled ``subject`` that reads as
        a question about him is filed as ``user`` anyway. Without that
        check the quota would be satisfied by relabelling rather than by
        producing different seeds, which is the failure mode of every
        prompt-only quota.
        """
        text = f"{candidate.get('topic', '')} {candidate.get('prompt_text', '')}"
        if is_person_directed(text, user_name):
            return MODE_PERSON
        claimed = str(candidate.get("about") or "")
        # A missing label falls through to the text, which is the honest
        # reading: an older model that ignores the new field still gets
        # classified by what its seed actually says.
        return MODE_PERSON if claimed in ("user", MODE_PERSON) else MODE_SUBJECT

    # ── pool write ───────────────────────────────────────────────────

    def _write_seed(
        self,
        *,
        topic: str,
        prompt_text: str,
        why: str,
        candidate_score: float,
        embedding: Any,
        now: datetime,
        about: str = MODE_PERSON,
        quality: dict[str, Any] | None = None,
    ) -> int:
        """Queue one seed in the pool. Returns the row id, or 0.

        ``prompt_text`` -- the LLM's phrasing of the question -- rides in
        the payload rather than in ``text``, because the seeds block lists
        bare topics and the narrative weaver wants the sentence. Both
        readers get what they need without either re-deriving it.
        """
        try:
            return self._cues.publish(
                topic,
                topic,
                payload={
                    "prompt_text": prompt_text,
                    "why": why,
                    "quality": {"version": 1, **(quality or {})},
                    "source": "llm",
                    "about": about,
                    "generated_at": now.isoformat(),
                    "candidate_score": float(candidate_score),
                },
                embedding=embedding,
            )
        except Exception:
            log.debug("curiosity_seed write failed", exc_info=True)
            return 0

    def _notify(self, cue_id: int, *, topic: str, prompt_text: str) -> None:
        if self._notify_cue_added is None:
            return
        try:
            self._notify_cue_added({
                "id": cue_id,
                "cue_type": "curiosity_seed",
                "subject": topic,
                "text": topic,
                "prompt_text": prompt_text,
                "state": "pending",
            })
        except Exception:
            log.debug("curiosity_seed notify_added failed", exc_info=True)

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
