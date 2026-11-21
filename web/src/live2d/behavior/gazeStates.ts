import { approach, clamp } from "../math";
import type { ChannelStoreSnapshot, MouseSnapshot } from "../types";
import { gazeFocusForClass, SLEEP_DEGRADE, WORLD_GAZE } from "./resolver";

export interface GazePoint {
  x: number;
  y: number;
}

interface GazeInputs {
  snapshot: ChannelStoreSnapshot;
  mouse: MouseSnapshot;
  now: number;
  dt: number;
}

interface GazeFrame extends GazeInputs {
  sleeping: boolean;
  speaking: boolean;
  listening: boolean;
  composing: boolean;
  thinking: boolean;
  listenPosture: string | undefined;
  returnGlance: boolean;
  gazeClass: string;
}

interface GazeState {
  name: string;
  matches: (frame: GazeFrame) => boolean;
  update: (target: GazePoint, frame: GazeFrame) => void;
  saccades?: false;
}

const EYE_CONTACT_Y = 0.2;

function easeToRest(target: GazePoint, { dt }: GazeFrame): void {
  const rest = gazeFocusForClass("rest");
  if (rest === "cursor") return;
  const rate = dt / 0.24;
  target.x = approach(target.x, rest.x, rate);
  target.y = approach(target.y, rest.y, rate);
}

/** The first matching state owns the target for this frame. */
const STATES: readonly GazeState[] = [
  {
    name: "sleeping",
    matches: (frame) => frame.sleeping,
    update(target, { dt }) {
      target.x = approach(target.x, 0, dt / 1.2);
      target.y = approach(target.y, -0.2, dt / 1.2);
    },
    saccades: false,
  },
  {
    name: "holdingStill",
    matches: (frame) => frame.listenPosture === "hold_still",
    update: easeToRest,
    saccades: false,
  },
  {
    name: "givingSpace",
    matches: (frame) => frame.listenPosture === "give_space",
    update: easeToRest,
  },
  {
    name: "conversation",
    matches: (frame) => frame.listening || frame.speaking,
    update(target) {
      target.x = 0;
      target.y = EYE_CONTACT_Y;
    },
  },
  {
    name: "composing",
    matches: (frame) => frame.composing,
    update(target, { now }) {
      const seconds = now / 1000;
      target.x = 0.08 * Math.sin(seconds * 0.5);
      target.y = EYE_CONTACT_Y + 0.05 * Math.sin(seconds * 0.35);
    },
  },
  {
    name: "thinking",
    matches: (frame) => frame.thinking,
    update(target, { now }) {
      const seconds = now / 1000;
      target.x = 0.35 * Math.sin(seconds * 0.6);
      target.y = 0.18 * Math.cos(seconds * 0.43) + 0.05;
    },
  },
  {
    name: "returnGlance",
    matches: (frame) => frame.returnGlance,
    update(target, { dt }) {
      target.x = approach(target.x, 0, dt / 0.18);
      target.y = approach(target.y, EYE_CONTACT_Y, dt / 0.18);
    },
    saccades: false,
  },
  {
    name: "held",
    matches: ({ gazeClass, snapshot }) =>
      gazeClass === "rest" || gazeClass === "window"
      || snapshot.liveAttentionTarget === "shared_activity",
    update(target, { gazeClass }) {
      const focus = gazeFocusForClass(gazeClass || "rest");
      target.x = focus === "cursor" ? 0 : focus.x;
      target.y = focus === "cursor" ? 0 : focus.y;
    },
  },
  {
    name: "idle",
    matches: ({ mouse, now }) =>
      !mouse.windowFocused || !(mouse.lastMoveAt > 0 && now - mouse.lastMoveAt <= 1500),
    update(target) {
      target.x *= 0.92;
      target.y *= 0.92;
    },
  },
  {
    name: "cursor",
    matches: () => true,
    update(target, { mouse }) {
      if (mouse.x == null || mouse.y == null) return;
      const rect = mouse.containerRect;
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      target.x = clamp((mouse.x - cx) / Math.max(1, mouse.viewportWidth / 2), -0.7, 0.7);
      target.y = clamp(-((mouse.y - cy) / Math.max(1, mouse.viewportHeight / 2)), -0.5, 0.7);
    },
  },
];

export class GazeStateMachine {
  private glanceId = "";
  private glanceUntil = 0;

  reset(): void {
    this.glanceId = "";
    this.glanceUntil = 0;
  }

  /** Apply the current state's motion and report whether it permits a saccade. */
  advance(target: GazePoint, inputs: GazeInputs): boolean {
    const { snapshot, now } = inputs;
    const plan = snapshot.liveEmbodiment;
    const listening = snapshot.voiceMode === "listening" || snapshot.voiceMode === "transcribing";
    const speaking = snapshot.ttsState === "speaking";
    const composing = snapshot.composing === true;
    const thinking = snapshot.voiceMode === "thinking" || (snapshot.turnInProgress && !speaking);
    const pulse = plan?.return_glance;
    if (!pulse?.id) {
      this.glanceUntil = 0;
    } else if (pulse.id !== this.glanceId) {
      this.glanceId = pulse.id;
      this.glanceUntil = now + clamp(pulse.remaining_ms, 0, 900);
    }
    const frame: GazeFrame = {
      ...inputs,
      sleeping: SLEEP_DEGRADE.has(snapshot.sleepStatus ?? "awake") || plan?.degrade_to_sleep === true,
      listening,
      speaking,
      composing,
      thinking,
      listenPosture: (listening || composing) && !speaking && !thinking
        ? plan?.listen_posture
        : undefined,
      gazeClass: plan?.gaze_class || WORLD_GAZE[(snapshot.worldActivity ?? "").toLowerCase()] || "",
      returnGlance: now < this.glanceUntil,
    };
    const state = STATES.find((candidate) => candidate.matches(frame));
    if (frame.returnGlance && state?.name !== "returnGlance") {
      // Conversation and sleep can interrupt a glance without replaying it later.
      this.glanceUntil = 0;
    }
    state?.update(target, frame);
    return state?.saccades !== false;
  }
}
