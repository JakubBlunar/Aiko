/**
 * Binary WebSocket frame protocol shared with the Python backend
 * (`app/web/audio_frames.py`).
 *
 * Keep this file in lock-step with the server module — the type bytes
 * and the byte layouts of `mic_start` / `audio_start` are part of the
 * over-the-wire contract.
 */

export const FRAME_MIC_PCM = 0x01;
export const FRAME_MIC_START = 0x02;
export const FRAME_TTS_PCM = 0x10;
export const FRAME_EARCON_PCM = 0x11;
export const FRAME_AUDIO_START = 0x12;
export const FRAME_AUDIO_END = 0x13;

/** Browser DSP flag bits sent in `mic_start.dsp_flags`. */
export const DSP_ECHO_CANCELLATION = 0b0000_0001;
export const DSP_NOISE_SUPPRESSION = 0b0000_0010;
export const DSP_AUTO_GAIN_CONTROL = 0b0000_0100;

export interface MicStartFrame {
  sampleRate: number;
  channels: number;
  dspFlags: number;
}

export interface AudioStartFrame {
  stream: number;
  sampleRate: number;
  channels: number;
}

/**
 * Build a `0x02 mic_start` frame. We always send mono.
 *
 * Layout (big-endian):
 *   [0]   type byte (0x02)
 *   [1..4] uint32 sample rate (Hz)
 *   [5]   uint8 channels
 *   [6]   uint8 dsp_flags
 */
export function buildMicStart(
  sampleRate: number,
  channels: number,
  dspFlags: number,
): Uint8Array {
  const buf = new ArrayBuffer(7);
  const view = new DataView(buf);
  view.setUint8(0, FRAME_MIC_START);
  view.setUint32(1, Math.max(0, Math.floor(sampleRate)) >>> 0, false);
  view.setUint8(5, Math.max(1, Math.min(255, channels | 0)));
  view.setUint8(6, dspFlags & 0xff);
  return new Uint8Array(buf);
}

/**
 * Build a `0x01 mic_pcm` frame around a chunk of Int16 LE samples.
 *
 * We do a single allocation + copy so the underlying audio worklet
 * can keep recycling its float32 buffer.
 */
export function buildMicPcm(pcmInt16Le: ArrayBuffer | Uint8Array): Uint8Array {
  const body =
    pcmInt16Le instanceof Uint8Array ? pcmInt16Le : new Uint8Array(pcmInt16Le);
  const out = new Uint8Array(body.length + 1);
  out[0] = FRAME_MIC_PCM;
  out.set(body, 1);
  return out;
}

/** Parse a `0x12 audio_start` frame body (without the leading type byte). */
export function parseAudioStart(body: Uint8Array): AudioStartFrame | null {
  if (body.byteLength < 6) return null;
  const view = new DataView(
    body.buffer,
    body.byteOffset,
    body.byteLength,
  );
  return {
    stream: view.getUint8(0),
    sampleRate: view.getUint32(1, false),
    channels: view.getUint8(5),
  };
}

/** Parse a `0x13 audio_end` frame body. Returns the stream byte. */
export function parseAudioEnd(body: Uint8Array): number | null {
  if (body.byteLength < 1) return null;
  return body[0];
}

/** Friendly tag for `0x10` / `0x11`. */
export function streamName(streamByte: number): "tts" | "earcon" | "unknown" {
  if (streamByte === FRAME_TTS_PCM) return "tts";
  if (streamByte === FRAME_EARCON_PCM) return "earcon";
  return "unknown";
}
