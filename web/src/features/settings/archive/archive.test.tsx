import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api";
import { MessageCard, OverviewContent } from "./ArchiveContent";
import { archiveStatus, localDate, type ArchiveOverview, type ArchiveResponse } from "./types";

afterEach(() => vi.unstubAllGlobals());

describe("archive review", () => {
  it("keeps plans, suggestions, citations and partial coverage visible", () => {
    const overview: ArchiveOverview = {
      local_day: "2026-10-20", message_count: 2, message_count_lower_bound: 2, session_count: 1,
      coverage: { scan_complete: true, current_day_open: false },
      summary: {
        source_messages_used: 2, source_text_complete: false, generated_at: "2026-10-21T12:00:00Z",
        bullets: [
          { kind: "plan", text: "The user planned a practice session.", event_date: "2026-10-22",
            sources: [{ message_id: 12, quote: "I plan to practice" }] },
          { kind: "assistant_suggestion", text: "Aiko suggested a warmup.", event_date: null,
            sources: [{ message_id: 13, quote: "Try a warmup" }] },
        ],
      },
    };
    const html = renderToStaticMarkup(<OverviewContent overview={overview} onSource={() => {}} />);
    expect(html).toContain("Planned");
    expect(html).toContain("Aiko suggested");
    expect(html).toContain("Open source #12");
    expect(html).toContain("Open source #13");
    expect(html).toContain("Mentioned event date: 2026-10-22");
    expect(html).toContain("Selected or shortened text only");
    expect(html).toContain("may not have happened");
    expect(html).not.toContain("Full retained text considered");
  });

  it("escapes source text and identifies shortened excerpts", () => {
    const html = renderToStaticMarkup(<MessageCard message={{
      message_id: 1, session_id: "alice:main", role: "user", created_at: "2026-10-20T12:00:00Z",
      text: "<script>secret()</script>", text_complete: false, content_length: 999, next_offset: 24,
    }} onSource={() => {}} />);
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("open source for more");
  });

  it("does not treat missing outlines as missing conversation", () => {
    expect(archiveStatus({ status: "missing_overviews" } as ArchiveResponse)).toBeNull();
    expect(archiveStatus({ status: "stale_cursor" } as ArchiveResponse)).toContain("Refresh");
    expect(archiveStatus({ status: "not_found" } as ArchiveResponse)).toContain("no longer available");
    expect(localDate(new Date(2026, 9, 20, 0, 10))).toBe("2026-10-20");
  });

  it("encodes literal searches and sends explicit generation/cancellation requests", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "ok" }) });
    vi.stubGlobal("fetch", fetch);
    await api.getArchive({ mode: "messages", start: "2026-10-20", end: "2026-10-20", topic: "%_ & guitar" });
    const query = new URL(String(fetch.mock.calls[0][0]), "http://localhost").searchParams;
    expect(query.get("topic")).toBe("%_ & guitar");
    await api.getArchive({ mode: "message", cursor: "opaque+cursor/=" });
    const nextQuery = new URL(String(fetch.mock.calls[1][0]), "http://localhost").searchParams;
    expect([...nextQuery.keys()]).toEqual(["mode", "cursor"]);
    await api.requestArchiveSummary("2026-10-20", "Europe/Bratislava");
    await api.requestArchiveSummary("2026-10-20", "Europe/Bratislava", true);
    expect(fetch.mock.calls[2][1].method).toBe("POST");
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ day: "2026-10-20", timezone: "Europe/Bratislava" });
    expect(fetch.mock.calls[3][1].method).toBe("DELETE");
  });
});
