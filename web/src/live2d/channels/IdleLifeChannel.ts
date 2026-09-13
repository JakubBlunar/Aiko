/**
 * IdleLifeChannel — H10 autonomous idle-life on the avatar.
 *
 * Consumes world activity/posture plus the semantic Live embodiment
 * plan. Writes body-angle and breath envelopes through tickPreModel
 * (same hook as AmbientBodyChannel). Sleep statuses degrade to the
 * SleepChannel: this channel writes nothing then.
 *
 * Conversation lock / composing / TTS reuse the existing GazeChannel
 * and AmbientBodyChannel reflexes — this channel eases its envelopes
 * to zero while those own the floor so they are not overwritten.
 *
 * Param IDs live only in ``behavior/resolver.ts``.
 */
import { approach } from "../math";
import {
  bodyDeltaForClass,
  breathHzForClass,
  classesFromWorld,
  expressionNameForClass,
  rigCapabilitiesFromManifest,
  SLEEP_DEGRADE,
  type RigCapabilities,
} from "../behavior/resolver";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

const BODY_Y = "ParamBodyAngleY";
const BODY_Z = "ParamBodyAngleZ";
const BREATH = "ParamBreath";
const ENVELOPE_TIME_S = 0.7;
const LISTENING_VOICE_MODES = new Set(["listening", "transcribing"]);

export class IdleLifeChannel implements AvatarChannel {
  readonly name = "idle-life";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  private _caps: RigCapabilities | null = null;
  private _bodyY = 0;
  private _bodyZ = 0;
  private _lastAt = 0;
  private _lastExpression: string | null = null;
  private _lastWrittenY: number | undefined = undefined;
  private _lastWrittenZ: number | undefined = undefined;
  private _wasWriting = false;
  private _motionCancelled = "no_authored_idle_life_motion";

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._caps = rigCapabilitiesFromManifest(deps.manifest);
    this._bodyY = 0;
    this._bodyZ = 0;
    this._lastAt = deps.now();
    this._lastExpression = null;
    this._wasWriting = false;
    this._lastWrittenY = undefined;
    this._lastWrittenZ = undefined;
  }

  detach(): void {
    this._cancel("detach");
    this._adapter = null;
    this._deps = null;
    this._caps = null;
    this._lastAt = 0;
    this._lastExpression = null;
    this._wasWriting = false;
  }

  tickPreModel(): void {
    const adapter = this._adapter;
    const deps = this._deps;
    const caps = this._caps;
    if (!adapter || !deps || !caps) {
      return;
    }
    const now = deps.now();
    const dt =
      this._lastAt > 0
        ? Math.max(0, Math.min(0.25, (now - this._lastAt) / 1000))
        : 0;
    this._lastAt = now;
    const snap = deps.getStoreSnapshot();
    const sleep = snap.sleepStatus ?? "awake";
    const plan = snap.liveEmbodiment ?? null;
    const degradeSleep =
      SLEEP_DEGRADE.has(sleep) || Boolean(plan?.degrade_to_sleep);
    const floorTaken =
      LISTENING_VOICE_MODES.has(snap.voiceMode)
      || snap.ttsState === "speaking"
      || snap.composing === true
      || snap.turnInProgress === true;
    if (degradeSleep || floorTaken) {
      this._easeToZero(dt);
      this._write(adapter, caps, now, { breathClass: "none", active: false });
      return;
    }
    const world = classesFromWorld(
      snap.worldActivity ?? "",
      snap.worldPosture ?? "",
    );
    const bodyClass = plan?.body_class && plan.body_class !== "none"
      ? plan.body_class
      : world.bodyClass;
    const breathClass = plan?.breath_class && plan.breath_class !== "none"
      ? plan.breath_class
      : world.breathClass;
    const expressionClass = plan?.expression_class ?? "none";
    const delta = bodyDeltaForClass(bodyClass);
    this._bodyY = approach(this._bodyY, delta.y, dt / ENVELOPE_TIME_S);
    this._bodyZ = approach(this._bodyZ, delta.z, dt / ENVELOPE_TIME_S);
    this._write(adapter, caps, now, { breathClass, active: true });
    this._maybeExpression(adapter, deps, expressionClass);
  }

  /** Test helper: last motion cancellation reason. */
  get motionCancelReason(): string {
    return this._motionCancelled;
  }

  private _easeToZero(dt: number): void {
    this._bodyY = approach(this._bodyY, 0, dt / ENVELOPE_TIME_S);
    this._bodyZ = approach(this._bodyZ, 0, dt / ENVELOPE_TIME_S);
  }

  private _write(
    adapter: Live2DModelAdapter,
    caps: RigCapabilities,
    now: number,
    opts: { breathClass: string; active: boolean },
  ): void {
    const writing =
      opts.active
      || Math.abs(this._bodyY) > 0.05
      || Math.abs(this._bodyZ) > 0.05;
    if (!writing && !this._wasWriting) {
      return;
    }
    this._wasWriting = writing;
    if (caps.canOrientY) {
      const current = adapter.getParam(BODY_Y);
      const base =
        current === this._lastWrittenY || current === undefined ? 0 : current;
      const next = base + this._bodyY;
      adapter.setParam(BODY_Y, next);
      this._lastWrittenY = next;
    }
    if (caps.canOrientZ) {
      const current = adapter.getParam(BODY_Z);
      const base =
        current === this._lastWrittenZ || current === undefined ? 0 : current;
      const next = base + this._bodyZ;
      adapter.setParam(BODY_Z, next);
      this._lastWrittenZ = next;
    }
    if (caps.canBreathe && opts.active && opts.breathClass !== "none") {
      const hz = breathHzForClass(opts.breathClass);
      const t = now / 1000;
      const value = 0.5 + 0.18 * Math.sin(2 * Math.PI * hz * t);
      adapter.setParam(BREATH, value);
    }
  }

  private _maybeExpression(
    adapter: Live2DModelAdapter,
    deps: ChannelDeps,
    expressionClass: string,
  ): void {
    const caps = this._caps;
    if (!caps?.canExpress) {
      return;
    }
    if (deps.engineState.exprSlotLockUntil > deps.now()) {
      return;
    }
    const snap = deps.getStoreSnapshot();
    const reaction = (snap.reaction || "neutral").toLowerCase();
    if (reaction !== "neutral" && reaction !== "content" && reaction !== "") {
      return;
    }
    const name = expressionNameForClass(expressionClass, deps.manifest);
    if (!name || name === this._lastExpression) {
      return;
    }
    adapter.expression(name);
    this._lastExpression = name;
  }

  private _cancel(reason: string): void {
    this._motionCancelled = reason;
    const adapter = this._adapter;
    const caps = this._caps;
    if (!adapter || !caps) {
      return;
    }
    if (caps.canOrientY) adapter.setParam(BODY_Y, 0);
    if (caps.canOrientZ) adapter.setParam(BODY_Z, 0);
    if (caps.canBreathe) adapter.setParam(BREATH, 0);
    this._bodyY = 0;
    this._bodyZ = 0;
    this._lastWrittenY = undefined;
    this._lastWrittenZ = undefined;
  }
}
