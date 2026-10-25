"""Read-only account usage surface; keys stay on the backend."""
from fastapi import HTTPException, Query

from app.llm.provider_usage import ProviderUsageService, UsageUnavailable


def register(app, session) -> None:
    service = ProviderUsageService()

    @app.get("/api/llm/providers/{provider_id}/usage")
    def provider_usage(provider_id: str, days: int = Query(default=7)) -> dict:
        if days not in (1, 7, 14, 30):
            raise HTTPException(400, "Choose 1, 7, 14 or 30 days.")
        provider = session._find_llm_provider(provider_id)
        if provider is None:
            raise HTTPException(404, "Unknown provider.")
        try:
            return service.fetch(provider, days)
        except UsageUnavailable as exc:
            raise HTTPException(502, str(exc)) from None
