import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import type { Memory } from "../../types";
import { DiaryEntry, DiaryTab } from "./DiaryTab";

function entry(overrides: Partial<Memory> = {}): Memory {
  return {
    id: 3, kind: "curiosity_finding", content: "I read about orchards.",
    created_at: "2026-01-03T08:00:00Z", salience: 0.5, source_session: null,
    source_message_id: null, last_used_at: null, use_count: 0, pinned: false,
    ...overrides,
  };
}

describe("Diary research entries", () => {
  it("has a research filter wired to the backend kind", () => {
    expect(renderToStaticMarkup(<DiaryTab />)).toContain(">research</button>");
    const source = readFileSync(new URL("./DiaryTab.tsx", import.meta.url), "utf8");
    expect(source).toContain('{ id: "curiosity_finding", label: "research" }');
    expect(source).toContain("kind,");
    expect(source).toContain("setKind(f.id)");
    expect(source).toContain("setPage(0)");
  });

  it("renders a distinct badge, source title, URL and excerpt with its limitation", () => {
    const html = renderToStaticMarkup(<DiaryEntry entry={entry({ metadata: {
      research_evidence: [{
        title: "Orchards", url: "https://example.org/orchard", snippet: "A search excerpt.",
      }],
      evidence_type: "search_excerpt", source_urls: ["https://example.org/orchard"],
    } })} />);
    expect(html).toContain(">research</span>");
    expect(html).toContain("Research sources (1)");
    expect(html).toContain("Orchards");
    expect(html).toContain('href="https://example.org/orchard"');
    expect(html).toContain("A search excerpt.");
    expect(html).toContain("not verified full pages");
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).not.toContain(" italic");
  });

  it("keeps legacy URLs and states when evidence is absent", () => {
    const html = renderToStaticMarkup(<DiaryEntry entry={entry({ metadata: {
      source_urls: ["https://example.org/old"],
    } })} />);
    expect(html).toContain('href="https://example.org/old"');
    expect(html).toContain("no search excerpts recorded");
    expect(renderToStaticMarkup(<DiaryEntry entry={entry()} />))
      .toContain("No source details recorded.");
  });

  it("handles malformed metadata and never makes unsafe URLs clickable", () => {
    const html = renderToStaticMarkup(<DiaryEntry entry={entry({ metadata: {
      research_evidence: [null, 4, {}, { title: "<script>", url: "javascript:alert(1)" }],
      source_urls: [4, null, "not a URL"],
    } })} />);
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("javascript:alert(1)");
    expect(html).not.toContain("href=");
    expect(html).not.toContain("<script>");
  });

  it("preserves reflection badges and does not call them research", () => {
    const html = renderToStaticMarkup(<DiaryEntry entry={entry({
      kind: "reflection", content: "[dream] An orchard dream.", pinned: true,
    })} />);
    expect(html).toContain(">dream</span>");
    expect(html).toContain("An orchard dream.");
    expect(html).toContain("italic");
    expect(html).toContain(">pinned</span>");
    expect(html).not.toContain("Research sources");
    expect(html).not.toContain("[dream]");
  });

  it("does not turn prefix-like research text into a dream", () => {
    const html = renderToStaticMarkup(<DiaryEntry entry={entry({
      content: "[dream] A search query about dream research.",
    })} />);
    expect(html).toContain(">research</span>");
    expect(html).toContain("[dream]");
    expect(html).not.toContain(">dream</span>");
  });
});
