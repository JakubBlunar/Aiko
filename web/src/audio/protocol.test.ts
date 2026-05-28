import { describe, expect, it } from "vitest";
import {
  DSP_AUTO_GAIN_CONTROL,
  DSP_ECHO_CANCELLATION,
  DSP_NOISE_SUPPRESSION,
  FRAME_AUDIO_END,
  FRAME_AUDIO_START,
  FRAME_EARCON_PCM,
  FRAME_MIC_PCM,
  FRAME_MIC_START,
  FRAME_TTS_PCM,
  buildMicPcm,
  buildMicStart,
  parseAudioEnd,
  parseAudioStart,
  streamName,
} from "./protocol";

describe("frame type bytes", () => {
  it("are stable", () => {
    // Tied to the over-the-wire contract — any change here means the
    // backend (app/web/audio_frames.py) needs to be touched too.
    expect(FRAME_MIC_PCM).toBe(0x01);
    expect(FRAME_MIC_START).toBe(0x02);
    expect(FRAME_TTS_PCM).toBe(0x10);
    expect(FRAME_EARCON_PCM).toBe(0x11);
    expect(FRAME_AUDIO_START).toBe(0x12);
    expect(FRAME_AUDIO_END).toBe(0x13);
  });

  it("declares DSP flag bits used in mic_start", () => {
    expect(DSP_ECHO_CANCELLATION).toBe(0b0000_0001);
    expect(DSP_NOISE_SUPPRESSION).toBe(0b0000_0010);
    expect(DSP_AUTO_GAIN_CONTROL).toBe(0b0000_0100);
  });
});

describe("buildMicStart", () => {
  it("encodes sample rate big-endian + channels + flags", () => {
    const frame = buildMicStart(
      48000,
      1,
      DSP_ECHO_CANCELLATION | DSP_AUTO_GAIN_CONTROL,
    );
    expect(frame.byteLength).toBe(7);
    expect(frame[0]).toBe(FRAME_MIC_START);
    const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
    expect(view.getUint32(1, false)).toBe(48000);
    expect(view.getUint8(5)).toBe(1);
    expect(view.getUint8(6)).toBe(
      DSP_ECHO_CANCELLATION | DSP_AUTO_GAIN_CONTROL,
    );
  });

  it("clamps channels into the byte range", () => {
    const frame = buildMicStart(16000, 999, 0);
    expect(frame[5]).toBe(255);
  });
});

describe("buildMicPcm", () => {
  it("prefixes a 0x01 frame byte and copies the body", () => {
    const body = new Uint8Array([1, 2, 3, 4, 5]);
    const frame = buildMicPcm(body);
    expect(frame.byteLength).toBe(body.byteLength + 1);
    expect(frame[0]).toBe(FRAME_MIC_PCM);
    expect(Array.from(frame.slice(1))).toEqual([1, 2, 3, 4, 5]);
  });

  it("accepts ArrayBuffers in addition to Uint8Array", () => {
    const buffer = new Uint8Array([9, 8]).buffer;
    const frame = buildMicPcm(buffer);
    expect(frame[0]).toBe(FRAME_MIC_PCM);
    expect(Array.from(frame.slice(1))).toEqual([9, 8]);
  });
});

describe("parseAudioStart", () => {
  it("decodes stream / rate / channels", () => {
    const buf = new ArrayBuffer(6);
    const view = new DataView(buf);
    view.setUint8(0, FRAME_TTS_PCM);
    view.setUint32(1, 22050, false);
    view.setUint8(5, 1);
    const parsed = parseAudioStart(new Uint8Array(buf));
    expect(parsed).toEqual({
      stream: FRAME_TTS_PCM,
      sampleRate: 22050,
      channels: 1,
    });
  });

  it("returns null on a too-short body", () => {
    expect(parseAudioStart(new Uint8Array(3))).toBeNull();
  });
});

describe("parseAudioEnd", () => {
  it("returns the stream byte", () => {
    expect(parseAudioEnd(new Uint8Array([FRAME_EARCON_PCM]))).toBe(
      FRAME_EARCON_PCM,
    );
  });

  it("returns null on an empty body", () => {
    expect(parseAudioEnd(new Uint8Array(0))).toBeNull();
  });
});

describe("streamName", () => {
  it("maps the known stream bytes", () => {
    expect(streamName(FRAME_TTS_PCM)).toBe("tts");
    expect(streamName(FRAME_EARCON_PCM)).toBe("earcon");
    expect(streamName(0xff)).toBe("unknown");
  });
});
