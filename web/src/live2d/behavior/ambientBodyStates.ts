import type { ChannelStoreSnapshot } from "../types";

const BLUSH_MOODS = new Set(["tender", "warm"]);
const SWEAT_LABELS = new Set(["concerned", "confused", "frustrated"]);
const TIRED_MOODS = new Set(["tired", "exhausted"]);

const STANCE = {
  resting: { lean: 0 },
  listening: { lean: 1 },
  quietListening: { lean: 0 },
} as const;

const TAIL = {
  steady: { frequency: 1, amplitude: 1 },
  wagging: { frequency: 1.8, amplitude: 1.5 },
  sleeping: { frequency: 0.3, amplitude: 0.3 },
} as const;

const BREATH = {
  steady: { frequency: 1, amplitude: 1 },
  boosted: { frequency: 2.5, amplitude: 1.5 },
  muted: { frequency: 1, amplitude: 0 },
} as const;

export function isTailWagActive(now: number, until: number): boolean {
  return until > 0 && now < until;
}

function stanceFor(snapshot: ChannelStoreSnapshot): keyof typeof STANCE {
  const listening = snapshot.voiceMode === "listening" || snapshot.voiceMode === "transcribing"
    || snapshot.composing === true;
  if (!listening) return "resting";
  const posture = snapshot.liveEmbodiment?.listen_posture;
  return posture === "give_space" || posture === "hold_still" ? "quietListening" : "listening";
}

export function ambientBodyTargets(snapshot: ChannelStoreSnapshot, arousal: number) {
  const mood = (snapshot.mood?.label || "").toLowerCase();
  const reaction = (snapshot.reaction || "").toLowerCase();
  const tired = TIRED_MOODS.has(mood) || (snapshot.circadianPeriod === "late_night" && arousal < 0.3);
  return {
    blush: BLUSH_MOODS.has(mood) && (snapshot.mood?.intensity ?? 0) > 0.4 ? 1 : 0,
    sweat: SWEAT_LABELS.has(mood) || SWEAT_LABELS.has(reaction) ? 1 : 0,
    lean: STANCE[stanceFor(snapshot)].lean,
    slump: tired ? 1 : 0,
    bounce: arousal > 0.6 ? 1 + arousal * 2 : 0,
  };
}

export function tailMotionFor(sleepStatus: string | undefined, boosted: boolean) {
  if (sleepStatus === "asleep") return TAIL.sleeping;
  return boosted ? TAIL.wagging : TAIL.steady;
}

export function breathMotionFor(expressiveness: number, boosted: boolean) {
  if (expressiveness <= 0) return BREATH.muted;
  return boosted ? BREATH.boosted : BREATH.steady;
}
