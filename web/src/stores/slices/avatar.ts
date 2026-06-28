import type { AudioOutputManager } from "@/audio/AudioOutputManager";
import type {
  AvatarMotionState,
  AvatarOverlayState,
  AvatarProfile,
  AvatarSettingsKnobs,
  AvatarTouchPayload,
  CircadianPeriod,
  MoodState,
  ResolvedOutfit,
} from "@/types";
import { readBool, writeBool } from "../persist";
import type { SliceCreator } from "../types";

const LS_AUDIO_MUTED = "aiko.audio.muted";

export interface AvatarSlice {
  // Live2D avatar (fixed Alexia bundle).
  avatar: AvatarProfile | null;
  /** Lip-sync amplitude in [0, 1]; updated at <=30 Hz from the WS. */
  audioAmplitude: number;
  /** The single ``AudioOutputManager`` owned by the WS hook. */
  audioOutput: AudioOutputManager | null;
  /** True while the AudioContext is ``running`` (sound is unlocked). */
  audioUnlocked: boolean;
  /** Per-device audio mute, persisted to localStorage. */
  audioMuted: boolean;
  setAudioOutput: (out: AudioOutputManager | null) => void;
  setAudioUnlocked: (unlocked: boolean) => void;
  setAudioMuted: (muted: boolean) => void;
  /** Latest transient overlay pulse fired via ``[[overlay:X]]``. */
  avatarOverlay: AvatarOverlayState | null;
  /** Latest LLM-driven ``[[motion:X]]`` directive. */
  avatarMotion: AvatarMotionState | null;
  setAvatar: (avatar: AvatarProfile | null) => void;
  /** Patch only the user-tunable runtime knobs. */
  setAvatarSettings: (settings: Partial<AvatarSettingsKnobs>) => void;
  /** Patch the world-state pieces (circadian period, resolved outfit). */
  updateAvatarWorldState: (next: {
    circadian_period?: CircadianPeriod;
    resolved_outfit?: ResolvedOutfit;
  }) => void;
  setAvatarOverlay: (overlay: AvatarOverlayState | null) => void;
  setAvatarMotion: (motion: AvatarMotionState | null) => void;
  setAudioAmplitude: (level: number) => void;

  // K31: latest avatar_touch payload + dedup counter.
  avatarTouch: AvatarTouchPayload | null;
  avatarTouchAt: number;
  pushAvatarTouch: (payload: AvatarTouchPayload) => void;

  // Phase 2b: persistent mood snapshot, updated post-turn.
  mood: MoodState;
  setMood: (mood: MoodState) => void;
}

export const createAvatarSlice: SliceCreator<AvatarSlice> = (set) => ({
  avatar: null,
  audioAmplitude: 0,
  audioOutput: null,
  audioUnlocked: false,
  audioMuted: readBool(LS_AUDIO_MUTED, false),
  setAudioOutput: (out) => set({ audioOutput: out }),
  setAudioUnlocked: (unlocked) => set({ audioUnlocked: unlocked }),
  setAudioMuted: (muted) => {
    writeBool(LS_AUDIO_MUTED, muted);
    set({ audioMuted: muted });
  },
  avatarOverlay: null,
  avatarMotion: null,
  setAvatar: (avatar) => set({ avatar }),
  setAvatarSettings: (settings) =>
    set((state) => {
      if (!state.avatar) {
        return state;
      }
      return {
        avatar: {
          ...state.avatar,
          settings: { ...state.avatar.settings, ...settings },
        },
      };
    }),
  updateAvatarWorldState: (next) =>
    set((state) => {
      if (!state.avatar) {
        return state;
      }
      const merged: AvatarProfile = { ...state.avatar };
      if (next.circadian_period !== undefined) {
        merged.circadian_period = next.circadian_period;
      }
      if (next.resolved_outfit !== undefined) {
        merged.resolved_outfit = next.resolved_outfit;
      }
      return { avatar: merged };
    }),
  setAvatarOverlay: (overlay) => set({ avatarOverlay: overlay }),
  setAvatarMotion: (motion) => set({ avatarMotion: motion }),
  setAudioAmplitude: (level) =>
    set({ audioAmplitude: Math.max(0, Math.min(1, level)) }),

  avatarTouch: null,
  avatarTouchAt: 0,
  pushAvatarTouch: (payload) =>
    set((state) => ({
      avatarTouch: payload,
      // Increment by at least 1 (and use Date.now() as a coarse
      // wall-clock id) so the engine subscribes to a
      // monotonically-increasing counter — back-to-back ``hug``
      // gestures still fan out as two distinct dispatches.
      avatarTouchAt: Math.max(state.avatarTouchAt + 1, Date.now()),
    })),

  mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
  setMood: (mood) => set({ mood }),
});
