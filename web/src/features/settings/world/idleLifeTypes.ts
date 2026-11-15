export interface IdleLifeEvent {
  id: number;
  event_key: string | null;
  run_id: string;
  worker: string;
  actor: string;
  kind: string;
  occurred_at: string;
  recorded_at: string;
  last_seen_at: string;
  observations: number;
  payload: Record<string, unknown>;
}
export interface IdleLifeTelemetry {
  oldest_recorded_at: string | null;
  latest_observed_at: string | null;
  retained_events: number;
  failed_writes_since_boot: number;
  last_error: string | null;
  runtime_fingerprint: string;
  coverage_note: string;
}
export interface IdleLifeHistory {
  events: IdleLifeEvent[];
  next_before_id: number | null;
  retention_days: number;
  requested_hours: number;
  telemetry: IdleLifeTelemetry;
}
export interface IdleLifeSummary {
  hours: number;
  counts: { worker: string; kind: string; count: number }[];
  admission: { reason: string; intervals: number; observations: number }[];
  actions: Record<string, number>;
  telemetry: IdleLifeTelemetry;
  interpretation: string;
  flags?: { kind: string; event_ids: number[]; detail: string }[];
  autonomous_steps?: number;
  maintenance_steps?: number;
  context_offer_events?: number;
  model_attribution_events?: number;
  canonical_home_resource_deltas?: { item_id: string; field: string; net_change: number }[];
  observed_worker_resource_deltas?: { item_id: string; field: string; net_change: number }[];
  resource_note?: string;
  threshold_note?: string;
  coverage?: {
    observed_scheduler_seconds: number;
    unknown_span_count: number;
    unknown_spans: { from: string; to: string; seconds: number; status: string }[];
    omitted_spans: number;
    note: string;
  };
}
