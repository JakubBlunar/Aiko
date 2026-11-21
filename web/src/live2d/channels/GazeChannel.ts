/** Apply the active gaze state and layer the channel's micro-saccade clock. */
import { GazeStateMachine } from "../behavior/gazeStates";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
  MouseSnapshot,
} from "../types";

const SACCADE_INTERVAL_MIN_MS = 1_500;
const SACCADE_INTERVAL_RANGE_MS = 1_500;
const SACCADE_DECAY = 0.92;

export interface GazeChannelOptions {
  /** Random source, defaults to ``Math.random``. Tests pass a
   * deterministic source so saccades are reproducible. */
  random?: () => number;
}

export class GazeChannel implements AvatarChannel {
  readonly name = "gaze";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;

  private readonly _target = { x: 0, y: 0 };
  private readonly _machine = new GazeStateMachine();
  private readonly _microSaccade = { x: 0, y: 0 };
  private _nextSaccadeAt = 0;
  private readonly _random: () => number;

  constructor(options: GazeChannelOptions = {}) {
    this._random = options.random ?? Math.random;
  }

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._target.x = 0;
    this._target.y = 0;
    this._microSaccade.x = 0;
    this._microSaccade.y = 0;
    this._machine.reset();
    this._nextSaccadeAt = deps.now() + this._saccadeInterval();
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._target.x = 0;
    this._target.y = 0;
    this._microSaccade.x = 0;
    this._microSaccade.y = 0;
  }

  tickGaze(now: number, dt: number, mouse: MouseSnapshot): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    const saccadesAllowed = this._machine.advance(
      this._target, { snapshot: deps.getStoreSnapshot(), mouse, now, dt },
    );
    if (saccadesAllowed && now >= this._nextSaccadeAt) {
      this._nextSaccadeAt = now + this._saccadeInterval();
      this._microSaccade.x = (this._random() - 0.5) * 0.1;
      this._microSaccade.y = (this._random() - 0.5) * 0.06;
    }
    this._microSaccade.x *= SACCADE_DECAY;
    this._microSaccade.y *= SACCADE_DECAY;

    adapter.focus(
      this._target.x + this._microSaccade.x,
      this._target.y + this._microSaccade.y,
    );
  }

  // ── test-only accessors ──────────────────────────────────────────
  get target(): { x: number; y: number } {
    return { ...this._target };
  }

  private _saccadeInterval(): number {
    return SACCADE_INTERVAL_MIN_MS + this._random() * SACCADE_INTERVAL_RANGE_MS;
  }
}
