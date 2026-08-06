/**
 * How hard the avatar is allowed to push the GPU.
 *
 * The rig was set up for a desktop: it renders at the display's native
 * pixel ratio with multisampling and no frame cap, and Cubism re-deforms
 * every mesh on each of those frames. On a phone that is a sustained
 * full-rate GPU load in a device with no fan — a 3x-DPR handset turns the
 * small floating-persona box into a ~570x750 backing buffer redrawn at
 * 90 or 120 Hz, which is enough to make the phone hot on its own.
 *
 * The budget is resolved once at Pixi setup and is deliberately dull:
 * phones get a frame cap, a pixel-ratio ceiling and a low-power GPU hint;
 * everything else is left exactly as it was. Kept as a pure function so
 * the policy is unit-testable in Node without Pixi or a DOM.
 */

/** Frames per second the avatar is allowed on a phone. Below ~24 the
 *  breathing idle reads as a stutter rather than a slower breath; 30 is
 *  the first value above that which still halves the work. */
export const MOBILE_MAX_FPS = 30;

/** Pixel-ratio ceiling on a phone. Going from 3x to 2x is a 2.25x cut in
 *  shaded pixels for a rig that is a few hundred CSS px tall, where the
 *  third sample is not visible. */
export const MOBILE_MAX_RESOLUTION = 2;

export interface RenderEnvironment {
  /** Phone-sized viewport (see ``useIsMobile``). */
  mobile: boolean;
  /** ``window.devicePixelRatio``. */
  devicePixelRatio: number;
}

export interface RenderBudget {
  /** Pixi ticker cap. ``0`` means uncapped, which is Pixi's own default
   *  and what desktop keeps. */
  maxFPS: number;
  /** Canvas backing-store scale. */
  resolution: number;
  powerPreference: "default" | "low-power" | "high-performance";
}

export function readRenderEnvironment(mobile: boolean): RenderEnvironment {
  const dpr =
    typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  return { mobile, devicePixelRatio: dpr };
}

export function resolveRenderBudget(env: RenderEnvironment): RenderBudget {
  const dpr = Math.max(1, env.devicePixelRatio || 1);
  if (!env.mobile) {
    // Desktop is unchanged on purpose: it has the thermal headroom, and
    // the avatar's liveliness is the point of the product there.
    return { maxFPS: 0, resolution: dpr, powerPreference: "default" };
  }
  return {
    maxFPS: MOBILE_MAX_FPS,
    resolution: Math.min(dpr, MOBILE_MAX_RESOLUTION),
    powerPreference: "low-power",
  };
}
