import type { MoodState } from "@/types";

// Every reaction maps to an atmosphere, not a new rig expression. Shared
// colors make a transition within one family quiet rather than rainbow-like.
export const MIST_COLORS = {
  warmth: "#e6b489",
  joy: "#ebc678",
  play: "#c39cdd",
  curiosity: "#83c8c3",
  reflection: "#99afd5",
  tenderness: "#dba5b9",
  concern: "#929dc5",
  rest: "#9fa5be",
} as const;

export type MistColor = keyof typeof MIST_COLORS;

const FAMILIES: Record<string, MistColor> = {
  warm: "warmth", content: "warmth", friendly: "warmth", calm: "warmth",
  cheerful: "joy", happy: "joy", excited: "joy", enthusiastic: "joy", proud: "joy",
  playful: "play", amused: "play", mischievous: "play", smug: "play",
  curious: "curiosity", surprised: "curiosity", focused: "curiosity",
  thoughtful: "reflection", wistful: "reflection", melancholy: "reflection",
  confused: "reflection", nervous: "reflection", serious: "reflection",
  tender: "tenderness", affectionate: "tenderness", blush: "tenderness", shy: "tenderness",
  gentle: "tenderness",
  concerned: "concern", sad: "concern", cry: "concern", pouty: "concern",
  sulky: "concern", angry: "concern", restless: "concern", embarrassed: "tenderness",
  frustrated: "concern", defiant: "concern",
  tired: "rest", sleepy: "rest",
};

export function mistFamily(label: string): MistColor | undefined {
  return FAMILIES[label.toLowerCase()];
}

/** Residual mood stays visible under a sentence reaction. Weights always
 * sum to one, keeping two colors as quiet as one. Unknown reactions retain
 * the mood rather than inventing a feeling. */
export function mistWeights(mood: MoodState, reaction: string, speaking: boolean) {
  const weights = Object.fromEntries(
    Object.keys(MIST_COLORS).map((key) => [key, 0]),
  ) as Record<MistColor, number>;
  const base = mistFamily(mood.label) ?? "reflection";
  const foreground = speaking ? mistFamily(reaction) : undefined;
  weights[base] = foreground ? 0.4 : 1;
  if (foreground) weights[foreground] += 0.6;
  return weights;
}
