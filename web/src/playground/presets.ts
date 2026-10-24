import type { AvatarProfile, MoodState } from "@/types";

export interface PlaygroundProfile extends AvatarProfile {
  playground_expressions: Record<string, string>;
  playground_reactions: string[];
}

export interface MoodPreset {
  title: string;
  description: string;
  mood: MoodState;
  reaction: string;
}

export const PRESETS: MoodPreset[] = [
  { title: "Warm + excited", description: "Something good is about to happen.",
    mood: { label: "warm", intensity: 0.65, valence: 0.6, arousal: 0.6 }, reaction: "excited" },
  { title: "Tender", description: "A quiet moment of closeness.",
    mood: { label: "tender", intensity: 0.65, valence: 0.6, arousal: 0.3 }, reaction: "tender" },
  { title: "Thoughtful", description: "Taking a moment to find the right words.",
    mood: { label: "content", intensity: 0.5, valence: 0.1, arousal: 0.35 }, reaction: "thoughtful" },
  { title: "Playful", description: "Warmth with a little mischief.",
    mood: { label: "warm", intensity: 0.6, valence: 0.6, arousal: 0.7 }, reaction: "mischievous" },
  { title: "Warm + uncertain", description: "Still caring, even without all the answers.",
    mood: { label: "warm", intensity: 0.55, valence: 0.4, arousal: 0.5 }, reaction: "nervous" },
  { title: "Winding down", description: "The last gentle moment of the evening.",
    mood: { label: "tired", intensity: 0.5, valence: 0.1, arousal: 0.15 }, reaction: "calm" },
];

export const MOOD_LABELS = [
  "content", "warm", "tender", "playful", "curious", "focused",
  "concerned", "melancholy", "restless", "tired",
];

export function pretty(value: string): string {
  return value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
