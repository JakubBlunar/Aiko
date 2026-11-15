import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { IdleLifeTimeline } from "./WorldIdleLife";
import type { IdleLifeEvent } from "./world/idleLifeTypes";

describe("idle-life timeline", () => {
  it("distinguishes a held interval from actual steps and escapes data", () => {
    const event: IdleLifeEvent = { id: 7, event_key: null, run_id: "run-7", worker: "away_activity",
      actor: "autonomous", kind: "admission", occurred_at: "2026-11-15T12:00:00Z",
      recorded_at: "2026-11-14T12:00:00Z", last_seen_at: "2026-11-14T12:01:00Z",
      observations: 4, payload: { reason: "shared_hold", source_id: 3, text: "<script>" } };
    const html = renderToStaticMarkup(<IdleLifeTimeline events={[event]} />);
    expect(html).toContain("shared_hold");
    expect(html).toContain("4 observations");
    expect(html).toContain("Narrative time");
    expect(html).toContain("Event #7");
    expect(html).not.toContain("<script>");
    expect(html).toContain("source_id");
  });
});
