/**
 * Tiny WebAudio earcons -- short, non-musical blips that signal voice-mode
 * state transitions. We synthesize them inline so we don't have to ship audio
 * files.
 *
 * Two earcons are exposed:
 *   - playThinking() -- short upward tone, fires when Aiko starts thinking
 *   - playDone()     -- short downward tone, fires when Aiko stops speaking
 *                       (i.e., the floor returns to the user)
 *
 * The audio context is lazily initialized on the first call (browser autoplay
 * policy permits this once the user has interacted, which voice mode requires
 * to grant the mic).
 */

let ctx: AudioContext | null = null;

function getContext(): AudioContext | null {
  if (typeof window === "undefined") {
    return null;
  }
  if (ctx === null) {
    try {
      const Ctor = (window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext) as typeof AudioContext;
      ctx = new Ctor();
    } catch {
      ctx = null;
    }
  }
  if (ctx && ctx.state === "suspended") {
    void ctx.resume().catch(() => {
      /* fine -- next call will retry */
    });
  }
  return ctx;
}

interface ToneSpec {
  startHz: number;
  endHz: number;
  durationMs: number;
  /** Peak gain in [0, 1]. Keep low; earcons should not overpower TTS. */
  peakGain?: number;
}

function playTone({ startHz, endHz, durationMs, peakGain = 0.06 }: ToneSpec): void {
  const audio = getContext();
  if (!audio) {
    return;
  }
  const now = audio.currentTime;
  const dur = durationMs / 1000;
  const osc = audio.createOscillator();
  const gain = audio.createGain();
  osc.type = "sine";
  osc.frequency.setValueAtTime(startHz, now);
  osc.frequency.linearRampToValueAtTime(endHz, now + dur);
  gain.gain.setValueAtTime(0, now);
  gain.gain.linearRampToValueAtTime(peakGain, now + dur * 0.15);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + dur);
  osc.connect(gain).connect(audio.destination);
  osc.start(now);
  osc.stop(now + dur + 0.02);
}

/** Up-chirp -- "I'm thinking now." */
export function playThinking(): void {
  playTone({ startHz: 480, endHz: 720, durationMs: 140 });
}

/** Down-chirp -- "I'm done speaking, your turn." */
export function playDone(): void {
  playTone({ startHz: 720, endHz: 360, durationMs: 180 });
}
