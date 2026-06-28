import type { BackchannelHint, VoiceMode } from "@/types";
import type { SliceCreator } from "../types";

export interface VoiceSlice {
  // Continuous voice mode
  voiceMode: VoiceMode;
  audioLevel: number;
  lastTranscript: string;
  /** Live partial transcript ("Hearing: …"); cleared on stt_final or
   * when the voice session ends. Never appended to chat history. */
  currentPartial: string;
  setVoiceMode: (mode: VoiceMode) => void;
  setAudioLevel: (level: number) => void;
  setLastTranscript: (text: string) => void;
  setCurrentPartial: (text: string) => void;

  // Phase 1a: transient backchannel hints from STT partials.
  backchannelHint: BackchannelHint | null;
  backchannelAt: number;
  pushBackchannel: (hint: BackchannelHint) => void;
}

export const createVoiceSlice: SliceCreator<VoiceSlice> = (set) => ({
  voiceMode: "off",
  audioLevel: 0,
  lastTranscript: "",
  currentPartial: "",
  setVoiceMode: (mode) =>
    set(() => {
      const next: Partial<VoiceSlice> = { voiceMode: mode };
      // Voice session ended -> the live "Hearing: …" line should disappear
      // even if no stt_final lands (e.g. mic toggled off mid-utterance).
      if (mode === "off") {
        next.currentPartial = "";
      }
      return next;
    }),
  setAudioLevel: (level) =>
    set({ audioLevel: Math.max(0, Math.min(1, level)) }),
  setLastTranscript: (text) => set({ lastTranscript: text }),
  setCurrentPartial: (text) => set({ currentPartial: text }),

  backchannelHint: null,
  backchannelAt: 0,
  pushBackchannel: (hint) =>
    set({ backchannelHint: hint, backchannelAt: Date.now() }),
});
