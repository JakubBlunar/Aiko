import { describe, expect, it } from "vitest";
import { MIST_COLORS, mistFamily, mistWeights } from "./moodMistPalette";

const mood = { label: "warm", intensity: 0.6, valence: 0.5, arousal: 0.4 };

describe("mood atmosphere", () => {
  it("blends lingering warmth with the spoken excitement at constant brightness", () => {
    const weights = mistWeights(mood, "excited", true);
    expect(weights.warmth).toBe(0.4);
    expect(weights.joy).toBe(0.6);
    expect(Object.values(weights).reduce((a, b) => a + b, 0)).toBe(1);
  });

  it("keeps the background mood after speech instead of a stale reaction", () => {
    const weights = mistWeights(mood, "sad", false);
    expect(weights.warmth).toBe(1);
    expect(weights.concern).toBe(0);
  });

  it("does not brighten when mood and reaction belong to the same family", () => {
    expect(mistWeights(mood, "friendly", true).warmth).toBe(1);
  });

  it("lets uncertainty show without losing the underlying warmth", () => {
    const weights = mistWeights(mood, "nervous", true);
    expect(weights.reflection).toBe(0.6);
    expect(weights.warmth).toBe(0.4);
    expect(mistFamily("confused")).toBe("reflection");
  });

  it("falls back safely for neutral or unknown reactions and moods", () => {
    expect(mistWeights(mood, "neutral", true).warmth).toBe(1);
    expect(mistWeights({ ...mood, label: "unknown" }, "unknown", true).reflection).toBe(1);
    expect(mistFamily("CURIOUS")).toBe("curiosity");
  });

  it("has a bounded authored palette", () => {
    expect(Object.keys(MIST_COLORS)).toHaveLength(8);
    Object.values(MIST_COLORS).forEach((color) => expect(color).toMatch(/^#[0-9a-f]{6}$/));
  });
});
