/**
 * LipsyncChannel — drives the mouth-open params from the broadcast
 * audio amplitude.
 *
 * Critical timing constraint: this channel writes ``ParamMouthOpenY``
 * inside ``tickPreModel`` (i.e. ``internalModel.on('beforeModelUpdate',
 * ...)`` on the underlying rig). That hook is the only safe place to
 * write the mouth — Cubism4InternalModel.update() runs in this order:
 *
 *     emit("beforeMotionUpdate")
 *     motionManager.update()        ← talk/idle motions write
 *                                     ParamMouthOpenY HERE if the
 *                                     .motion3.json has mouth keyframes
 *     emit("afterMotionUpdate")
 *     coreModel.saveParameters()    ← per-frame snapshot
 *     expressionManager.update()
 *     eyeBlink / focus / breath / physics / pose
 *     emit("beforeModelUpdate")     ← WE HOOK HERE
 *     coreModel.update()            ← renders this frame
 *     coreModel.loadParameters()    ← restores from snapshot
 *
 * Writing on ``tickTier3`` (a plain RAF outside the rig) would happen
 * BEFORE ``motionManager.update()``, so any talk motion with mouth
 * keyframes would silently overwrite the lip-sync at step 2 — visible
 * as the mouth freezing during TTS. Writing in ``beforeModelUpdate``
 * means our amplitude is the value rendered in step 8 regardless of
 * what motion or expression wrote earlier.
 *
 * See ``docs/alexia-model-notes.md`` §5 for the deeper context.
 *
 * Smoothing: target is the latest broadcast amplitude in [0, 1]; we
 * critically-damp toward it with ``factor = 0.35`` per frame. The
 * factor matches the legacy ``Live2DAvatar.tsx`` implementation —
 * raw 30 Hz amplitude updates would otherwise step-look on a 60 Hz
 * canvas.
 *
 * Param targeting: prefer ``manifest.lip_sync_ids`` (the rig's
 * declared LipSync group). Fall back to ``ParamMouthOpenY`` for
 * Cubism 4 or ``PARAM_MOUTH_OPEN_Y`` for Cubism 2 when the rig
 * doesn't declare any.
 */
import { clamp } from "../math";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

const MOUTH_PARAM_CUBISM_4 = "ParamMouthOpenY";
const MOUTH_PARAM_CUBISM_2 = "PARAM_MOUTH_OPEN_Y";
/** Critically-damped smoothing factor per frame. Matches the legacy
 * useEffect (0.35 ≈ ~150ms time constant at 60Hz). */
const SMOOTH_FACTOR = 0.35;

export class LipsyncChannel implements AvatarChannel {
  readonly name = "lipsync";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  private _smoothed = 0;
  private _paramIds: string[] = [];

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._smoothed = 0;
    this._paramIds = resolveLipSyncParams(deps);
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._smoothed = 0;
    this._paramIds = [];
  }

  /** Pre-model update — runs once per Pixi frame, immediately before
   * the rig commits parameters to the model. Only writes here. */
  tickPreModel(): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps || this._paramIds.length === 0) {
      return;
    }
    const target = deps.getStoreSnapshot().audioAmplitude || 0;
    this._smoothed = clamp(
      this._smoothed + (target - this._smoothed) * SMOOTH_FACTOR,
      0,
      1,
    );
    for (const paramId of this._paramIds) {
      adapter.setParam(paramId, this._smoothed);
    }
  }

  /** Test-only accessor for the smoothed value. */
  get smoothedAmplitude(): number {
    return this._smoothed;
  }
}

function resolveLipSyncParams(deps: ChannelDeps): string[] {
  const declared = deps.manifest.lip_sync_ids;
  if (declared && declared.length > 0) {
    return [...declared];
  }
  return [
    deps.manifest.cubism_version === 2 ? MOUTH_PARAM_CUBISM_2 : MOUTH_PARAM_CUBISM_4,
  ];
}
