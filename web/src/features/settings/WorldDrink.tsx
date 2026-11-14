import { useEffect, useMemo, useState } from "react";
import type { WorldItem } from "../../types";

export function drinkTemperatureBand(
  vessel: Record<string, unknown>, nowMs: number,
): string {
  const drink = vessel.drink as Record<string, unknown> | null;
  if (!drink) return "room temperature";
  const initial = Number(drink.thermal_c ?? 22);
  const prepared = Date.parse(String(drink.thermal_at));
  const minutes = Number.isFinite(prepared) ? Math.max(0, (nowMs - prepared) / 60000) : 0;
  let tau = 24;
  if (vessel.insulated) tau *= 2;
  if (vessel.has_lid && vessel.lid_closed) tau *= 2;
  const degrees = 22 + ((Number.isFinite(initial) ? initial : 22) - 22) * Math.exp(-minutes / tau);
  if (degrees < 10) return "chilled";
  if (degrees < 18) return "cool";
  if (degrees < 28) return "room temperature";
  if (degrees < 40) return "lukewarm";
  if (degrees < 60) return "warm";
  return "hot";
}

export function WorldDrink({ item }: { item: WorldItem }) {
  const view = item.home_drink;
  const vessel = item.state.home_vessel as Record<string, unknown> | undefined;
  const anchor = useMemo(() => ({
    wall: Date.now(), server: Date.parse(view?.as_of ?? ""),
  }), [view?.as_of]);
  const [wallNow, setWallNow] = useState(Date.now);
  useEffect(() => {
    if (!view?.remaining_ml) return;
    const timer = window.setInterval(() => setWallNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [view?.remaining_ml]);
  if (!view || !vessel) return null;
  const now = Number.isFinite(anchor.server)
    ? anchor.server + Math.max(0, wallNow - anchor.wall) : wallNow;
  return (
    <div className="mt-1 text-[11px] text-amber-100/75">
      {view.remaining_ml > 0
        ? `${view.name} · ${drinkTemperatureBand(vessel, now)} · ${view.remaining_ml} ml`
        : view.clean ? "Empty and clean" : "Empty · needs washing"}
      {view.has_lid ? ` · lid ${view.lid_closed ? "closed" : "open"}` : ""}
    </div>
  );
}
