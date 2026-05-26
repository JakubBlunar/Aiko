/**
 * In-memory ``Live2DModelAdapter`` stand-in for channel unit tests.
 *
 * Records every call (param writes, expression(), motion(), reset…)
 * so tests can assert exact behaviour without spinning up Pixi. Also
 * exposes ``triggerBeforeModelUpdate()`` which fires every registered
 * pre-update listener — that is the hook the real adapter wires
 * through ``internalModel.on('beforeModelUpdate', ...)``.
 *
 * The fake intentionally skips bounds checking and capability gating
 * because that's the *channel's* responsibility — exposing every
 * write here is what lets us assert e.g. "the gesture wrote
 * ParamEyeLOpen=0 for the gesture lifetime, then released back to 1".
 */
import type { Live2DModelAdapter } from "../types";

export interface RecordedSetParam {
  paramId: string;
  value: number;
  /** Monotonic call index so tests can assert ordering without
   * having to inject a clock. */
  seq: number;
}

export class FakeAdapter implements Live2DModelAdapter {
  /** Latest value seen for each param. ``setParam`` overwrites; the
   * full history is kept in ``setParamHistory`` if a test needs to
   * walk the timeline. */
  readonly params: Map<string, number> = new Map();
  readonly setParamHistory: RecordedSetParam[] = [];
  readonly expressionCalls: string[] = [];
  readonly motionCalls: Array<{
    group: string;
    index: number | undefined;
    priority?: number;
  }> = [];
  resetExpressionCount = 0;
  readonly focusCalls: Array<{ x: number; y: number }> = [];

  private _seq = 0;
  private readonly _preUpdateListeners: Array<() => void> = [];

  setParam(paramId: string, value: number): void {
    this.params.set(paramId, value);
    this.setParamHistory.push({ paramId, value, seq: this._seq++ });
  }

  getParam(paramId: string): number | undefined {
    return this.params.get(paramId);
  }

  expression(name: string): void {
    this.expressionCalls.push(name);
  }

  resetExpression(): void {
    this.resetExpressionCount += 1;
  }

  motion(group: string, index: number | undefined, priority?: number): void {
    this.motionCalls.push({ group, index, priority });
  }

  focus(x: number, y: number): void {
    this.focusCalls.push({ x, y });
  }

  onBeforeModelUpdate(listener: () => void): () => void {
    this._preUpdateListeners.push(listener);
    return () => {
      const idx = this._preUpdateListeners.indexOf(listener);
      if (idx >= 0) {
        this._preUpdateListeners.splice(idx, 1);
      }
    };
  }

  /** Test-only helper: synchronously fire every registered
   * before-model-update listener in registration order. The real
   * pixi-live2d-display path fires these once per frame just before
   * the model writes parameters into the rig. */
  triggerBeforeModelUpdate(): void {
    for (const listener of [...this._preUpdateListeners]) {
      listener();
    }
  }

  /** Test-only helper: clear every recording so a single test can
   * walk through multiple "phases" without leaking earlier state. */
  reset(): void {
    this.params.clear();
    this.setParamHistory.length = 0;
    this.expressionCalls.length = 0;
    this.motionCalls.length = 0;
    this.resetExpressionCount = 0;
    this.focusCalls.length = 0;
    this._seq = 0;
  }
}
