/**
 * Production wrapper around ``pixi-live2d-display``'s ``Live2DModel``.
 *
 * The engine + every channel talks to this thin surface only. That
 * means:
 *
 * 1. We never touch ``model.internalModel.coreModel.setParameterValueById``
 *    from a channel — the adapter centralises the unsafe-cast
 *    plumbing and silently swallows missing-rig writes (consistent
 *    with how the original component handled it).
 *
 * 2. The lip-sync ``beforeModelUpdate`` listener is set up exactly
 *    once (from ``AvatarEngine.start``) instead of being scattered
 *    across two ``useEffect``s the way the old component did.
 *
 * 3. Tests use ``FakeAdapter`` with the same surface — no Pixi
 *    needed.
 *
 * See ``docs/alexia-model-notes.md`` section 5 for the call-order
 * details that motivate the ``onBeforeModelUpdate`` hook.
 */
import { MotionPriority } from "pixi-live2d-display";
import type { Live2DModel } from "pixi-live2d-display";
import type { Live2DModelAdapter } from "./types";

interface CoreModelLike {
  setParameterValueById?: (id: string, value: number) => void;
  getParameterValueById?: (id: string) => number;
}

interface InternalModelEmitter {
  coreModel?: CoreModelLike;
  motionManager?: {
    expressionManager?: {
      resetExpression?: () => void;
    };
  };
  focusController?: {
    focus: (x: number, y: number, instant?: boolean) => void;
  };
  on?: (event: string, listener: () => void) => void;
  off?: (event: string, listener: () => void) => void;
}

/** Dependencies the production adapter needs. We keep the imports
 * narrow so the test file (``__fixtures__/fake-model.ts``) doesn't
 * have to pull anything from Pixi. */
export class PixiLive2DAdapter implements Live2DModelAdapter {
  private readonly _model: InstanceType<typeof Live2DModel>;
  private readonly _coreModel: CoreModelLike | null;

  constructor(model: InstanceType<typeof Live2DModel>) {
    this._model = model;
    const internal = (model as unknown as { internalModel?: InternalModelEmitter }).internalModel;
    this._coreModel = internal?.coreModel ?? null;
  }

  setParam(paramId: string, value: number): void {
    const setter = this._coreModel?.setParameterValueById;
    if (setter) {
      try {
        setter.call(this._coreModel, paramId, value);
      } catch {
        /* swallow — missing param ids are common when capability
         * detection misses a synonym; channels gate their own
         * writes with capability flags so a stray write here
         * doesn't break the rig. */
      }
    }
  }

  getParam(paramId: string): number | undefined {
    const getter = this._coreModel?.getParameterValueById;
    if (!getter) {
      return undefined;
    }
    try {
      return getter.call(this._coreModel, paramId);
    } catch {
      return undefined;
    }
  }

  expression(name: string): void {
    try {
      this._model.expression(name);
    } catch (err) {
      console.warn(`[PixiLive2DAdapter] expression("${name}") failed`, err);
    }
  }

  resetExpression(): void {
    const internal = (this._model as unknown as { internalModel?: InternalModelEmitter })
      .internalModel;
    const reset = internal?.motionManager?.expressionManager?.resetExpression;
    if (!reset) {
      return;
    }
    try {
      reset.call(internal!.motionManager!.expressionManager);
    } catch (err) {
      console.warn("[PixiLive2DAdapter] resetExpression() failed", err);
    }
  }

  motion(group: string, index: number | undefined, priority?: number): void {
    try {
      this._model.motion(
        group,
        index,
        (priority as unknown as MotionPriority) ?? MotionPriority.NORMAL,
      );
    } catch (err) {
      console.warn(`[PixiLive2DAdapter] motion("${group}", ${index}) failed`, err);
    }
  }

  focus(x: number, y: number): void {
    const internal = (this._model as unknown as { internalModel?: InternalModelEmitter })
      .internalModel;
    const fc = internal?.focusController;
    if (!fc) {
      return;
    }
    try {
      fc.focus(x, y);
    } catch {
      /* swallow — minimal rigs without ParamAngle/EyeBall accept the
       * call but the controller drives nothing; we don't surface that. */
    }
  }

  onBeforeModelUpdate(listener: () => void): () => void {
    const internal = (this._model as unknown as { internalModel?: InternalModelEmitter })
      .internalModel;
    if (!internal?.on || !internal?.off) {
      console.warn(
        "[PixiLive2DAdapter] internalModel does not expose on/off; lipsync will not run",
      );
      return () => undefined;
    }
    internal.on("beforeModelUpdate", listener);
    return () => {
      try {
        internal.off!("beforeModelUpdate", listener);
      } catch {
        /* swallow — happens when the model has already been destroyed */
      }
    };
  }
}
