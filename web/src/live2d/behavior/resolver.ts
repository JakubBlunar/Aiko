/**
 * Frontend LiveBehaviorResolver — the only Live2D-aware mapper.
 *
 * Python emits semantic classes (``settle``, ``window``, ``slow``).
 * This file is the only place Live idle-life maps those onto Param
 * IDs, focus coordinates, expression names, or motion groups.
 * Capability-absent rigs degrade to no-op deltas.
 */
import type { AvatarManifest } from "../types";

export const SLEEP_DEGRADE = new Set(["asleep", "winding_down", "woken"]);

export const WORLD_BODY: Record<string, string> = {
  reading: "settle",
  napping: "slump",
  looking_outside: "open",
  watching_screens: "settle",
  tinkering: "lean_in",
  stretching: "perk",
  snacking: "settle",
  doodling: "settle",
  thinking: "settle",
};

export const WORLD_GAZE: Record<string, string> = {
  looking_outside: "window",
  watching_screens: "rest",
  reading: "rest",
  napping: "rest",
  doodling: "rest",
};

export const WORLD_BREATH: Record<string, string> = {
  napping: "quiet",
  reading: "slow",
  thinking: "slow",
};

export const POSTURE_BODY: Record<string, string> = {
  lying: "slump",
  curled_up: "settle",
  leaning: "lean_in",
};

const BODY_Y: Record<string, number> = {
  none: 0,
  lean_in: 5,
  settle: -2.5,
  slump: -4,
  perk: 3,
  open: 1.5,
};

const BODY_Z: Record<string, number> = {
  none: 0,
  lean_in: 0,
  settle: 1,
  slump: 0,
  perk: 2,
  open: 2.5,
};

const BREATH_HZ: Record<string, number> = {
  none: 0.22,
  normal: 0.22,
  slow: 0.14,
  quiet: 0.1,
};

const GAZE_FOCUS: Record<string, { x: number; y: number }> = {
  rest: { x: 0, y: 0 },
  window: { x: 0.42, y: 0.22 },
  user_eye_contact: { x: 0, y: 0.2 },
  none: { x: 0, y: 0 },
};

export interface SemanticClasses {
  gazeClass: string;
  bodyClass: string;
  breathClass: string;
}

export function classesFromWorld(
  activity: string,
  posture: string,
): SemanticClasses {
  const act = (activity || "").trim().toLowerCase();
  const pose = (posture || "").trim().toLowerCase();
  return {
    gazeClass: WORLD_GAZE[act] ?? "cursor_follow",
    bodyClass: WORLD_BODY[act] ?? POSTURE_BODY[pose] ?? "none",
    breathClass: WORLD_BREATH[act] ?? "normal",
  };
}

export function gazeFocusForClass(
  gazeClass: string,
): { x: number; y: number } | "cursor" {
  if (gazeClass === "cursor_follow") {
    return "cursor";
  }
  return GAZE_FOCUS[gazeClass] ?? GAZE_FOCUS.rest;
}

export function bodyDeltaForClass(bodyClass: string): { y: number; z: number } {
  return {
    y: BODY_Y[bodyClass] ?? 0,
    z: BODY_Z[bodyClass] ?? 0,
  };
}

export function breathHzForClass(breathClass: string): number {
  return BREATH_HZ[breathClass] ?? BREATH_HZ.normal;
}

export interface RigCapabilities {
  canOrientY: boolean;
  canOrientZ: boolean;
  canBreathe: boolean;
  canExpress: boolean;
  canMotion: boolean;
}

export function rigCapabilitiesFromManifest(
  manifest: AvatarManifest,
): RigCapabilities {
  const flags = manifest.capabilities ?? {};
  return {
    canOrientY: Boolean(flags.has_body_angle_y),
    canOrientZ: Boolean(flags.has_body_angle_z),
    canBreathe: Boolean(flags.has_breath),
    canExpress: (manifest.expressions?.length ?? 0) > 0
      || Object.keys(manifest.reaction_mapping ?? {}).length > 0,
    canMotion: Boolean(manifest.idle_motion_group)
      || Object.keys(manifest.motions ?? {}).length > 0,
  };
}

export function expressionNameForClass(
  expressionClass: string,
  manifest: AvatarManifest,
): string | null {
  if (!expressionClass || expressionClass === "none") {
    return null;
  }
  const mapping = manifest.reaction_mapping ?? {};
  const aliases: Record<string, string[]> = {
    attentive: ["thoughtful", "neutral", "content"],
    content: ["content", "warm", "neutral"],
    drowsy: ["tired", "sleepy", "neutral"],
  };
  for (const key of aliases[expressionClass] ?? [expressionClass]) {
    const name = mapping[key];
    if (name) {
      return name;
    }
  }
  return null;
}
