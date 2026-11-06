import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { WorldItem } from "../../types";
import { creationRevisions, WorldCreation } from "./WorldCreation";

const baseItem: WorldItem = {
  id: 1, slug: "paper_boats", name: "Paper Boats", description: "A poem", kind: "other",
  consumable: false, quantity: 1, location_id: 1, state: {}, given_by: null,
  created_at: "2026-11-06T10:00:00Z", updated_at: "2026-11-06T11:00:00Z",
};

const state = { creation_schema: 1, revisions: [
  { revision: 1, title: "Paper Boats", form: "poem", text: "The first folded sail.", at: "2026-11-06T10:00:00Z" },
  { revision: 2, title: "Paper Boats", form: "poem", text: "The second <folded> sail.\nAnother line.", at: "2026-11-06T11:00:00Z" },
] };

describe("saved world writing", () => {
  it("shows the latest saved text, timestamp and accessible revision choices", () => {
    const item = { ...baseItem, state };
    const html = renderToStaticMarkup(<WorldCreation item={item} />);
    expect(html).toContain("Read Aiko");
    expect(html).toContain("Version of Paper Boats");
    expect(html).toContain("Revision 1");
    expect(html).toContain("Revision 2");
    expect(html).toContain("The second &lt;folded&gt; sail.");
    expect(html).not.toContain("The first folded sail.");
    expect(html).toContain('dateTime="2026-11-06T11:00:00Z"');
    expect(html).toContain("creative writing");
    expect(html).toContain("whitespace-pre-wrap break-words");
  });

  it("keeps older saved text available and ignores malformed state", () => {
    expect(creationRevisions(state)[0].text).toBe("The first folded sail.");
    expect(creationRevisions({ creation_schema: 1, revisions: [null, 3, {}, ...state.revisions] }))
      .toHaveLength(2);
    expect(creationRevisions({})).toEqual([]);
    expect(renderToStaticMarkup(<WorldCreation item={baseItem} />)).toBe("");
  });
});
