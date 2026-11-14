import { useEffect, useMemo, useState } from "react";
import type { WorldItem } from "../../types";

export function WorldBathroom({ item }: { item: WorldItem }) {
  const view = item.home_bathroom;
  const anchor = useMemo(() => ({
    wall: Date.now(), server: Date.parse(view?.as_of ?? ""),
  }), [view?.as_of]);
  const [wallNow, setWallNow] = useState(Date.now);
  useEffect(() => {
    if (view?.bath_phase !== "soaking" && view?.towel !== "drying") return;
    const timer = window.setInterval(() => setWallNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [view?.bath_phase, view?.towel]);
  if (!view) return null;
  const now = Number.isFinite(anchor.server)
    ? anchor.server + Math.max(0, wallNow - anchor.wall) : wallNow;
  const ready = view.bath_ready_to_finish || now >= Date.parse(view.bath_ready_at ?? "");
  const state = item.state.home_bathroom as Record<string, unknown> | undefined;
  const wetAt = Date.parse(String(state?.towel_wet_at ?? ""));
  const towel = Number.isFinite(wetAt) && now - wetAt >= 4 * 3600_000 ? "dry" : view.towel;
  const bath = state?.bath as Record<string, unknown> | undefined;
  const started = Date.parse(String(bath?.started_at ?? ""));
  const water = Number.isFinite(started)
    ? (22 + 18 * Math.exp(-Math.max(0, now - started) / 3600_000) >= 30 ? "warm" : "cool")
    : view.water_temperature_band;
  return (
    <div className="mt-1 space-y-1 text-[11px] text-ink-100/65">
      <div>
        {view.bath_phase === "empty" ? "Bath empty" : (
          <>Bath started · {water} water
            {ready ? " · ready to finish" : " · soak in progress"}
          </>
        )}
        {` · towel ${towel}`}
      </div>
      {view.last_brushed_teeth_at ? (
        <div>Teeth brushed: <time dateTime={view.last_brushed_teeth_at}>
          {new Date(view.last_brushed_teeth_at).toLocaleString()}
        </time></div>
      ) : null}
    </div>
  );
}
