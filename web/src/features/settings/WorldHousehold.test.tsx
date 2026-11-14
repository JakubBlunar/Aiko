import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { HomeHouseholdSnapshot } from "../../types";
import { WorldHouseholdContents } from "./WorldHousehold";

describe("home belongings", () => {
  it("shows deliveries separately from possessions and offers eligible bin recovery", () => {
    const view: HomeHouseholdSnapshot = {
      enabled: true, catalogue: [{ id: "thermos", name: "Thermos", kind: "vessel" }],
      orders: [{ id: "order", product: "thermos", status: "delivered",
        arrives_at: "2026-11-14T12:30:00Z" }], wishlist: ["thermos"],
      stored: [], bin: [{ id: 7, name: "Old cup", lifecycle: {
        expires_at: "2026-11-16T12:00:00Z",
      } }], capacity: 16, active_space_used: 12,
      actions: [{ token: "restore", action: "restore_item", item_id: 7, summary: "Restore cup" }],
    };
    const html = renderToStaticMarkup(<WorldHouseholdContents view={view} busy={false}
      error="" onRestore={() => {}} />);
    expect(html).toContain("delivered");
    expect(html).toContain("Recoverable bin");
    expect(html).toContain("Old cup");
    expect(html).toContain("Bring back");
    expect(html).toContain("12 / 16 active belongings");
    view.actions = [];
    const full = renderToStaticMarkup(<WorldHouseholdContents view={view} busy={false}
      error="" onRestore={() => {}} />);
    expect(full).not.toContain("Bring back");
    expect(full).toContain("Make room");
  });
});
