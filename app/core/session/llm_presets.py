"""Curated LLM provider preset catalogue.

Extracted from :mod:`app.core.session.session_controller` into a leaf
module so the ``llm_settings_mixin`` can read ``_PROVIDER_PRESETS``
without importing the controller (which would be circular). Exposed
verbatim via ``GET /api/llm/presets`` so the React drawer can render
self-documenting cards without re-encoding these strings on the client.
The ``free_tier`` label is intentionally vague (rate limits move around
quarterly); the goal is to give users a hint, not to enforce a quota.
"""
from __future__ import annotations

from typing import Any


_PROVIDER_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "ollama",
        "label": "Local Ollama",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "recommended_models": [
            "llama3.1:8b",
            "qwen2.5:7b",
            "jaahas/qwen3.5-uncensored:9b",
        ],
        "env_hint": "",
        "api_key_required": False,
        "free_tier": "Unlimited (runs on your machine)",
        "docs_url": "https://ollama.com",
        "default_workers_use_local": False,
        # ``None`` -> auto-detect via Ollama's ``/api/show`` per model.
        "default_context_window": None,
    },
    {
        "id": "ollama_cloud",
        "label": "Ollama Cloud",
        "provider": "ollama",
        "base_url": "https://ollama.com",
        "recommended_models": [
            "llama3.1:70b",
            "qwen2.5:72b",
        ],
        "env_hint": "OLLAMA_API_KEY",
        "api_key_required": True,
        "free_tier": "Paid plan required",
        "docs_url": "https://ollama.com/cloud",
        "default_workers_use_local": True,
        "default_context_window": None,
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "provider": "openai_compatible",
        "base_url": (
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        ),
        "recommended_models": [
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ],
        "env_hint": "GEMINI_API_KEY",
        "api_key_required": True,
        "free_tier": "Free tier: ~15 req/min, ~1500 req/day",
        "docs_url": "https://ai.google.dev",
        "default_workers_use_local": True,
        # 128 k cap from 1-2 M native — see ``_CONTEXT_WINDOW_TABLE``
        # in ``openai_compatible_client.py`` for the rationale.
        "default_context_window": 131_072,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        # GPT-5 (Aug 2025+) is the default chat suggestion — newer
        # architecture, ~40 % cheaper than 4.1-mini on cached input,
        # 400 k native context. The four-model shortlist matches
        # the user's evaluation set (gpt-5-mini for chat,
        # gpt-5-nano for cheap workers, 4.1 family as fallback).
        # Pricier flagship variants (gpt-5, gpt-5.4-pro, …) still
        # appear in the dropdown via the live ``/v1/models`` response.
        "recommended_models": [
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
        ],
        "env_hint": "OPENAI_API_KEY",
        "api_key_required": True,
        "free_tier": "Paid (no free tier)",
        "docs_url": "https://platform.openai.com",
        "default_workers_use_local": True,
        "default_context_window": 131_072,
    },
    {
        "id": "groq",
        "label": "Groq",
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "recommended_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ],
        "env_hint": "GROQ_API_KEY",
        "api_key_required": True,
        "free_tier": "Free tier: 30 req/min",
        "docs_url": "https://console.groq.com",
        "default_workers_use_local": True,
        "default_context_window": 131_072,
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "recommended_models": [
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o-mini",
            "google/gemini-2.5-flash",
        ],
        "env_hint": "OPENROUTER_API_KEY",
        "api_key_required": True,
        "free_tier": "Pay-per-token (some models free)",
        "docs_url": "https://openrouter.ai/docs",
        "default_workers_use_local": True,
        "default_context_window": 131_072,
    },
)
