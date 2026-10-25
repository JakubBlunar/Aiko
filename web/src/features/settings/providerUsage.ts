export interface UsageTokens {
  total_tokens: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  cache_write_tokens: number | null;
  cache_hit_pct: number | null;
  usd: number | null;
  diem: number | null;
}

export interface ProviderUsage {
  provider: string;
  scope: "account";
  days: number;
  start_date: string;
  end_date: string;
  fetched_at: string;
  requests_complete: boolean;
  summary: UsageTokens & { requests: number | null };
  models: (UsageTokens & { name: string })[];
  daily: { date: string; requests: number | null; usd: number | null; diem: number | null }[];
  balances: { usd: number | null; diem: number | null;
    bundledCredits: number | null; earnedCredits: number | null } | null;
  prices: { id: string; name: string; input: number | null; output: number | null;
    cache_input: number | null; cache_write: number | null }[];
  warnings: string[];
}

export function isVeniceProvider(provider: { kind: string; base_url: string }): boolean {
  try {
    const url = new URL(provider.base_url.trim().replace(/\/+$/, ""));
    return provider.kind === "openai_compatible" && url.protocol === "https:"
      && url.host === "api.venice.ai" && url.pathname === "/api/v1"
      && !url.username && !url.password && !url.search && !url.hash;
  } catch {
    return false;
  }
}

export function usageNumber(value: number | null | undefined, digits = 0): string {
  return value == null ? "Not reported" : value.toLocaleString(undefined, {
    maximumFractionDigits: digits,
  });
}

export function usageUsd(value: number | null | undefined): string {
  return value == null ? "Not reported" : `$${value.toFixed(value > 0 && value < 0.01 ? 6 : 4)}`;
}
