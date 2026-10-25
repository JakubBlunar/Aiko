"""Provider account statistics, independent of inference and chat history.

Venice already retains a billing ledger, so this adapter can show historical
usage without logging prompts or instrumenting every worker. Other providers
can add adapters behind this boundary. No inference requests are made here.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import requests

from app.core.infra import timephrase
from app.core.infra.settings import LlmProvider
from app.llm.factory import resolve_provider_api_key

VENICE_BASE = "https://api.venice.ai/api/v1"
PERIODS = (1, 7, 14, 30)


class UsageUnavailable(Exception):
    """Safe, credential-free explanation suitable for the settings panel."""


def supports_account_usage(provider: LlmProvider) -> bool:
    try:
        url = urlsplit(provider.base_url.strip().rstrip("/"))
        return (
            provider.kind == "openai_compatible"
            and url.scheme == "https" and url.netloc.lower() == "api.venice.ai"
            and url.path == "/api/v1" and not url.query and not url.fragment
        )
    except ValueError:
        return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _sum_known(values: list[float | None]) -> float | None:
    return sum(values) if all(v is not None for v in values) else None


def _token_split(row: dict) -> dict:
    parts = row.get("breakdown")
    result = dict(input_tokens=None, output_tokens=None, cached_tokens=None,
                  cache_write_tokens=None, cache_hit_pct=None)
    if not isinstance(parts, list) or not parts:
        return result
    amounts: dict[str, float] = {}
    for part in parts:
        if not isinstance(part, dict):
            return result
        kind = part.get("type")
        units = _number(part.get("units"))
        if not isinstance(kind, str) or kind not in {
            "Input", "Output", "Cache Read", "Cache Write",
        } or units is None:
            return result
        amounts[kind] = amounts.get(kind, 0) + units
    # Billing types partition the input; cache reads/writes are not additional
    # requests or output tokens. Missing types in a reported breakdown are zero.
    input_tokens = sum(amounts.get(k, 0) for k in ("Input", "Cache Read", "Cache Write"))
    cached = amounts.get("Cache Read", 0)
    return dict(
        input_tokens=input_tokens, output_tokens=amounts.get("Output", 0),
        cached_tokens=cached, cache_write_tokens=amounts.get("Cache Write", 0),
        cache_hit_pct=100 * cached / input_tokens if input_tokens else None,
    )


def normalize_analytics(body: dict) -> dict:
    """Keep missing/beta fields unknown instead of fabricating zero usage."""
    if not isinstance(body.get("byModel"), list) or not isinstance(body.get("byDate"), list):
        raise UsageUnavailable("Venice returned an unrecognized analytics format.")
    models = []
    for row in body["byModel"]:
        if not isinstance(row, dict) or not isinstance(row.get("unitType"), str):
            raise UsageUnavailable("Venice returned an unrecognized model usage format.")
        if row["unitType"] != "tokens":
            continue
        models.append(dict(
            name=str(row.get("modelName", "Unknown model")),
            total_tokens=_number(row.get("totalUnits")),
            usd=_number(row.get("totalUsd")), diem=_number(row.get("totalDiem")),
            **_token_split(row),
        ))
    summary = {
        key: _sum_known([m[key] for m in models])
        for key in ("total_tokens", "input_tokens", "output_tokens", "cached_tokens",
                    "cache_write_tokens")
    }
    prompt, cached = summary["input_tokens"], summary["cached_tokens"]
    summary["cache_hit_pct"] = (
        100 * cached / prompt if prompt and cached is not None else None
    )
    daily = []
    for row in body["byDate"]:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise UsageUnavailable("Venice returned an unrecognized daily usage format.")
        daily.append(dict(date=row["date"][:10], usd=_number(row.get("USD")),
                          diem=_number(row.get("DIEM"))))
    summary["usd"] = _sum_known([r["usd"] for r in daily])
    summary["diem"] = _sum_known([r["diem"] for r in daily])
    return dict(summary=summary, models=models, daily=daily)


def normalize_prices(body: dict) -> list[dict]:
    if not isinstance(body.get("data"), list):
        raise UsageUnavailable("Venice returned an unrecognized pricing format.")
    prices = []
    for row in body.get("data", []):
        if not isinstance(row, dict):
            continue
        spec = row.get("model_spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("pricing"), dict):
            continue
        rates = {}
        for kind in ("input", "output", "cache_input", "cache_write"):
            rate = spec["pricing"].get(kind)
            rates[kind] = _number(rate.get("usd")) if isinstance(rate, dict) else None
        prices.append(dict(id=str(row.get("id", "")), name=str(spec.get("name", row.get("id"))),
                           **rates))
    return prices


class ProviderUsageService:
    """Short-lived in-memory cache; credentials and raw ledger rows never leave Python."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def fetch(self, provider: LlmProvider, days: int) -> dict:
        if days not in PERIODS:
            raise ValueError("Choose 1, 7, 14 or 30 days.")
        if not supports_account_usage(provider):
            raise UsageUnavailable("Account statistics are currently available for Venice only.")
        key = resolve_provider_api_key(provider)
        if not key:
            raise UsageUnavailable("Save a Venice API key in the provider settings first.")
        today = timephrase.to_aware(timephrase.utcnow()).date()
        start = today - timedelta(days=days - 1)
        cache_key = (provider.id, hashlib.sha256(key.encode()).hexdigest(), days, today)
        # Coalesce concurrent refreshes without sharing requests.Session across threads.
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 60:
                return cached[1]
            with requests.Session() as http:
                http.headers.update({
                    "Authorization": f"Bearer {key}", "Accept": "application/json",
                })
                result = self._fetch(http, start.isoformat(), today.isoformat(), days)
            self._cache = {k: v for k, v in self._cache.items() if time.monotonic() - v[0] < 60}
            if len(self._cache) >= 16:
                self._cache.clear()
            self._cache[cache_key] = (time.monotonic(), result)
            return result

    @staticmethod
    def _get(http: requests.Session, path: str, params: dict | None = None) -> dict:
        try:
            with http.get(VENICE_BASE + path, params=params, timeout=(5, 15),
                          allow_redirects=False) as response:
                if response.status_code != 200:
                    hints = {
                        401: (
                            "Venice billing requires an Admin API key. Add a separate Venice "
                            "provider with an Admin key for statistics and leave it unassigned "
                            "to chat roles. An invalid key can also cause this error."
                        ),
                        403: "Cannot read billing data; check key permissions or IP restrictions.",
                        429: "Venice rate limited statistics. Try again later.",
                    }
                    raise UsageUnavailable(hints.get(response.status_code,
                        f"Venice statistics returned HTTP {response.status_code}."))
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("expected object")
                return body
        except (requests.RequestException, ValueError):
            # Never expose provider bodies, URLs, headers, or exception strings.
            raise UsageUnavailable("Could not read Venice statistics. Try again later.") from None

    def _fetch(self, http: requests.Session, start: str, end: str, days: int) -> dict:
        result = normalize_analytics(self._get(http, "/billing/usage-analytics", {
            "startDate": start, "endDate": end,
        }))
        result.update(provider="venice", scope="account", days=days, start_date=start,
                      end_date=end, fetched_at=timephrase.utcnow().isoformat(),
                      warnings=[], balances=None, prices=[])
        for path, field in (("/billing/balance", "balances"), ("/models", "prices")):
            try:
                body = self._get(http, path)
                if field == "prices":
                    result[field] = normalize_prices(body)
                elif isinstance(body.get("balances"), dict):
                    result[field] = {k: _number(body["balances"].get(k))
                                     for k in ("usd", "diem", "bundledCredits", "earnedCredits")}
            except UsageUnavailable as exc:
                result["warnings"].append(f"{field.capitalize()}: {exc}")
        self._requests(http, result, start, end)
        return result

    def _requests(self, http: requests.Session, result: dict, start: str, end: str) -> None:
        """Count billed LLM request IDs once across token/currency ledger rows."""
        end_exclusive = timephrase.parse_iso(end + "T00:00:00Z") + timedelta(days=1)
        params = dict(startTimestamp=start + "T00:00:00Z",
                      endTimestamp=end_exclusive.isoformat().replace("+00:00", "Z"), pageSize=1000)
        seen: set[str] = set()
        cursors: set[str] = set()
        daily: dict[str, int] = {}
        complete = True
        available = False
        deadline = time.monotonic() + 25
        try:
            for _ in range(20):
                page = self._get(http, "/billing/usage-history", params)
                if not isinstance(page.get("data"), list) or "nextCursor" not in page:
                    raise UsageUnavailable("Venice returned an unrecognized history format.")
                available = True
                for row in page["data"]:
                    if not isinstance(row, dict):
                        complete = False
                        continue
                    details = row.get("inferenceDetails")
                    if "-llm-" not in str(row.get("sku", "")) and not (
                        isinstance(details, dict) and "promptTokens" in details
                    ):
                        continue
                    if not isinstance(details, dict):
                        if "-llm-" in str(row.get("sku", "")):
                            complete = False
                        continue
                    request_id = details.get("requestId")
                    if not isinstance(request_id, str) or not request_id:
                        complete = False
                        continue
                    if request_id in seen:
                        continue
                    seen.add(request_id)
                    date = str(row.get("timestamp", ""))[:10]
                    daily[date] = daily.get(date, 0) + 1
                cursor = page["nextCursor"]
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise UsageUnavailable("Venice returned an invalid history cursor.")
                cursors.add(cursor)
                params = {"cursor": cursor}  # Venice rejects filters on continuation pages.
                if time.monotonic() >= deadline:
                    complete = False
                    break
            else:
                complete = False
        except UsageUnavailable as exc:
            complete = False
            result["warnings"].append(f"Request counts: {exc}")
        result["summary"]["requests"] = len(seen) if available else None
        result["requests_complete"] = complete
        # Fill calendar days, including no-usage days, for a stable chart.
        spend = {r["date"]: r for r in result["daily"]}
        result["daily"] = []
        for offset in range(result["days"]):
            date = (timephrase.parse_iso(start + "T00:00:00Z") + timedelta(days=offset)).date()
            label = date.isoformat()
            result["daily"].append(dict(
                date=label, requests=daily.get(label, 0) if available else None,
                **{k: spend.get(label, {}).get(k, 0) for k in ("usd", "diem")},
            ))
