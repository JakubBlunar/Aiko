"""Connect a standing project to the existing bounded curiosity researcher."""
from __future__ import annotations

import logging
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.infra import timephrase
from app.core.memory.fact_check_privacy import scrub_claim_for_search

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.chat_client import ChatClient

log = logging.getLogger("app.pursuit_research")


class PursuitResearch:
    def __init__(
        self, *, store: "MemoryStore", embed: Callable[[str], Any],
        client: "ChatClient", model: str, enabled: Callable[[], bool],
        user_name: Callable[[], str],
    ) -> None:
        self._store = store
        self._embed = embed
        self._client = client
        self._model = model
        self._enabled = enabled
        self._user_name = user_name

    @staticmethod
    def matches(memory: "Memory", state: dict[str, Any]) -> bool:
        metadata = memory.metadata or {}
        return (
            metadata.get("hobby_key") == state.get("key")
            and metadata.get("hobby_started_at") == state.get("started_at")
        )

    def result(self, state: dict[str, Any]) -> "Memory | None":
        applied = set(state.get("research_result_ids") or [])
        return next((
            memory for memory in sorted(
                self._store.iter_by_kind("curiosity_finding"), key=lambda row: row.id,
            )
            if self.matches(memory, state) and memory.id not in applied
            and (memory.metadata or {}).get("research_evidence")
        ), None)

    def request(self, state: dict[str, Any], now: datetime) -> int | None:
        if not self._enabled():
            return None
        for question in self._store.iter_by_kind("open_question"):
            if self.matches(question, state) and not (
                question.metadata or {}
            ).get("curiosity_resolved_at"):
                return int(question.id)
        ids = state.get("research_result_ids") or []
        result = self._store.get(ids[-1]) if ids else None
        latest = timephrase.format_memory_block([result], now) if result is not None else "(none)"
        prompt = (
            timephrase.today_anchor()
            + "\n" + timephrase.STORED_TEXT_TIME_RULE
            + "\nChoose ONE small factual question about the public subject of this "
            "AI companion's personal project. This will be sent to a public search "
            "engine. Do not include private names, relationships, feelings, dates or "
            "events. Preserve the specific subject, not a vague substitute. Project "
            "descriptions are ideas, NOT evidence: test questionable premises rather "
            "than asserting them. Do not invent a lesson, measurement, book contents "
            "or completed experience. If there is no public subject, decline. "
            'Return JSON {"researchable": bool, "question": "short factual question"}.\n'
            f"Project: {str(state.get('artifact') or state.get('label') or '')[:200]}\n"
            f"Description (unverified): {str(state.get('artifact_detail') or '')[:200]}\n"
            f"Latest recorded result:\n{latest[:600]}"
        )
        content, _ = self._client.chat_json(
            [{"role": "user", "content": prompt}], model=self._model,
            options={"temperature": 0.3, "num_predict": 160},
            format_json=True, surface="pursuit_research",
        )
        raw = json.loads(content) if isinstance(content, str) else content
        if not isinstance(raw, dict) or raw.get("researchable") is not True:
            log.info("pursuit research declined: key=%s", state.get("key"))
            return None
        question = raw.get("question")
        if not isinstance(question, str) or not 8 <= len(question.strip()) <= 200:
            raise ValueError("pursuit researcher returned an invalid question")
        safe = scrub_claim_for_search(
            question, user_names=[self._user_name()], assistant_name="Aiko",
        )
        if not safe:
            log.info("pursuit research blocked by privacy: key=%s", state.get("key"))
            return None
        memory = self._store.add(
            content=safe, kind="open_question", embedding=self._embed(safe),
            salience=0.65, tier="long_term", skip_dedupe=True,
            metadata={
                "curiosity_purpose": "research", "already_neutral": True,
                "research_origin": "pursuit", "hobby_key": state.get("key"),
                "hobby_started_at": state.get("started_at"),
                "requested_at": now.isoformat(),
            },
        )
        if memory is None:
            raise RuntimeError("pursuit research question was not persisted")
        return int(memory.id)

    def write_diary(self, state: dict[str, Any], now: datetime) -> int | None:
        ids = state.get("research_result_ids") or []
        if not ids:
            return None
        result = self._store.get(ids[-1])
        if result is None:
            raise ValueError("research diary source no longer exists")
        for entry in self._store.iter_by_kind("diary"):
            if entry.metadata.get("research_memory_id") == result.id:
                return int(entry.id)
        query = str(result.metadata.get("source_query") or state.get("artifact") or "")[:200]
        text = (
            f"I searched about {query}. The excerpts suggested: {result.content}. "
            "I have kept the sources so I can come back to this."
        )
        if timephrase.has_relative_deictic(text):
            text = (
                f"I kept research entry {result.id} with its dated finding and search "
                "excerpts so I can come back to the evidence."
            )
        entry = self._store.add(
            content=text, kind="diary", embedding=self._embed(text),
            tier="long_term", temporal_type="past_event", event_time=now.isoformat(),
            skip_dedupe=True, metadata={
                "research_memory_id": result.id, "hobby_key": state.get("key"),
                "hobby_started_at": state.get("started_at"),
            },
        )
        if entry is None:
            raise RuntimeError("research diary entry was not persisted")
        return int(entry.id)
