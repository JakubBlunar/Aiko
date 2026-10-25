import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ProviderUsageSection, UsageDashboard } from "./ProviderUsageSection";
import { isVeniceProvider, usageNumber, usageUsd, type ProviderUsage } from "./providerUsage";

const data: ProviderUsage = {
  provider: "venice", scope: "account", days: 1, start_date: "2026-10-25", end_date: "2026-10-25",
  fetched_at: "2026-10-25T12:00:00Z", requests_complete: true,
  summary: { requests: 7, total_tokens: 1200, input_tokens: 1000, output_tokens: 200,
    cached_tokens: 600, cache_write_tokens: 100, cache_hit_pct: 60, usd: 0.03, diem: 0.8 },
  models: [], daily: [{ date: "2026-10-25", requests: 7, usd: 0.03, diem: 0.8 }],
  balances: { usd: 5, diem: 7, bundledCredits: 3, earnedCredits: 2 },
  prices: [{ id: "deepseek", name: "DeepSeek", input: 0.3, output: 1.2, cache_input: 0.03, cache_write: null }],
  warnings: [],
};

describe("Venice account usage dashboard", () => {
  it("offers Admin-key setup with an unassigned provider and a masked input", () => {
    const html = renderToStaticMarkup(<ProviderUsageSection />);
    expect(html).toContain('type="password"');
    expect(html).toContain("Save statistics provider");
    expect(html).toContain("unassigned to chat roles");
    expect(html).toContain("https://venice.ai/settings/api");
  });
  it("shows usage, scope, cache denominator, separate balances and current pricing", () => {
    const html = renderToStaticMarkup(<UsageDashboard data={data} />);
    for (const text of ["60%", "Billed text requests", "account-wide", "UTC calendar days",
      "Cached input tokens / all input tokens", "DIEM spend", "USD balance", "Bundled credits",
      "DeepSeek", "USD per million tokens", "$0.0300", "10 minutes"])
      expect(html).toContain(text);
  });

  it("marks partial counts and never turns unreported cache or pricing into zero", () => {
    const html = renderToStaticMarkup(<UsageDashboard data={{ ...data, requests_complete: false,
      summary: { ...data.summary, cache_hit_pct: null, requests: 3 }, warnings: ["History unavailable"] }} />);
    expect(html).toContain("At least 3");
    expect(html).toContain("Request counts are incomplete");
    expect(html).toContain("Not reported");
    expect(html).toContain("History unavailable");
    expect(html).not.toContain(">0%</div>");
  });

  it("distinguishes known zeros from unavailable values and retains tiny charges", () => {
    expect(usageNumber(null)).toBe("Not reported");
    expect(usageNumber(0)).toBe("0");
    expect(usageUsd(0.000012)).toBe("$0.000012");
    expect(usageUsd(null)).toBe("Not reported");
  });

  it("recognizes only the official saved Venice endpoint", () => {
    const p = { kind: "openai_compatible", base_url: "https://api.venice.ai/api/v1/" };
    expect(isVeniceProvider(p)).toBe(true);
    for (const base_url of ["https://api.venice.ai.evil.test/api/v1", "http://api.venice.ai/api/v1",
      "https://user:pass@api.venice.ai/api/v1", "https://api.venice.ai/api/v1?extra=1"])
      expect(isVeniceProvider({ ...p, base_url })).toBe(false);
  });
});
