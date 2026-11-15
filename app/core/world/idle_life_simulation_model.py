"""Explicitly requested local-model experiments; no connectors or writable memory store."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlparse

from app.llm.token_utils import estimate_tokens


class ModelBudgetExceeded(RuntimeError):
    pass


class BudgetedModel:
    """Hard call cap, estimated token preflight and usage-based stopping."""

    def __init__(self, client, *, max_calls=12, max_tokens=24000):
        self._client = client
        self.max_calls = max(0, min(100, int(max_calls)))
        self.max_tokens = max(0, min(1000000, int(max_tokens)))
        self.calls = 0
        self.tokens = 0
        self.exhausted = False

    def chat_json(self, messages, **kwargs):
        prompt = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        remaining = self.max_tokens - self.tokens - prompt
        if self.calls >= self.max_calls or remaining < 32:
            self.exhausted = True
            raise ModelBudgetExceeded("Simulation model budget exhausted")
        options = dict(kwargs.get("options") or {})
        options["num_predict"] = min(int(options.get("num_predict", 256)), remaining)
        kwargs["options"] = options
        kwargs["think"] = False  # keep the explicit generation allowance meaningful.
        self.calls += 1
        try:
            content, usage = self._client.chat_json(messages, **kwargs)
        except Exception:
            self.tokens += prompt  # the request may already have consumed input work.
            raise
        self.tokens += max(prompt, int(getattr(usage, "prompt_tokens", 0) or 0))
        self.tokens += max(
            estimate_tokens(str(content)), int(getattr(usage, "completion_tokens", 0) or 0)
        )
        self.exhausted = self.calls >= self.max_calls or self.tokens >= self.max_tokens
        return content, usage

    def status(self):
        return {
            "calls": self.calls,
            "charged_tokens_actual_or_estimated": self.tokens,
            "max_calls": self.max_calls,
            "token_allowance": self.max_tokens,
            "exhausted": self.exhausted,
            "token_note": "Preflight uses estimates; actual tokenizer usage can differ.",
        }


class SnapshotMemories:
    """Only SELECTs against the disposable database; no LanceDB or embedding service."""

    def __init__(self, db):
        self._db = db

    def _rows(self, clause, args):
        cursor = self._db._get_conn().execute(
            "SELECT id,content,kind,source_session,source_message_id,created_at,event_time,"
            "temporal_type,relevance_until,metadata,confidence,provenance FROM memories " + clause,
            args,
        )
        names = [entry[0] for entry in cursor.description]
        rows = []
        for raw in cursor:
            row = dict(zip(names, raw, strict=True))
            row["metadata"] = json.loads(row["metadata"] or "{}")
            rows.append(SimpleNamespace(**row))
        return rows

    def get(self, memory_id):
        rows = self._rows("WHERE id=?", (int(memory_id),))
        return rows[0] if rows else None

    def list_recent(self, limit=64):
        return self._rows("ORDER BY pinned DESC,created_at DESC LIMIT ?", (max(1, min(64, limit)),))


def local_model(args):
    parsed = urlparse(args.ollama_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Model experiments require an explicit loopback Ollama endpoint")
    from app.core.infra.settings import OllamaSettings
    from app.llm.ollama_client import OllamaClient

    client = OllamaClient(
        OllamaSettings(
            base_url=args.ollama_url,
            chat_model=args.model,
            context_window=args.context_window,
            timeout=30,
        )
    )
    return BudgetedModel(client, max_calls=args.max_model_calls, max_tokens=args.max_model_tokens)


def context_provider(db, args):
    from app.core.concepts.concept_diets import DietTuning
    from app.core.concepts.concept_store import ConceptStore
    from app.core.concepts.concept_view import ConceptView
    from app.core.infra.memory_settings import MemorySettings
    from app.core.world.idle_life_context import IdleLifeContextProvider
    import random

    memories = SnapshotMemories(db)
    concepts = ConceptStore(db)
    concepts.load_all()
    view = ConceptView(
        concepts,
        memory_store=memories,
        kv_get=db.kv_get,
        tuning=DietTuning(context_window=args.context_window),
    )
    settings = MemorySettings()
    settings.idle_life_context_ratio = args.context_ratio
    row = db.execute_fetchone("SELECT session_id FROM messages ORDER BY id DESC LIMIT 1")
    session_id = args.session_id or (str(row[0]) if row else "default")
    return IdleLifeContextProvider(
        chat_db=db,
        session_provider=lambda: session_id,
        view_provider=lambda: view,
        memory_provider=lambda: memories,
        settings_provider=lambda: settings,
        context_window_provider=lambda: args.context_window,
        rng=random.Random(args.seed + 2),
    )
