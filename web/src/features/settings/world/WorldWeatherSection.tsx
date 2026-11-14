import { useEffect, useState } from "react";
import type { WeatherSettingsSnapshot } from "@/types";
import { Toggle } from "@/components/Toggle";
import { Section } from "../SettingsSection";

interface WeatherSectionProps {
  weather: WeatherSettingsSnapshot | null;
  onPatch: (patch: Record<string, unknown>) => void;
}

/** H11 weather sync settings. Toggle the shared real-world sky, set a
 * coarse home city (geocoded server-side on save), and pick units. The
 * latest cached conditions show as a small readout. */
export function WorldWeatherSection({ weather, onPatch }: WeatherSectionProps) {
  const [locationInput, setLocationInput] = useState(
    weather?.location_name ?? "",
  );
  // Keep the input in sync when a settings frame lands (e.g. the server
  // canonicalised "tokyo" -> "Tokyo, Japan" after geocoding).
  useEffect(() => {
    setLocationInput(weather?.location_name ?? "");
  }, [weather?.location_name]);

  const syncOn = Boolean(weather?.sync_enabled);
  const units = weather?.units ?? "metric";
  const current = weather?.current ?? null;
  const dirty = (weather?.location_name ?? "") !== locationInput.trim();
  const lat = weather?.latitude ?? null;
  const lon = weather?.longitude ?? null;
  const hasCoords =
    typeof lat === "number" && typeof lon === "number";

  return (
    <Section title="Weather (shared sky)">
      <p className="text-[11px] text-ink-100/50">
        When on, Aiko quietly knows the real-world weather + season at your
        location and can let it colour her mood. Coarse city only — never
        GPS, off by default. She can also answer forecast questions for any
        place you name.
      </p>
      <Toggle
        checked={syncOn}
        onChange={(checked) => onPatch({ sync_enabled: checked })}
      >
        Share my real-world weather with Aiko
      </Toggle>

      <label className="block text-[11px] text-ink-100/60">
        <span>Your location (city)</span>
        <div className="mt-1 flex gap-2">
          <input
            value={locationInput}
            onChange={(e) => setLocationInput(e.target.value)}
            placeholder="e.g. Kamenná Poruba, Žilina, Slovakia"
            className="flex-1 rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
          />
          <button
            type="button"
            onClick={() => onPatch({ location_name: locationInput.trim() })}
            disabled={!dirty}
            className="rounded border border-ink-400/40 bg-ink-500/20 px-3 py-1 text-[11px] text-ink-100 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Save
          </button>
        </div>
        <span className="mt-1 block text-[10px] text-ink-100/40">
          If several places share a name, add the region and country to
          disambiguate — e.g. "City, Region, Country". Check the
          coordinates below resolved to the right spot.
        </span>
      </label>

      {hasCoords ? (
        <p className="flex items-center gap-2 text-[10px] text-ink-100/45">
          <span>
            Resolved: {lat!.toFixed(4)}, {lon!.toFixed(4)}
          </span>
          <a
            href={`https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=11/${lat}/${lon}`}
            target="_blank"
            rel="noreferrer"
            className="text-ink-300 underline decoration-dotted hover:text-ink-100"
          >
            verify on map ↗
          </a>
        </p>
      ) : weather?.location_name ? (
        <p className="text-[10px] text-amber-300/70">
          Couldn't resolve coordinates for this name — try adding the
          region and country.
        </p>
      ) : null}

      <label className="flex items-center gap-2 text-[11px] text-ink-100/60">
        <span>Units:</span>
        <select
          value={units}
          onChange={(e) => onPatch({ units: e.target.value })}
          className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
        >
          <option value="metric">Metric (°C, km/h)</option>
          <option value="imperial">Imperial (°F, mph)</option>
        </select>
      </label>

      {current ? (
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/60">
          Right now in{" "}
          <span className="text-ink-100/90">
            {current.location_label || weather?.location_name || "your area"}
          </span>
          : {current.description}, {Math.round(current.temperature)}°
          {current.temp_unit}
          {current.season ? ` · ${current.season}` : ""}
        </p>
      ) : syncOn && weather?.location_name ? (
        <p className="text-[11px] text-ink-100/40">
          Fetching conditions…
        </p>
      ) : null}
    </Section>
  );
}
