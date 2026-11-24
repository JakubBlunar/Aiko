import type { AvatarManifest, Live2DModelAdapter } from "./types";
import type { EngineState } from "./state";
import { calibration } from "../../../tools/rig-studio/src/alexia";
import type { Clip, Key, Rig, Values } from "../../../tools/rig-studio/src/model";
import {
  clamp, gestureLayer, idleLayers, interpolate, sampleLayers, type MotionLayer,
} from "../../../tools/rig-studio/src/runtime";

/** Translate the app's Alexia channel commands at the custom player's frame boundary. */
export class RigStudioAdapter implements Live2DModelAdapter {
  private readonly listeners = new Set<() => void>();
  private readonly persistent: Values = Object.create(null);
  private frameWrites?: Values;
  private readonly last = new Map<string, number>();
  private expressionName = "";
  private readonly bindings: { raw: string; id: string; keys: Key[] }[] = [];
  private readonly neutral = new Map<string, number>();
  private activeMotion?: { clip: Clip; start: number; priority: number };
  private readonly tail?: Clip;
  private tailStarted = -Infinity;
  private lastWagDeadline = 0;
  private gaze: Values = {};

  constructor(
    private readonly rig: Rig,
    private readonly manifest: Pick<AvatarManifest, "expression_params" | "motions">,
    private readonly state: Pick<EngineState, "tailWagBoostUntil">,
    private readonly now = () => performance.now(),
  ) {
    this.tail = rig.clips.find(clip => clip.phaseGesture?.parameter === "tail.curl");
    for (const control of calibration) {
      if (!rig.parameters.some(p => p.id === control.id)) continue;
      // Multi-state selectors are decoded as a pair below. The tail's five
      // rotations cannot be inverted individually; its native phase owns it.
      if (control.id === "tail.curl") continue;
      for (const [raw, points] of Object.entries(control.source)) {
        this.neutral.set(raw,points[control.knots.indexOf(control.default)]);
        if (control.id === "outfit.style" || control.id === "eyes.style") continue;
        this.bindings.push({ raw, id: control.id, keys: points.map((v,i) => [v,control.knots[i]]) });
      }
    }
  }

  setParam(id: string, value: number) {
    if (!Number.isFinite(value) || !this.neutral.has(id)) return;
    if (this.frameWrites) this.frameWrites[id] = value;
    else if (value === this.neutral.get(id)) delete this.persistent[id];
    else this.persistent[id] = value;
    this.last.set(id,value);
  }

  getParam(id: string) { return this.last.get(id); }

  expression(name: string) {
    if (Object.hasOwn(this.manifest.expression_params ?? {},name)) this.expressionName = name;
  }

  resetExpression() { this.expressionName = ""; }

  onBeforeModelUpdate(listener: () => void) {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  focus(x: number, y: number) {
    this.gaze = x || y ? {"eyes.look.x":clamp(x,-1,1),"eyes.look.y":clamp(y,-1,1)} : {};
  }

  motion(group: string, index?: number, priority = 2) {
    const refs = this.manifest.motions[group];
    if (!refs?.length) return;
    const ref = refs[index ?? Math.floor(Math.random()*refs.length)];
    const clip = this.rig.clips.find(c => c.id === ref?.name);
    if (!clip) return;
    const start = this.now();
    const current = this.activeMotion;
    if (current && priority < 3 && priority <= current.priority &&
        start-current.start < 1000*(current.clip.phaseGesture?.duration ?? current.clip.duration)) return;
    this.activeMotion = {clip,start,priority};
  }

  /** Sample once per render. The caller can supply its existing idle and gesture clocks. */
  sampleFrame(layers?: readonly MotionLayer[], pins: Values = {}): Values {
    const time = this.now();
    const stack = layers ? [...layers] : idleLayers(this.rig,time/1000);
    if (this.tail?.phaseGesture) {
      // GestureChannel already owns this deadline. Let the native turn finish
      // if the short pulse expires or another pulse arrives while it is moving.
      const deadline = this.state.tailWagBoostUntil;
      const duration = this.tail.phaseGesture.duration*1000;
      if (deadline > time && deadline !== this.lastWagDeadline && time-this.tailStarted >= duration) {
        this.tailStarted = time;
      }
      this.lastWagDeadline = deadline;
      if (time-this.tailStarted < duration) stack.push(gestureLayer(this.tail,(time-this.tailStarted)/1000));
    }
    const motion = this.activeMotion;
    if (motion) {
      const elapsed = (time-motion.start)/1000;
      if (elapsed < (motion.clip.phaseGesture?.duration ?? motion.clip.duration)) {
        stack.push(gestureLayer(motion.clip,elapsed));
      } else this.activeMotion = undefined;
    }
    // Cubism restores its pre-model writes after drawing. Keeping them in
    // the persistent map would strand a reaction's tears or grin after reset.
    const frame: Values = Object.create(null);
    this.frameWrites = frame;
    try { for (const listener of this.listeners) listener(); }
    finally { this.frameWrites = undefined; }
    const raw = {...this.persistent};
    for (const binding of this.manifest.expression_params?.[this.expressionName] ?? []) {
      raw[binding.param_id] = (raw[binding.param_id] ?? 0) + binding.on_value;
    }
    Object.assign(raw,frame);
    const pose = {...this.gaze};
    for (const {raw:id,id:target,keys} of this.bindings) {
      if (raw[id] !== undefined) pose[target] = interpolate(keys,raw[id]);
    }
    if (raw.ParamEyeLOpen !== undefined && raw.ParamEyeROpen !== undefined) {
      pose["eyes.open"] = (raw.ParamEyeLOpen + raw.ParamEyeROpen)/2;
    }
    if (raw.Param16 !== undefined || raw.Param17 !== undefined) {
      pose["outfit.style"] = (raw.Param16 ?? 0) < 15 ? 0 : (raw.Param17 ?? 0) >= 15 ? 2 : 1;
    }
    if (raw.Param55 !== undefined || raw.Param56 !== undefined) {
      const stars = raw.Param55 ?? 0, spirals = raw.Param56 ?? 0;
      pose["eyes.style"] = Math.max(stars,spirals) < 15 ? 0 : stars > spirals ? 1 : 2;
    }
    // The app writes its baseline posture even during a bow. Keep the native
    // motion's rotation and add the app's lean or tilt to that moving pose.
    const movingAngles = ["body.pitch","head.pitch","head.yaw","head.tilt"].filter(id =>
      pose[id] !== undefined && stack.some(layer => Object.hasOwn(layer.clip.tracks,id)));
    if (movingAngles.length) {
      const base = sampleLayers(this.rig,stack);
      for (const id of movingAngles) pose[id] += base[id];
    }
    // ExpressionChannel's speech taper approaches zero asymptotically.
    // Release the rig's exclusive grin mask once it falls below 5% during speech.
    if (pose["mouth.open"] > 0 && pose["face.grin"] < .05) pose["face.grin"] = 0;
    return sampleLayers(this.rig,stack,{...pose,...pins});
  }
}
