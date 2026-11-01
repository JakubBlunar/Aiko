export interface ArchiveMessage {
  message_id: number;
  session_id: string;
  role: "user" | "assistant";
  created_at: string;
  text: string;
  text_complete: boolean;
  content_length: number;
  next_offset: number | null;
}

export interface ArchiveBullet {
  kind: "discussion" | "user_statement" | "assistant_suggestion" | "plan" | "decision" | "correction";
  text: string;
  event_date: string | null;
  sources: Array<{ message_id: number; quote: string }>;
}

export interface ArchiveOverview {
  local_day: string;
  message_count: number | null;
  message_count_lower_bound: number;
  session_count: number;
  coverage: { scan_complete: boolean; current_day_open: boolean };
  excerpts?: ArchiveMessage[];
  summary?: {
    bullets: ArchiveBullet[];
    source_messages_used: number;
    source_text_complete: boolean;
    generated_at: string;
  };
}

export interface ArchiveJob {
  local_day: string;
  timezone: string;
  state: "pending" | "running" | "failed";
  reason: string;
}

export interface ArchiveResponse {
  status: string;
  range?: { timezone: string };
  archive_timezone: string;
  generation_available: boolean;
  overviews?: ArchiveOverview[];
  missing_days?: string[];
  jobs?: ArchiveJob[];
  messages?: ArchiveMessage[];
  context?: ArchiveMessage[];
  message?: ArchiveMessage;
  next_cursor?: string | null;
  coverage?: {
    source_scan_complete?: boolean;
    all_source_days_indexed?: boolean;
    scan_complete?: boolean;
    current_period_open?: boolean;
  };
}

export interface ArchiveQuery {
  mode: "overviews" | "messages" | "message";
  start?: string;
  end?: string;
  topic?: string;
  cursor?: string;
  message_id?: number;
}

export function archiveStatus(result: ArchiveResponse): string | null {
  if (result.status === "stale_cursor") return "The conversation changed. Refresh to start again.";
  if (result.status === "store_unavailable") return "The conversation archive is unavailable.";
  if (result.status === "not_found") return "This source message is no longer available.";
  return null;
}

export function localDate(value = new Date()): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}
