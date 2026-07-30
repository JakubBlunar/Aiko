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
            "qwen3.5:9b",
            "jaahas/qwen3.5-uncensored:9b",
            "qwen3.6:27b",
        ],
        "env_hint": "",
        "api_key_required": False,
        "free_tier": "Unlimited (runs on your machine)",
        "docs_url": "https://ollama.com",
        # Deliberately *not* None (which means "ask /api/show for the
        # model's maximum"): recent Qwen tags advertise 256 k, and a KV
        # cache that size spills off any consumer GPU. 64 k is the
        # largest window a 9B model fits alongside its weights in 12 GB.
        "default_context_window": 65_536,
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
        "default_context_window": None,
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "provider": "openai_compatible",
        "base_url": (
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        ),
        # Flash-Lite first: this app's spend is almost entirely *input*
        # (a large stable prompt against a handful of reply tokens), and
        # the Lite tags are the cheapest current-generation input rate
        # Google sells — 3.1 Flash-Lite matches gpt-5-mini's $0.25/M and
        # $0.025/M cached exactly. 3.5 Flash-Lite costs a little more for
        # roughly double the throughput; 3.6 Flash is the step up in
        # capability at 6x the input price.
        "recommended_models": [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3.6-flash",
            "gemini-2.5-flash-lite",
        ],
        "env_hint": "GEMINI_API_KEY",
        "api_key_required": True,
        "free_tier": "Free tier available (rate-limited)",
        "docs_url": "https://ai.google.dev",
        # 128 k cap from the 1 M native window — see
        # ``_CONTEXT_WINDOW_TABLE`` in ``openai_compatible_client.py``.
        "default_context_window": 131_072,
        # Gemini has no /v1/responses surface, so pin the compat
        # endpoint rather than relying on the model-name auto-detection
        # happening to not match a Gemini tag.
        "api_style": "chat_completions",
        # Every Gemini 3 tag reasons by default (``medium`` on 3.6 Flash,
        # ``high`` on 3.1 Pro) and thinking tokens bill at the output
        # rate, so an unset effort is the expensive, slow option. ``low``
        # is the cheapest value the whole family accepts — only the 2.5
        # line can switch thinking off outright.
        "default_reasoning_effort": "low",
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
        "default_context_window": 131_072,
    },
    {
        "id": "xai",
        "label": "xAI (Grok)",
        "provider": "openai_compatible",
        "base_url": "https://api.x.ai/v1",
        # Grok reasons by default and can't be disabled; low/medium/high
        # are the valid efforts. grok-4.5 is the flagship; grok-4 and the
        # cheaper grok-3-mini round out the shortlist. The live
        # ``/v1/models`` response still fills the dropdown with the rest.
        "recommended_models": [
            "grok-4.5",
            "grok-4.3",
            "grok-3-mini",
        ],
        "env_hint": "XAI_API_KEY",
        "api_key_required": True,
        "free_tier": "Paid (no free tier)",
        "docs_url": "https://docs.x.ai",
        "default_context_window": 131_072,
        # xAI recommends the Responses API for new integrations, and it's
        # where Grok's reasoning-effort + prompt caching live — so force
        # the Responses surface rather than the legacy chat-completions
        # one (see LlmProvider.api_style / OpenAICompatibleClient).
        "api_style": "responses",
        # A safe latency-friendly default for background/worker roles; the
        # user can bump it per-route. Empty would resolve to "omit" for a
        # forced-responses provider, which is also fine.
        "default_reasoning_effort": "low",
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
        "default_context_window": 131_072,
    },
)
