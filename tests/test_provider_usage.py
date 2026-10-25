"""Billing-only statistics: no inference, credentials in responses, or live state."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from app.core.infra.settings import LlmProvider
from app.llm import provider_usage as usage
from app.web.rest import provider_usage_routes


def provider(url=usage.VENICE_BASE, key="test-secret"):
    return LlmProvider(id="venice", name="Venice", kind="openai_compatible",
                       base_url=url, api_key=key)


def analytics():
    return dict(byDate=[dict(date="2026-10-25", USD=0.03, DIEM=0.8)], byModel=[dict(
        modelName="DeepSeek", unitType="tokens", totalUnits=1200, totalUsd=0.03,
        totalDiem=0.8, breakdown=[dict(type="Input", units=300),
                                dict(type="Cache Read", units=600),
                                dict(type="Cache Write", units=100),
                                dict(type="Output", units=200)],
    )])


def ledger(request_id, date="2026-10-25", **extra):
    return dict(timestamp=date + "T12:00:00Z", sku="deepseek-llm-input-mtoken",
                inferenceDetails=dict(requestId=request_id, promptTokens=1000,
                                      completionTokens=200),
                **extra)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(usage.timephrase, "utcnow", lambda: datetime(
        2026, 10, 25, 12, tzinfo=timezone.utc))


def fake_get(monkeypatch, pages=None):
    pages = iter(pages or [dict(data=[], nextCursor=None)])
    calls = []

    def get(http, path, params=None):
        calls.append((path, params))
        if path.endswith("usage-analytics"):
            return analytics()
        if path.endswith("balance"):
            return dict(balances=dict(usd=5, diem=7, bundledCredits=3, earnedCredits=2))
        if path == "/models":
            return dict(data=[dict(id="deepseek", model_spec=dict(name="DeepSeek", pricing=dict(
                input=dict(usd=0.3), output=dict(usd=1.2), cache_input=dict(usd=0.03),
            )))])
        assert path == "/billing/usage-history"
        return next(pages)

    monkeypatch.setattr(usage.ProviderUsageService, "_get", staticmethod(get))
    return calls


def test_cache_rate_is_weighted_and_write_tokens_are_input():
    body = analytics()
    body["byModel"].append(dict(modelName="Other", unitType="tokens", totalUnits=100,
                               totalUsd=0, totalDiem=0, breakdown=[
                                   dict(type="Input", units=100), dict(type="Output", units=0)]))
    body["byModel"].append(dict(modelName="Image", unitType="images", totalUnits=4))
    result = usage.normalize_analytics(body)
    assert len(result["models"]) == 2
    s = result["summary"]
    assert s["total_tokens"] == 1300
    assert s["input_tokens"] == 1100
    assert s["output_tokens"] == 200
    assert s["cached_tokens"] == 600
    assert s["cache_write_tokens"] == 100
    assert s["cache_hit_pct"] == pytest.approx(600 / 1100 * 100)
    assert s["usd"] == 0.03 and s["diem"] == 0.8


def test_unknown_token_splits_do_not_become_zero_cache_hits():
    body = analytics()
    del body["byModel"][0]["breakdown"]
    s = usage.normalize_analytics(body)["summary"]
    assert s["total_tokens"] == 1200
    assert s["cached_tokens"] is None and s["cache_hit_pct"] is None
    body["byModel"][0]["breakdown"] = [dict(type="New beta type", units=100)]
    assert usage.normalize_analytics(body)["summary"]["input_tokens"] is None


def test_zero_usage_and_invalid_schema_are_distinct():
    s = usage.normalize_analytics(dict(byModel=[], byDate=[]))["summary"]
    assert s["total_tokens"] == 0 and s["usd"] == 0
    assert s["cache_hit_pct"] is None
    with pytest.raises(usage.UsageUnavailable):
        usage.normalize_analytics({})
    with pytest.raises(usage.UsageUnavailable):
        usage.normalize_analytics(dict(byModel=[{}], byDate=[]))
    with pytest.raises(usage.UsageUnavailable):
        usage.normalize_prices(dict(data=None))


def test_pagination_deduplicates_requests_across_currencies_and_fills_days(monkeypatch, clock):
    calls = fake_get(monkeypatch, [dict(data=[ledger("one"), ledger("one", currency="DIEM")],
                                       nextCursor="page2"),
                                  dict(data=[ledger("one"), ledger("two", "2026-10-24")],
                                       nextCursor=None)])
    result = usage.ProviderUsageService().fetch(provider(), 7)
    assert result["summary"]["requests"] == 2
    assert result["requests_complete"] is True
    assert len(result["daily"]) == 7
    assert result["daily"][0]["requests"] == 0
    assert result["daily"][-1]["requests"] == 1
    history = [params for path, params in calls if path.endswith("usage-history")]
    assert history == [dict(startTimestamp="2026-10-19T00:00:00Z",
                            endTimestamp="2026-10-26T00:00:00Z", pageSize=1000),
                       dict(cursor="page2")]
    assert calls[0][1] == dict(startDate="2026-10-19", endDate="2026-10-25")
    assert result["prices"][0]["cache_input"] == 0.03
    assert result["prices"][0]["cache_write"] is None
    assert "test-secret" not in str(result)


def test_cache_expires_and_credential_changes_invalidate(monkeypatch, clock):
    ticks = [100.0]
    monkeypatch.setattr(usage.time, "monotonic", lambda: ticks[0])
    calls = fake_get(monkeypatch, [dict(data=[], nextCursor=None)] * 4)
    service = usage.ProviderUsageService()
    p = provider()
    first = service.fetch(p, 1)
    assert service.fetch(p, 1) is first and len(calls) == 4
    p.api_key = "new-secret"
    assert service.fetch(p, 1) is not first and len(calls) == 8
    ticks[0] += 61
    service.fetch(p, 1)
    assert len(calls) == 12


def test_missing_ids_and_repeated_cursors_produce_explicit_lower_bound(monkeypatch, clock):
    fake_get(monkeypatch, [dict(data=[ledger("one"), dict(sku="x-llm-input-mtoken")],
                               nextCursor="same"), dict(data=[], nextCursor="same")])
    result = usage.ProviderUsageService().fetch(provider(), 1)
    assert result["summary"]["requests"] == 1
    assert result["requests_complete"] is False
    assert result["warnings"]


def test_history_failure_preserves_analytics_and_unknown_count(monkeypatch, clock):
    fake_get(monkeypatch)
    original = usage.ProviderUsageService._get

    def get(http, path, params=None):
        if path.endswith("usage-history"):
            raise usage.UsageUnavailable("No history permission.")
        return original(http, path, params)

    monkeypatch.setattr(usage.ProviderUsageService, "_get", staticmethod(get))
    result = usage.ProviderUsageService().fetch(provider(), 7)
    assert result["summary"]["requests"] is None
    assert result["summary"]["total_tokens"] == 1200
    assert result["daily"][-1]["requests"] is None
    assert not result["requests_complete"]


@pytest.mark.parametrize("url", ["https://api.venice.ai.attacker.test/api/v1",
    "http://api.venice.ai/api/v1", "https://api.venice.ai/api/v1?key=secret",
    "https://user:secret@api.venice.ai/api/v1", "https://api.openai.com/v1"])
def test_unsupported_endpoints_never_receive_keys(url):
    assert not usage.supports_account_usage(provider(url))
    with pytest.raises(usage.UsageUnavailable, match="Venice only"):
        usage.ProviderUsageService().fetch(provider(url), 7)


def test_environment_key_and_missing_key(monkeypatch, clock):
    p = provider(key="")
    p.api_key_env = "AIKO_TEST_VENICE_KEY"
    monkeypatch.delenv(p.api_key_env, raising=False)
    with pytest.raises(usage.UsageUnavailable, match="Save a Venice API key"):
        usage.ProviderUsageService().fetch(p, 7)
    monkeypatch.setenv(p.api_key_env, "env-secret")
    fake_get(monkeypatch)
    assert usage.ProviderUsageService().fetch(p, 7)["provider"] == "venice"


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_failures_are_sanitized_and_redirects_disabled(status):
    http = MagicMock()
    http.get.return_value.__enter__.return_value.status_code = status
    with pytest.raises(usage.UsageUnavailable) as exc:
        usage.ProviderUsageService._get(http, "/billing/balance")
    assert "secret" not in str(exc.value)
    assert http.get.call_args.kwargs["allow_redirects"] is False


def test_rest_route_looks_up_saved_provider_and_validates(monkeypatch):
    # Call the registered sync endpoint directly: no event loop, live app, or GPU.
    app = FastAPI()
    p = provider()
    session = SimpleNamespace(_find_llm_provider=lambda id: p if id == "venice" else None)
    provider_usage_routes.register(app, session)
    endpoint = next(r.endpoint for r in app.routes if r.path.endswith("/usage"))
    monkeypatch.setattr(usage.ProviderUsageService, "fetch", lambda self, obj, days: dict(
        provider=obj.id, days=days))
    assert endpoint("venice", 14) == dict(provider="venice", days=14)
    with pytest.raises(Exception) as exc:
        endpoint("missing", 7)
    assert exc.value.status_code == 404
    with pytest.raises(Exception) as exc:
        endpoint("venice", 90)
    assert exc.value.status_code == 400
