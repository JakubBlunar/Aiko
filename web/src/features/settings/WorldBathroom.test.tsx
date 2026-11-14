import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { WorldBathroom } from "./WorldBathroom";
import type { WorldItem } from "../../types";

describe("bathroom status", () => {
  it("distinguishes a bath ready to finish from one actually finished", () => {
    const item: WorldItem = {
      id: 1, slug: "fixtures", name: "Bathroom fixtures", kind: "gadget", description: "",
      consumable: false, quantity: 1, location_id: 1, state: {}, given_by: "household",
      created_at: "2026-11-14T18:00:00Z", updated_at: "2026-11-14T18:00:00Z",
      home_bathroom: { as_of: "2026-11-14T18:21:00Z",
        bath_phase: "soaking", bath_ready_to_finish: true,
        bath_ready_at: "2026-11-14T18:20:00Z", water_temperature_band: "warm",
        towel: "dry", last_brushed_teeth_at: null },
    };
    expect(renderToStaticMarkup(<WorldBathroom item={item} />)).toContain("ready to finish");
    expect(renderToStaticMarkup(<WorldBathroom item={item} />)).not.toContain("Bath empty");
    item.home_bathroom!.bath_phase = "empty";
    expect(renderToStaticMarkup(<WorldBathroom item={item} />)).toContain("Bath empty");
  });
});
