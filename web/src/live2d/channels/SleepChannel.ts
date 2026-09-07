/** Persisted sleep embodiment: closed eyes and quieter body motion. */
import { approach } from "../math";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

const LEFT_EYE = "ParamEyeLOpen";
const RIGHT_EYE = "ParamEyeROpen";
const BREATH = "ParamBreath";
const BODY_Y = "ParamBodyAngleY";
const BODY_Z = "ParamBodyAngleZ";

export class SleepChannel implements AvatarChannel {
  readonly name = "sleep";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  private _amount = 0;
  private _lastAt = 0;
  private _eyeIds: string[] = [];
  private _hasBreath = false;
  private _hasBodyY = false;
  private _hasBodyZ = false;
  private _wasWriting = false;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._amount = 0;
    this._lastAt = deps.now();
    const ids = new Set((deps.manifest.parameters ?? []).map((param) => param.id));
    this._eyeIds = (deps.manifest.eye_blink_ids ?? []).filter((id) => ids.has(id));
    if (this._eyeIds.length === 0 && deps.manifest.capabilities?.has_wink) {
      this._eyeIds = [LEFT_EYE, RIGHT_EYE].filter((id) => ids.has(id));
    }
    this._hasBreath = ids.has(BREATH);
    this._hasBodyY = ids.has(BODY_Y);
    this._hasBodyZ = ids.has(BODY_Z);
    this._wasWriting = false;
  }

  detach(): void {
    const adapter = this._adapter;
    if (adapter) {
      for (const id of this._eyeIds) {
        adapter.setParam(id, 1);
      }
      if (this._hasBreath) adapter.setParam(BREATH, 0);
      if (this._hasBodyY) adapter.setParam(BODY_Y, 0);
      if (this._hasBodyZ) adapter.setParam(BODY_Z, 0);
    }
    this._adapter = null;
    this._deps = null;
    this._eyeIds = [];
    this._amount = 0;
    this._lastAt = 0;
    this._wasWriting = false;
  }

  tickTier3(now: number, dt: number): void {
    this._advance(now, dt);
    this._write(now);
  }

  tickPreModel(): void {
    const deps = this._deps;
    if (!deps) return;
    const now = deps.now();
    const dt = this._lastAt > 0 ? Math.max(0, (now - this._lastAt) / 1000) : 0;
    this._advance(now, dt);
    this._write(now);
  }

  private _advance(now: number, dt: number): void {
    const status = this._deps?.getStoreSnapshot().sleepStatus ?? "awake";
    const target =
      status === "asleep" ? 1 : status === "winding_down" ? 0.55 : 0;
    const seconds = target > this._amount ? 1.4 : status === "woken" ? 3.5 : 1.8;
    this._amount = approach(this._amount, target, dt / seconds);
    this._lastAt = now;
  }

  private _write(now: number): void {
    const adapter = this._adapter;
    if (!adapter) return;
    if (this._amount <= 0.001) {
      if (this._wasWriting) {
        for (const id of this._eyeIds) adapter.setParam(id, 1);
        if (this._hasBreath) adapter.setParam(BREATH, 0);
        if (this._hasBodyY) adapter.setParam(BODY_Y, 0);
        if (this._hasBodyZ) adapter.setParam(BODY_Z, 0);
        this._wasWriting = false;
      }
      return;
    }
    this._wasWriting = true;
    for (const id of this._eyeIds) {
      adapter.setParam(id, Math.max(0, 1 - this._amount));
    }
    const t = now / 1000;
    if (this._hasBreath) {
      const current = adapter.getParam(BREATH) ?? 0.5;
      const sleeping = 0.12 + Math.sin(t * Math.PI * 0.32) * 0.025;
      adapter.setParam(
        BREATH,
        current * (1 - this._amount) + sleeping * this._amount,
      );
    }
    const motionScale = 1 - this._amount * 0.85;
    if (this._hasBodyY) {
      adapter.setParam(BODY_Y, (adapter.getParam(BODY_Y) ?? 0) * motionScale);
    }
    if (this._hasBodyZ) {
      adapter.setParam(BODY_Z, (adapter.getParam(BODY_Z) ?? 0) * motionScale);
    }
  }
}
