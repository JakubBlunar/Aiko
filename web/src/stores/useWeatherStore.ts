import { create } from "zustand";
import type { WeatherSettingsSnapshot, WeatherSnapshot } from "@/types";

/**
 * H11 weather sync store. Holds the latest real-world weather snapshot
 * (drives the persona-window backdrop overlay) plus the masked settings
 * snapshot (location / units / sync toggle for the Settings drawer).
 * Kept standalone so a ``weather_updated`` frame only re-runs the
 * overlay + the Weather settings section, not the whole app.
 */
export interface WeatherSlice {
  weather: WeatherSnapshot | null;
  weatherSettings: WeatherSettingsSnapshot | null;
  setWeather: (snapshot: WeatherSnapshot | null) => void;
  setWeatherSettings: (settings: WeatherSettingsSnapshot | null) => void;
}

export const useWeatherStore = create<WeatherSlice>()((set) => ({
  weather: null,
  weatherSettings: null,
  setWeather: (snapshot) => set({ weather: snapshot }),
  setWeatherSettings: (settings) =>
    set((state) => ({
      weatherSettings: settings,
      // A settings frame can carry a fresher ``current`` snapshot; adopt
      // it when present so the overlay updates on a location change too.
      weather: settings?.current ?? state.weather,
    })),
}));
