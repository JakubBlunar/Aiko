import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { WorldItem } from "../../types";
import { sketchPages, WorldSketchbook } from "./WorldSketchbook";

describe("editable sketchbook", () => {
  it("shows actual strokes, saved versions and a link to the exact revision", () => {
    const first = { title: "Rooftops", note: "First attempt", revision: 1,
      at: "2026-11-14T12:00:00Z", strokes: [{ points: [[20, 200], [100, 100], [180, 200]],
        width: 2, shade: 0, filled: false }] };
    const item: WorldItem = { id: 1, slug: "sketchbook", name: "Sketchbook", kind: "sketchbook",
      consumable: false, quantity: 1, location_id: 1, description: "", given_by: "household",
      created_at: first.at, updated_at: first.at, state: { home_sketchbook: {
        schema: 1, reference: "An authored fictional skyline.", pages: [{ id: "page1",
          revisions: [first, { ...first, title: "Roof shadows", revision: 2 }] }],
      } } };
    const html = renderToStaticMarkup(<WorldSketchbook item={item} />);
    expect(html).toContain("polyline");
    expect(html).toContain('points="20,200 100,100 180,200"');
    expect(html).toContain("Revision 1");
    expect(html).toContain("Roof shadows");
    expect(html).toContain("sketch.svg?page=1&amp;revision=2");
    expect(html).toContain("fictional skyline");
    expect(sketchPages(item.state)[0].revisions).toHaveLength(2);
  });

  it("distinguishes a blank book from a saved drawing and rejects malformed pages", () => {
    const item = { id: 1, state: { home_sketchbook: { schema: 1, pages: [] } } } as unknown as WorldItem;
    expect(renderToStaticMarkup(<WorldSketchbook item={item} />)).toContain("No saved drawings yet");
    expect(sketchPages({ home_sketchbook: { schema: 1, pages: [null, { id: "bad", revisions: [null] }] } }))
      .toEqual([]);
  });
});
