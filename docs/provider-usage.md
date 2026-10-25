# Provider usage in settings

Settings -> Chat -> Provider usage reads Venice account statistics using the
saved provider key on the backend. It sends only authenticated metadata GETs to
`https://api.venice.ai/api/v1`: no prompts, messages, persona, or inference probes.
The provider must use that official HTTPS base URL. Keys never reach the browser.

Venice requires an **Admin API key** for billing endpoints. A valid Inference Only
key can list models but receives HTTP 401 with "Admin API key required" on billing.
Use **Connect Venice billing statistics** in the usage panel to save the Admin key.
This adds a separate "Venice statistics" provider and selects it for statistics,
leaving it unassigned to inference roles. Its secret uses the existing provider
credential storage flow. It can be edited or deleted in Saved providers. You can
also add a separate provider from the Venice template manually and select it.

The first adapter is Venice. The adapter boundary is `app/llm/provider_usage.py`;
other providers can be added without changing inference. This version uses
Venice's retained billing data directly, including historical usage, rather than
maintaining a second persistent ledger. It does not poll in the background.

- Today, 7d, 14d and 30d include the current UTC calendar day. They are not rolling
  24-hour intervals or local-midnight days.
- Account-wide analytics include usage outside Aiko where reported by Venice.
  Two saved providers for the same Venice account show the same account totals.
- Text token totals and splits come from `billing/usage-analytics`. Cache hit rate
  is cache-read input / (regular input + cache read + cache write), weighted by
  tokens across text models. Unknown splits are shown as "Not reported".
- Spend includes all model types. USD and DIEM remain separate; DIEM usage is not
  presented as cash paid. Balances also show bundled and earned credits.
- Billed text requests are distinct `inferenceDetails.requestId` values from
  `billing/usage-history`, deduplicated across input/output/cache/currency rows.
  Unbilled failed attempts are not counted. Missing IDs or a truncated walk make
  the count an explicit lower bound. History failure leaves it unknown.
- History uses cursor-only continuation calls, at most 20 pages and a 25-second
  soft walk budget. Analytics remain visible if optional history, balance or
  pricing calls fail. Request counts can be newer than the analytics snapshot.
- Current model pricing is fetched from `models`; it is USD per million text
  tokens, and is never used to recalculate historical charges.
- Results are cached in memory for 60 seconds, scoped to provider, credential,
  range and UTC date. Venice additionally caches its beta analytics for about ten
  minutes, so Refresh cannot force instant accounting updates.

References: [analytics](https://docs.venice.ai/api-reference/endpoint/billing/usage-analytics),
[history](https://docs.venice.ai/api-reference/endpoint/billing/usage-history),
[balance](https://docs.venice.ai/api-reference/endpoint/billing/balance),
[caching](https://docs.venice.ai/guides/features/prompt-caching),
[models](https://docs.venice.ai/api-reference/endpoint/models/list).
