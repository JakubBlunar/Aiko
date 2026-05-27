/**
 * MotionChannel — handles all ``model.motion(group, index)`` calls.
 *
 * Two triggers today:
 *
 * 1. **LLM-driven** ``[[motion:X]]`` tags. The session controller
 *    parses the tag, looks up its (group, index) on the avatar
 *    profile, and broadcasts an ``avatarMotion`` object on the WS.
 *    The store reflects the latest ref; the engine forwards it via
 *    ``onMotion``.
 *
 *    The channel checks ``manifest.motions[group]`` exists before
 *    firing — Alexia rigs fall back to silently doing nothing for
 *    unknown groups, matching the behaviour of the original
 *    ``Live2DAvatar.tsx`` useEffect.
 *
 * 2. **Talk-motion auto-start**. When TTS transitions ``idle ->
 *    speaking`` and ``manifest.talk_motion_group`` is set, the
 *    channel fires a random talk motion at NORMAL priority. We
 *    pass ``undefined`` for the index — pixi-live2d-display picks
 *    a random one in the group. The original useEffect did the
 *    same; the channel preserves the wire-level behaviour exactly.
 *
 * Idle cadence is *not* handled here — that's the AmbientBodyChannel's
 * job because it depends on mood + voice-mode gating that has nothing
 * to do with talk motions or LLM motion tags. Splitting them keeps
 * each channel's responsibilities crisp.
 *
 * Priority handling: the Pixi library's MotionPriority enum is just
 * ``IDLE = 1, NORMAL = 2, FORCE = 3``. The channel accepts a numeric
 * priority through the optional adapter argument and falls back to
 * NORMAL (2) when the caller doesn't specify one. This keeps the
 * adapter free of pixi-live2d-display imports for tests.
 */
import type { AvatarMotionState } from "../../types";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

/** Mirror of pixi-live2d-display's ``MotionPriority``. Defined locally
 * so test code doesn't import the Pixi package. */
export const MOTION_PRIORITY = {
  IDLE: 1,
  NORMAL: 2,
  FORCE: 3,
} as const;

export class MotionChannel implements AvatarChannel {
  readonly name = "motion";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  /** Tracks the last avatarMotion fired so a duplicate ref (which the
   * engine de-dupes by reference identity in the store, but we still
   * defend against here in case dispatchMotion is invoked manually
   * with the same object) doesn't fire twice. */
  private _lastMotionFiredAt: number = 0;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._lastMotionFiredAt = 0;
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._lastMotionFiredAt = 0;
  }

  onMotion(event: AvatarMotionState): void {
    const adapter = this._adapter;
    const manifest = this._deps?.manifest;
    if (!adapter || !manifest) {
      return;
    }
    if (event.firedAt === this._lastMotionFiredAt) {
      return;
    }
    this._lastMotionFiredAt = event.firedAt;
    if (!event.group) {
      return;
    }
    // Silently no-op when the group is missing on the rig — matches
    // the original useEffect's "best-effort" stance and keeps the
    // channel resilient to slightly-mismatched profiles between
    // backend and frontend.
    if (!manifest.motions || !manifest.motions[event.group]) {
      return;
    }
    adapter.motion(event.group, event.index, motionPriority(event.priority));
    this._deps?.debug?.("channel.motion", "trigger", {
      name: event.name,
      group: event.group,
      index: event.index,
      priority: event.priority ?? "normal",
    });
  }

  onTtsState(next: "idle" | "speaking"): void {
    if (next !== "speaking") {
      return;
    }
    const adapter = this._adapter;
    const manifest = this._deps?.manifest;
    if (!adapter || !manifest) {
      return;
    }
    const group = manifest.talk_motion_group;
    if (!group) {
      return;
    }
    if (!manifest.motions || !manifest.motions[group]) {
      return;
    }
    // ``undefined`` lets pixi-live2d-display pick a random index in
    // the group — exactly what the legacy useEffect did.
    adapter.motion(group, undefined, MOTION_PRIORITY.NORMAL);
    this._deps?.debug?.("channel.motion", "talkStart", { group });
  }
}

/** Map the optional ``priority`` lane on an ``AvatarMotionState`` to
 * the numeric pixi-live2d-display priority. Default ``"normal"`` so
 * every existing (priority-less) ``[[motion:X]]`` event behaves
 * exactly as before this knob was added. */
function motionPriority(value: AvatarMotionState["priority"]): number {
  switch (value) {
    case "idle":
      return MOTION_PRIORITY.IDLE;
    case "force":
      return MOTION_PRIORITY.FORCE;
    case "normal":
    case undefined:
      return MOTION_PRIORITY.NORMAL;
    default:
      return MOTION_PRIORITY.NORMAL;
  }
}
