/**
 * OutfitChannel — drives the pajamas / pajamas_hooded / day cross-
 * fade with the additive-per-param-id sum that fixes the original
 * "shared param stomp" bug.
 *
 * Background: the Alexia rig binds outfit toggles to numeric params
 * with overlapping ranges. ``pajamas`` and ``pajamas_hooded`` BOTH
 * write ``Param16=30``; only ``pajamas_hooded`` adds ``Param17=30``.
 * Sequential ``setParam(p.param_id, env*on_value)`` writes used to
 * make the *inactive* envelope (=0) silently zero out the active
 * envelope's contribution during a crossfade — the rig would
 * briefly flash to ``day`` mid-transition between the two pajamas
 * variants.
 *
 * The fix is what this channel locks in: accumulate every active
 * binding's contribution per param-id, then write the sum once.
 * During a pajamas <-> pajamas_hooded crossfade Param16 stays at
 * 30 (= 0.5*30 + 0.5*30 across the transition window) while
 * Param17 fades smoothly 0 -> 30.
 *
 * Capability gating: each binding only contributes when the
 * matching ``has_*`` capability is present on the manifest. A rig
 * without ``has_day_clothes`` can still receive an "auto" circadian
 * flip toward day at sunrise — the channel just fades the pajamas
 * envelope to 0 without touching the (missing) day binding.
 *
 * Crossfade timing: ~800ms ease (``rate = dt / 0.8``). Faster looks
 * snappy, slower looks sleepy; 800ms hits the same beat the legacy
 * implementation used and matches the human eye's expectation for
 * a wardrobe change cue.
 *
 * Tests live in ``OutfitChannel.test.ts`` and lock in:
 *   - the additive-sum invariant during a pajamas <-> hooded
 *     crossfade
 *   - day fade direction
 *   - capability gating skips the missing binding
 *   - non-outfit-capable rigs are total no-ops (no setParam at all)
 */
import { approach } from "../math";
import type { ResolvedOutfit } from "../../types";
import type {
  AvatarChannel,
  AvatarManifest,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

/** Cross-fade duration in seconds. Matches the legacy useEffect. */
const CROSSFADE_SECONDS = 0.8;

interface Envelope {
  pajamas: number;
  pajamas_hooded: number;
  day: number;
}

export class OutfitChannel implements AvatarChannel {
  readonly name = "outfit";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  private _envelope: Envelope = { pajamas: 0, pajamas_hooded: 0, day: 0 };
  private _hasAnyOutfit = false;
  // Tracked across ticks so the debug log only fires once per outfit
  // change, not every frame. ``null`` (initial) maps to "first frame
  // after attach" — we log that too so debugging captures the boot
  // outfit alongside transitions.
  private _lastObservedOutfit: ResolvedOutfit | null = null;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._envelope = { pajamas: 0, pajamas_hooded: 0, day: 0 };
    const caps = deps.manifest.capabilities ?? {};
    this._hasAnyOutfit =
      !!caps.has_pajamas || !!caps.has_pajamas_hooded || !!caps.has_day_clothes;
    this._lastObservedOutfit = null;
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._envelope = { pajamas: 0, pajamas_hooded: 0, day: 0 };
    this._hasAnyOutfit = false;
    this._lastObservedOutfit = null;
  }

  /** Per-frame outfit-envelope ease + additive write. */
  tickTier3(_now: number, dt: number): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps || !this._hasAnyOutfit) {
      return;
    }
    const manifest = deps.manifest;
    const caps = manifest.capabilities ?? {};
    const outfits = manifest.outfits ?? {};

    const resolvedOutfit: ResolvedOutfit =
      (deps.getStoreSnapshot().resolvedOutfit as ResolvedOutfit) || "";

    if (resolvedOutfit !== this._lastObservedOutfit) {
      deps.debug?.("channel.outfit", "outfitChanged", {
        from: this._lastObservedOutfit,
        to: resolvedOutfit,
      });
      this._lastObservedOutfit = resolvedOutfit;
    }

    const rate = dt / CROSSFADE_SECONDS;
    this._envelope.pajamas = approach(
      this._envelope.pajamas,
      resolvedOutfit === "pajamas" ? 1 : 0,
      rate,
    );
    this._envelope.pajamas_hooded = approach(
      this._envelope.pajamas_hooded,
      resolvedOutfit === "pajamas_hooded" ? 1 : 0,
      rate,
    );
    this._envelope.day = approach(
      this._envelope.day,
      resolvedOutfit === "day" ? 1 : 0,
      rate,
    );

    // Additive per-param-id sum — see the module-level docstring for
    // the rationale (Param16 stomp fix).
    const sums: Record<string, number> = {};
    if (caps.has_pajamas) {
      accumulate(sums, outfits.pajamas, this._envelope.pajamas);
    }
    if (caps.has_pajamas_hooded) {
      accumulate(sums, outfits.pajamas_hooded, this._envelope.pajamas_hooded);
    }
    if (caps.has_day_clothes) {
      accumulate(sums, outfits.day_clothes, this._envelope.day);
    }
    for (const paramId in sums) {
      adapter.setParam(paramId, sums[paramId]);
    }
  }

  // ── test-only accessors ──────────────────────────────────────────
  /** Read-only view of the current envelope values. Tests use this
   * to assert the crossfade is moving in the right direction without
   * having to peer at param writes (which can lump multiple
   * contributions). */
  get envelopeSnapshot(): Readonly<Envelope> {
    return { ...this._envelope };
  }
}

function accumulate(
  sums: Record<string, number>,
  binding: AvatarManifest["outfits"][string] | undefined,
  envelope: number,
): void {
  if (!binding) {
    return;
  }
  for (const p of binding.params) {
    sums[p.param_id] = (sums[p.param_id] ?? 0) + envelope * p.on_value;
  }
}
