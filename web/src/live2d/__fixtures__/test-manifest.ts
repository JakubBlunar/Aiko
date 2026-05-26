/**
 * Minimal ``AvatarManifest`` builder for engine + channel tests.
 *
 * Most tests only care about a handful of fields (capabilities,
 * overlays, lip-sync ids). ``buildManifest`` returns a sane default
 * that callers can spread-override. Keeps each test file from
 * having to repeat the long ``AvatarProfile`` shape.
 */
import type { AvatarManifest } from "../types";

export function buildManifest(overrides: Partial<AvatarManifest> = {}): AvatarManifest {
  const base: AvatarManifest = {
    display_name: "Test Avatar",
    entry_filename: "test.model3.json",
    cubism_version: 3,
    expressions: [],
    motions: {},
    reaction_mapping: {},
    idle_motion_group: null,
    talk_motion_group: null,
    lip_sync_ids: ["ParamMouthOpenY"],
    eye_blink_ids: ["ParamEyeLOpen", "ParamEyeROpen"],
    parameters: [],
    parts: [],
    capabilities: {},
    overlays: {},
    outfits: {},
    expression_params: {},
    cat_tail_param_ids: [],
    cat_ear_param_ids: [],
    settings: { scale_multiplier: 1, auto_outfit: "auto", expressiveness: 1 },
    loaded: true,
  };
  return { ...base, ...overrides };
}

export const NEUTRAL_MOOD = {
  label: "content" as const,
  intensity: 0.5,
  valence: 0,
  arousal: 0.4,
};

export function buildStoreSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    reaction: "neutral",
    ttsState: "idle" as const,
    voiceMode: "off" as const,
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: NEUTRAL_MOOD,
    resolvedOutfit: "" as const,
    backchannelHint: "",
    circadianPeriod: "",
    expressiveness: 1,
    ...overrides,
  };
}
