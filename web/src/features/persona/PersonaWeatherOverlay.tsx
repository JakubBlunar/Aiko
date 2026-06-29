import { useMemo } from "react";
import { useWeatherStore } from "@/stores/useWeatherStore";
import type { WeatherCondition } from "@/types";

/**
 * H11 persona-window weather backdrop. A pure-CSS ambient overlay
 * (rain streaks / snowfall / sun glow / fog / clouds / storm flicker)
 * mounted BEHIND the Live2D avatar in the persona window so the shared
 * real-world sky tints Aiko's space. Pointer-events-none + low opacity
 * so it never competes with the avatar or the HUD; absent entirely when
 * no snapshot has arrived. Driven by the ``weather_updated`` WS frame
 * via ``useWeatherStore``.
 */

const RAIN_DROPS = 28;
const SNOW_FLAKES = 26;

function tintFor(condition: string, isDay: boolean): string {
  switch (condition as WeatherCondition) {
    case "clear":
      return isDay
        ? "radial-gradient(ellipse at 50% 0%, rgba(255,214,120,0.18), transparent 65%)"
        : "radial-gradient(ellipse at 50% 0%, rgba(120,150,255,0.16), transparent 65%)";
    case "cloudy":
      return "radial-gradient(ellipse at 50% 0%, rgba(180,190,200,0.16), transparent 70%)";
    case "fog":
      return "linear-gradient(to bottom, rgba(200,205,210,0.22), transparent 80%)";
    case "rain":
      return "radial-gradient(ellipse at 50% 0%, rgba(90,120,160,0.22), transparent 70%)";
    case "snow":
      return "radial-gradient(ellipse at 50% 0%, rgba(210,225,245,0.20), transparent 70%)";
    case "storm":
      return "radial-gradient(ellipse at 50% 0%, rgba(70,80,120,0.28), transparent 70%)";
    default:
      return "transparent";
  }
}

export function PersonaWeatherOverlay() {
  const weather = useWeatherStore((s) => s.weather);

  const drops = useMemo(
    () =>
      Array.from({ length: RAIN_DROPS }, (_, i) => ({
        left: `${(i * 37) % 100}%`,
        delay: `${(i % 10) * 0.13}s`,
        duration: `${0.5 + ((i * 7) % 5) * 0.12}s`,
      })),
    [],
  );
  const flakes = useMemo(
    () =>
      Array.from({ length: SNOW_FLAKES }, (_, i) => ({
        left: `${(i * 41) % 100}%`,
        delay: `${(i % 12) * 0.4}s`,
        duration: `${3 + ((i * 5) % 4)}s`,
        drift: `${((i % 5) - 2) * 12}px`,
      })),
    [],
  );

  if (!weather || !weather.condition) return null;
  const condition = String(weather.condition);
  const isDay = Boolean(weather.is_day);

  return (
    <div
      className="pointer-events-none absolute inset-0 z-0 overflow-hidden"
      aria-hidden="true"
      data-weather={condition}
    >
      <div
        className="absolute inset-0"
        style={{ background: tintFor(condition, isDay) }}
      />

      {condition === "rain" || condition === "storm" ? (
        <div className="absolute inset-0">
          {drops.map((d, i) => (
            <span
              key={i}
              className="weather-rain-drop"
              style={{
                left: d.left,
                animationDelay: d.delay,
                animationDuration: d.duration,
              }}
            />
          ))}
        </div>
      ) : null}

      {condition === "snow" ? (
        <div className="absolute inset-0">
          {flakes.map((f, i) => (
            <span
              key={i}
              className="weather-snow-flake"
              style={
                {
                  left: f.left,
                  animationDelay: f.delay,
                  animationDuration: f.duration,
                  "--drift": f.drift,
                } as React.CSSProperties
              }
            />
          ))}
        </div>
      ) : null}

      {condition === "fog" || condition === "cloudy" ? (
        <div className="weather-fog absolute inset-x-0 top-0 h-2/3" />
      ) : null}

      {condition === "storm" ? <div className="weather-storm-flash absolute inset-0" /> : null}
    </div>
  );
}
