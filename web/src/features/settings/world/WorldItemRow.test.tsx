import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import type { WorldItem } from "@/types";
import { WorldItemRow } from "./WorldItemRow";

it("opens the shared box from its shelf row and hides generic item controls", () => {
  const item: WorldItem = {
    id: 1, slug: "shared_keepsake_box", name: "Our keepsake box", description: "",
    kind: "decor", consumable: false, quantity: 1, location_id: 1, given_by: "household",
    state: { home_essential: true, keepsake_latest_note_id: null },
    created_at: "2026-11-20T20:00:00Z", updated_at: "2026-11-20T20:00:00Z",
  };
  const noop = () => {};
  const renderRow = (worldItem: WorldItem) => renderToStaticMarkup(
    <WorldItemRow item={worldItem} locations={[]} editing={false} busy={false}
      draft={{ name: "", description: "", kind: "decor", quantity: 1, location_id: 1 }}
      setDraft={noop} onStartEdit={noop} onCancelEdit={noop} onSave={noop}
      onDelete={noop} onConsume={noop} />,
  );
  const box = renderRow(item);
  expect(box).toContain("Our keepsake box");
  expect(box).toContain("Open the box");
  expect(box).not.toContain("<button");

  const ordinaryItem = renderRow({ ...item, slug: "decoration", state: {} });
  expect(ordinaryItem).toContain(">remove</button>");
  expect(ordinaryItem).not.toContain("Open the box");
});
