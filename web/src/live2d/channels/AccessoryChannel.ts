/**
 * AccessoryChannel — drives the persistent accessory toggles
 * (lollipop, eyeglasses, head_sunglasses, eye_color, crossed_arms)
 * shipped in Phase 4 of the Alexia expression overhaul.
 *
 * Mirrors :class:`OutfitChannel` structurally: each accessory has a
 * smoothed envelope (0 → 1), every active envelope contributes its
 * ``expression_params`` bindings additively into a per-param-id sum,
 * and the sum is written once per frame. The additive write fixes
 * the same class of "shared param stomp" bug the outfit layer
 * already solved — if two accessories ever touched the same param,
 * sequential writes would silently zero each other out.
 *
 * Mood-tier source-of-truth: ``manifest.settings.accessory_state``
 * (mirrored from ``AvatarSettings.accessory_state`` on the server).
 * The channel re-reads it on every ``onAccessoriesChange`` push, so
 * a PATCH to ``/api/avatar/accessories`` reaches the renderer one
 * WS broadcast later.
 *
 * Outfit gating: ``manifest.outfit_gated_expressions`` carries the
 * per-expression allow-list (``zs1 → ["day_clothes"]``). The
 * channel mirrors the ``ExpressionChannel`` semantics here — when
 * the active resolved outfit isn't in the allow-list, the
 * accessory's envelope is forced to 0 so the crossed-arms pose
 * silently no-ops against pajamas instead of painting outside the
 * silhouette.
 *
 * Eye-color enum: ``manifest.expression_params[yjys1]`` writes the
 * left iris's purple shift, and ``yjys2`` writes the right. The
 * enum maps to the bindings:
 *   - ``default``       → neither active
 *   - ``both_purple``   → yjys1 + yjys2 both active
 *   - ``left_purple``   → yjys1 only
 *   - ``right_purple``  → yjys2 only
 *
 * Tests live in ``AccessoryChannel.test.ts`` and lock in:
 *   - per-accessory toggle drives the corresponding param writes;
 *   - eye_color enum routes to the right halves;
 *   - outfit-gated accessory zeros out when the gate fails;
 *   - capability-gated accessory is a total no-op on rigs that
 *     don't advertise it.
 */
import { approach } from "../math";
import type { ExpressionParam } from "../../types";
import type {
  AvatarChannel,
  AvatarManifest,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

/** Cross-fade duration in seconds. Matches OutfitChannel's beat so
 * the rig feels visually consistent when both layers change at
 * once (e.g. user flips outfit and toggles glasses in the same
 * patch). */
const CROSSFADE_SECONDS = 0.6;

/** Capability-to-expression-stem map for Alexia. Mirrors the
 * Python ``_ALEXIA_EXPR_TO_CAPABILITY`` table (inverted). We only
 * encode the accessory-tier entries here; outfit caps live in
 * ``OutfitChannel`` and emotion expressions don't need a backing
 * param-write set from this channel.
 *
 * Future rigs that ship the same accessory grammar will Just Work
 * provided their backend stamps the same ``expression_params`` map
 * keyed off the same expression filenames. */
const ACCESSORY_EXPRESSION_STEMS: Record<string, string> = {
  lollipop: "bbt",
  eyeglasses: "dyj",
  head_sunglasses: "mj",
  crossed_arms: "zs1",
};

interface AccessoryEnvelopes {
  lollipop: number;
  eyeglasses: number;
  head_sunglasses: number;
  crossed_arms: number;
  /** Eye-color left half (``yjys1``). 1 when ``both_purple`` or
   * ``left_purple``, else 0. */
  eye_color_a: number;
  /** Eye-color right half (``yjys2``). 1 when ``both_purple`` or
   * ``right_purple``, else 0. */
  eye_color_b: number;
}

function makeEnvelopes(): AccessoryEnvelopes {
  return {
    lollipop: 0,
    eyeglasses: 0,
    head_sunglasses: 0,
    crossed_arms: 0,
    eye_color_a: 0,
    eye_color_b: 0,
  };
}

export class AccessoryChannel implements AvatarChannel {
  readonly name = "accessory";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  private _envelope: AccessoryEnvelopes = makeEnvelopes();
  private _hasAnyAccessory = false;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._envelope = makeEnvelopes();
    const caps = deps.manifest.capabilities ?? {};
    this._hasAnyAccessory =
      !!caps.has_lollipop ||
      !!caps.has_eyeglasses ||
      !!caps.has_head_sunglasses ||
      !!caps.has_crossed_arms ||
      !!caps.has_eye_color_a ||
      !!caps.has_eye_color_b;
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._envelope = makeEnvelopes();
    this._hasAnyAccessory = false;
  }

  /** Per-frame envelope ease + additive write. Mirrors
   * ``OutfitChannel.tickTier3`` so both layers crossfade on the
   * same Pixi tier-3 cadence. */
  tickTier3(_now: number, dt: number): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps || !this._hasAnyAccessory) {
      return;
    }
    const manifest = deps.manifest;
    const caps = manifest.capabilities ?? {};
    const accessoryState = readAccessoryState(manifest);
    const activeOutfitCap = resolveActiveOutfitCapability(deps);
    const gateMap = manifest.outfit_gated_expressions ?? {};

    const rate = dt > 0 ? dt / CROSSFADE_SECONDS : 0;
    const target = computeTargets(
      accessoryState,
      gateMap,
      activeOutfitCap,
      caps,
    );

    this._envelope.lollipop = approach(this._envelope.lollipop, target.lollipop, rate);
    this._envelope.eyeglasses = approach(this._envelope.eyeglasses, target.eyeglasses, rate);
    this._envelope.head_sunglasses = approach(
      this._envelope.head_sunglasses,
      target.head_sunglasses,
      rate,
    );
    this._envelope.crossed_arms = approach(
      this._envelope.crossed_arms,
      target.crossed_arms,
      rate,
    );
    this._envelope.eye_color_a = approach(
      this._envelope.eye_color_a,
      target.eye_color_a,
      rate,
    );
    this._envelope.eye_color_b = approach(
      this._envelope.eye_color_b,
      target.eye_color_b,
      rate,
    );

    const sums: Record<string, number> = {};
    const exprParams = manifest.expression_params ?? {};
    accumulate(sums, exprParams[ACCESSORY_EXPRESSION_STEMS.lollipop], this._envelope.lollipop);
    accumulate(sums, exprParams[ACCESSORY_EXPRESSION_STEMS.eyeglasses], this._envelope.eyeglasses);
    accumulate(
      sums,
      exprParams[ACCESSORY_EXPRESSION_STEMS.head_sunglasses],
      this._envelope.head_sunglasses,
    );
    accumulate(
      sums,
      exprParams[ACCESSORY_EXPRESSION_STEMS.crossed_arms],
      this._envelope.crossed_arms,
    );
    accumulate(sums, exprParams.yjys1, this._envelope.eye_color_a);
    accumulate(sums, exprParams.yjys2, this._envelope.eye_color_b);

    for (const paramId in sums) {
      adapter.setParam(paramId, sums[paramId]);
    }
  }

  /** Read-only view of the envelopes. Test-only. */
  get envelopeSnapshot(): Readonly<AccessoryEnvelopes> {
    return { ...this._envelope };
  }
}

interface AccessoryTargets {
  lollipop: number;
  eyeglasses: number;
  head_sunglasses: number;
  crossed_arms: number;
  eye_color_a: number;
  eye_color_b: number;
}

function computeTargets(
  state: Record<string, string | boolean>,
  gateMap: Record<string, string[]>,
  activeOutfitCap: string,
  caps: Record<string, boolean>,
): AccessoryTargets {
  const passes = (exprStem: string): boolean => {
    const allow = gateMap[exprStem];
    if (!allow || allow.length === 0) return true;
    // Permissive when the outfit hasn't been reported yet — keeps
    // the rig from stranding gated accessories during the
    // first-frame race between StoreBridge attach and the initial
    // ``avatar`` event.
    if (!activeOutfitCap) return true;
    return allow.includes(activeOutfitCap);
  };
  const toggleOn = (key: string, capFlag: string, exprStem: string): number => {
    if (!caps[capFlag]) return 0;
    const value = state[key];
    const on = value === true || value === "true" || value === "on";
    if (!on) return 0;
    return passes(exprStem) ? 1 : 0;
  };
  const eye = String(state.eye_color ?? "default").toLowerCase();
  const eyeColorA =
    caps.has_eye_color_a && (eye === "both_purple" || eye === "left_purple")
      ? 1
      : 0;
  const eyeColorB =
    caps.has_eye_color_b && (eye === "both_purple" || eye === "right_purple")
      ? 1
      : 0;
  return {
    lollipop: toggleOn("lollipop", "has_lollipop", ACCESSORY_EXPRESSION_STEMS.lollipop),
    eyeglasses: toggleOn(
      "eyeglasses",
      "has_eyeglasses",
      ACCESSORY_EXPRESSION_STEMS.eyeglasses,
    ),
    head_sunglasses: toggleOn(
      "head_sunglasses",
      "has_head_sunglasses",
      ACCESSORY_EXPRESSION_STEMS.head_sunglasses,
    ),
    crossed_arms: toggleOn(
      "crossed_arms",
      "has_crossed_arms",
      ACCESSORY_EXPRESSION_STEMS.crossed_arms,
    ),
    eye_color_a: eyeColorA,
    eye_color_b: eyeColorB,
  };
}

function readAccessoryState(
  manifest: AvatarManifest,
): Record<string, string | boolean> {
  return manifest.settings?.accessory_state ?? {};
}

function resolveActiveOutfitCapability(deps: ChannelDeps): string {
  const outfit = deps.getStoreSnapshot()?.resolvedOutfit ?? "";
  if (outfit === "day") return "day_clothes";
  return outfit;
}

function accumulate(
  sums: Record<string, number>,
  bindings: ExpressionParam[] | undefined,
  envelope: number,
): void {
  if (!bindings || envelope <= 0) {
    return;
  }
  for (const p of bindings) {
    sums[p.param_id] = (sums[p.param_id] ?? 0) + envelope * p.on_value;
  }
}
