import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { drinkTemperatureBand, WorldDrink } from "./WorldDrink";
import type { WorldItem } from "../../types";

const at = "2026-11-14T14:00:00Z";
const vessel = { schema: 1, clean: false, heat_safe: true, drink: {
  recipe: "coffee", name: "coffee", amount_ml: 175, thermal_c: 85, thermal_at: at,
} };

describe("world drinks", () => {
  it("ages hot and cold drinks toward room temperature and honors insulation", () => {
    const later = Date.parse(at) + 20 * 60_000;
    expect(drinkTemperatureBand(vessel, later)).toBe("warm");
    expect(drinkTemperatureBand({ ...vessel, has_lid: true, lid_closed: true }, later))
      .toBe("hot");
    const juice = { ...vessel, drink: { ...vessel.drink, thermal_c: 8 } };
    expect(drinkTemperatureBand(juice, Date.parse(at))).toBe("chilled");
    expect(drinkTemperatureBand(juice, later + 4 * 3600_000)).toBe("room temperature");
  });

  it("shows drink contents and preserves the empty reusable vessel", () => {
    const item: WorldItem = {
      id: 1, slug: "mug", name: "Mug", description: "", kind: "vessel",
      consumable: false, quantity: 1, location_id: 1, given_by: null,
      created_at: at, updated_at: at, state: { home_vessel: vessel },
      home_drink: { recipe: "coffee", name: "coffee", remaining_ml: 175,
        temperature_band: "hot", clean: false, has_lid: false, lid_closed: false, as_of: at },
    };
    expect(renderToStaticMarkup(<WorldDrink item={item} />)).toContain("175 ml");
    const empty = { ...item, home_drink: { ...item.home_drink!, remaining_ml: 0 } };
    expect(renderToStaticMarkup(<WorldDrink item={empty} />)).toContain("needs washing");
  });
});
