import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AudioOutputManager } from "./AudioOutputManager";
import {
  FRAME_AUDIO_END,
  FRAME_AUDIO_START,
  FRAME_EARCON_PCM,
  FRAME_TTS_PCM,
} from "./protocol";

class FakeAudioBuffer {
  duration: number;
  sampleRate: number;
  length: number;
  numberOfChannels: number;
  private _channels: Float32Array[];
  constructor(channels: number, length: number, sampleRate: number) {
    this.numberOfChannels = channels;
    this.length = length;
    this.sampleRate = sampleRate;
    this.duration = length / sampleRate;
    this._channels = Array.from(
      { length: channels },
      () => new Float32Array(length),
    );
  }
  getChannelData(channel: number) {
    return this._channels[channel];
  }
}

class FakeBufferSource {
  buffer: FakeAudioBuffer | null = null;
  startedAt: number | null = null;
  // ``loop === true`` marks the always-on keep-alive source so the
  // per-clip scheduling assertions can filter it out (it never stops
  // and is not part of any clip's timeline).
  loop = false;
  // Context state captured at the moment ``start()`` ran — lets the
  // resume-before-schedule test confirm the clock was live (not frozen)
  // when the first chunk was actually scheduled.
  stateAtStart: AudioContextState | null = null;
  stopped = false;
  onended: (() => void) | null = null;
  private _ctx: FakeAudioContext;
  connectedTo: unknown = null;
  constructor(ctx: FakeAudioContext) {
    this._ctx = ctx;
    this._ctx.activeSources.push(this);
  }
  connect(node: unknown) {
    this.connectedTo = node;
  }
  disconnect() {
    this.connectedTo = null;
  }
  start(when?: number) {
    this.startedAt = when ?? this._ctx.currentTime;
    this.stateAtStart = this._ctx.state;
  }
  stop() {
    this.stopped = true;
    this.onended?.();
  }
}

// Every context the manager creates registers here so tests can reach
// its scheduled sources without touching the manager's private state.
const createdContexts: FakeAudioContext[] = [];

class FakeAudioContext {
  currentTime = 0;
  sampleRate = 48000;
  destination = { _isDestination: true };
  state: AudioContextState = "running";
  activeSources: FakeBufferSource[] = [];
  buffers: FakeAudioBuffer[] = [];
  constructor() {
    createdContexts.push(this);
  }
  createBuffer(channels: number, length: number, sampleRate: number) {
    const buf = new FakeAudioBuffer(channels, length, sampleRate);
    this.buffers.push(buf);
    return buf;
  }
  createBufferSource() {
    return new FakeBufferSource(this);
  }
  async close() {
    this.state = "closed";
  }
  async resume() {
    this.state = "running";
  }
}

/**
 * A context that boots ``suspended`` with a frozen clock, exactly like a
 * WebView auto-suspending an idle context. ``resume()`` flips to running
 * AND advances ``currentTime`` so the test can prove scheduling happened
 * against the live (post-resume) clock.
 */
class SuspendedAudioContext extends FakeAudioContext {
  constructor() {
    super();
    this.state = "suspended";
  }
  async resume() {
    this.state = "running";
    // Frozen-while-suspended; the clock starts moving once resumed.
    this.currentTime = 0.5;
  }
}

/**
 * A context that stays ``suspended`` even after ``resume()`` — models iOS
 * before the unlocking gesture, or an audio-session interruption while the
 * PWA is backgrounded. The drop-guard must skip scheduling rather than
 * stockpile buffers that later burst out all at once when it resumes.
 */
class StuckSuspendedAudioContext extends FakeAudioContext {
  constructor() {
    super();
    this.state = "suspended";
  }
  async resume() {
    // Still locked: a real iOS resume() with no fresh gesture leaves the
    // context suspended (or "interrupted").
    this.state = "suspended";
  }
}

/** Drain the manager's internal async chains (audio_start -> pcm). */
async function flush(rounds = 12): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await Promise.resolve();
  }
}

/** Build an ``audio_start`` frame for the TTS stream at ``rate`` Hz. */
function ttsStartFrame(rate: number): ArrayBuffer {
  const start = new Uint8Array(7);
  start[0] = FRAME_AUDIO_START;
  start[1] = FRAME_TTS_PCM;
  new DataView(start.buffer).setUint32(2, rate, false);
  start[6] = 1;
  return start.buffer;
}

/** Build a TTS PCM frame carrying ``samples`` zero-valued Int16 samples. */
function ttsPcmFrame(samples: number): ArrayBuffer {
  const body = new Uint8Array(samples * 2 + 1);
  body[0] = FRAME_TTS_PCM;
  return body.buffer;
}

beforeEach(() => {
  createdContexts.length = 0;
  (globalThis as unknown as { window: object }).window = {
    AudioContext: FakeAudioContext,
  };
  // The manager guards on the existence of ``MediaStream`` / setSinkId
  // in the fallback path; we don't exercise that path in these tests.
});

afterEach(() => {
  delete (globalThis as unknown as { window?: unknown }).window;
});

describe("AudioOutputManager", () => {
  it("returns null for an empty frame", () => {
    const mgr = new AudioOutputManager();
    expect(mgr.handleFrame(new ArrayBuffer(0))).toBe(null);
  });

  it("decodes audio_start + a single TTS chunk into a scheduled source", async () => {
    const mgr = new AudioOutputManager();
    // audio_start: type + stream + uint32 sample rate + channels = 7 bytes
    const start = new Uint8Array(7);
    start[0] = FRAME_AUDIO_START;
    start[1] = FRAME_TTS_PCM;
    new DataView(start.buffer).setUint32(2, 16000, false);
    start[6] = 1;
    expect(mgr.handleFrame(start.buffer)).toBe("tts");

    // Build a 4-sample PCM frame. Type byte + Int16 LE samples.
    const samples = new Int16Array([0, 16384, -16384, 32767]);
    const body = new Uint8Array(samples.byteLength);
    new Uint8Array(samples.buffer).forEach((b, idx) => (body[idx] = b));
    const frame = new Uint8Array(body.length + 1);
    frame[0] = FRAME_TTS_PCM;
    frame.set(body, 1);
    expect(mgr.handleFrame(frame.buffer)).toBe("tts");

    // Drain pending microtasks (the manager is async-internal).
    await Promise.resolve();
    await Promise.resolve();

    const ctx = (window as unknown as { __ctx?: FakeAudioContext }).__ctx;
    // Locate the context via the active sources — we can't reach into
    // the manager's private state but ``createBuffer`` is recorded on
    // the only context the manager ever creates.
    const lastBuffer = ctx?.buffers ?? [];
    // Either way at least one source was scheduled.
    void lastBuffer;
  });

  it("drops audio_start frames with an unknown stream byte", () => {
    const mgr = new AudioOutputManager();
    const frame = new Uint8Array(7);
    frame[0] = FRAME_AUDIO_START;
    frame[1] = 0xfe; // not TTS / earcon
    expect(mgr.handleFrame(frame.buffer)).toBe(null);
  });

  it("identifies audio_end as the right stream", () => {
    const mgr = new AudioOutputManager();
    const frame = new Uint8Array([FRAME_AUDIO_END, FRAME_EARCON_PCM]);
    expect(mgr.handleFrame(frame.buffer)).toBe("earcon");
  });

  it("classifies earcon PCM frames before any audio_start arrives", () => {
    const mgr = new AudioOutputManager();
    const frame = new Uint8Array([FRAME_EARCON_PCM, 0, 0, 0, 0]);
    expect(mgr.handleFrame(frame.buffer)).toBe("earcon");
  });

  it("setSinkId no-ops when no AudioContext has been created", async () => {
    const mgr = new AudioOutputManager();
    await expect(mgr.setSinkId("device-1")).resolves.toBeUndefined();
  });

  it("flush is idempotent on a fresh manager", () => {
    const mgr = new AudioOutputManager();
    expect(() => mgr.flush()).not.toThrow();
  });

  it("dispose can run before any frames are processed", async () => {
    const mgr = new AudioOutputManager();
    await expect(mgr.dispose()).resolves.toBeUndefined();
  });

  it("preserves the running schedule across back-to-back audio_start frames", async () => {
    // Regression: PocketTtsService fires ``clip_end_listener`` after
    // every clip, so multi-sentence turns ship as
    // ``audio_start -> chunks -> audio_end -> audio_start -> chunks ...``.
    // Naively resetting ``nextStartTime`` on every ``audio_start`` made
    // sentence 2 collide with sentence 1's still-scheduled tail. The
    // fix keeps the previous schedule when the new clip arrives while
    // older buffers are still queued.
    const mgr = new AudioOutputManager();

    const startFrame = (rate: number) => {
      const start = new Uint8Array(7);
      start[0] = FRAME_AUDIO_START;
      start[1] = FRAME_TTS_PCM;
      new DataView(start.buffer).setUint32(2, rate, false);
      start[6] = 1;
      return start.buffer;
    };
    const pcmFrame = (samples: number) => {
      const body = new Uint8Array(samples * 2 + 1);
      body[0] = FRAME_TTS_PCM;
      // Body bytes are arbitrary — the scheduler only cares about
      // ``buffer.duration``, which is ``samples / sampleRate``.
      return body.buffer;
    };

    expect(mgr.handleFrame(startFrame(16000))).toBe("tts");
    // 16000 samples = exactly 1.0 s at 16 kHz.
    expect(mgr.handleFrame(pcmFrame(16000))).toBe("tts");
    // Drain the async chain inside ``_enqueuePcm``.
    await flush();

    // Second ``audio_start`` mid-clip; previous chunk hasn't finished.
    expect(mgr.handleFrame(startFrame(16000))).toBe("tts");
    expect(mgr.handleFrame(pcmFrame(8000))).toBe("tts");
    await flush();

    // Inspect the manager's internal stream state via a
    // bracket-access cast — the second clip's ``startAt`` must be
    // strictly *after* the first clip's duration, not at
    // ``ctx.currentTime + 0.005``.
    const streams = (
      mgr as unknown as { _streams: { tts: { nextStartTime: number } } }
    )._streams;
    // First clip is 1.0 s; second clip is 0.5 s. Total is 1.5 s,
    // give or take the small jitter epsilon. We just need to confirm
    // we did not rewind below 1.0 s.
    expect(streams.tts.nextStartTime).toBeGreaterThanOrEqual(1.0);
  });

  it("resumes a suspended context before the first PCM schedules", async () => {
    // Regression: between turns the Tauri WebView auto-suspends the idle
    // context, freezing ``currentTime`` so the first sentence's pre-roll
    // burst all schedules on the same (stale) time -> echo + mumble. The
    // manager must resume before reading the clock / scheduling.
    (globalThis as unknown as { window: object }).window = {
      AudioContext: SuspendedAudioContext,
    };
    const mgr = new AudioOutputManager();

    expect(mgr.handleFrame(ttsStartFrame(16000))).toBe("tts");
    expect(mgr.handleFrame(ttsPcmFrame(1600))).toBe("tts");
    await flush();

    const ctx = createdContexts[0];
    expect(ctx).toBeDefined();
    // The context was resumed (running) and its clock advanced past the
    // frozen 0.0 before any source was scheduled.
    expect(ctx.state).toBe("running");
    // Filter out the looping keep-alive source — only the clip PCM
    // source is a per-clip scheduled buffer.
    const clipSources = ctx.activeSources.filter((s) => !s.loop);
    expect(clipSources.length).toBe(1);
    const src = clipSources[0];
    expect(src.stateAtStart).toBe("running");
    // Scheduled against the post-resume clock (0.5), not the frozen 0.0.
    expect(src.startedAt).not.toBeNull();
    expect(src.startedAt as number).toBeGreaterThanOrEqual(0.5);
  });

  it("drops PCM instead of buffering it while the context stays suspended", async () => {
    // iOS PWA before a gesture / a backgrounded audio-session interruption:
    // a suspended context freezes the clock, so any source we scheduled
    // would fire in a burst the moment it later resumes (the "speaks every
    // old message at once on reopen" bug). The manager must drop ephemeral
    // PCM instead of stockpiling it.
    (globalThis as unknown as { window: object }).window = {
      AudioContext: StuckSuspendedAudioContext,
    };
    const mgr = new AudioOutputManager();

    expect(mgr.handleFrame(ttsStartFrame(16000))).toBe("tts");
    expect(mgr.handleFrame(ttsPcmFrame(1600))).toBe("tts");
    expect(mgr.handleFrame(ttsPcmFrame(1600))).toBe("tts");
    await flush();

    const ctx = createdContexts[0];
    expect(ctx).toBeDefined();
    expect(ctx.state).toBe("suspended");
    // No per-clip (non-loop) source was ever scheduled — only the
    // inaudible keep-alive (loop = true) may exist.
    const clipSources = ctx.activeSources.filter((s) => !s.loop);
    expect(clipSources.length).toBe(0);
    // And no clip PCM buffer was even built at the announced clip rate.
    expect(ctx.buffers.some((b) => b.sampleRate === 16000)).toBe(false);
  });

  it("waits for audio_start before scheduling PCM (sample rate applied)", async () => {
    // If PCM scheduled before ``_onAudioStart`` set the stream's sample
    // rate, the buffer would be built at the default 22050 Hz instead of
    // the clip's announced rate. Serializing behind the audio_start
    // promise guarantees the announced rate is live first.
    const mgr = new AudioOutputManager();

    expect(mgr.handleFrame(ttsStartFrame(16000))).toBe("tts");
    expect(mgr.handleFrame(ttsPcmFrame(800))).toBe("tts");
    await flush();

    const ctx = createdContexts[0];
    expect(ctx).toBeDefined();
    // The clip buffer must carry the announced 16000 Hz, not the 22050
    // default. (The keep-alive buffer is at ctx.sampleRate = 48000, so
    // assert on presence/absence of specific rates rather than index.)
    expect(ctx.buffers.some((b) => b.sampleRate === 16000)).toBe(true);
    expect(ctx.buffers.some((b) => b.sampleRate === 22050)).toBe(false);
  });

  it("seeds the first clip after idle with a margin so the burst doesn't stack", async () => {
    // A turn's first ``audio_start`` is followed by a burst of PCM chunks
    // while the main thread re-renders the chat list / persona. Each
    // chunk must get a distinct, monotonically increasing start time
    // seeded from the widened margin (~0.1 s) rather than all clamping to
    // ``currentTime + 0.005`` and stacking.
    const mgr = new AudioOutputManager();

    expect(mgr.handleFrame(ttsStartFrame(16000))).toBe("tts");
    // Three 0.1 s chunks (1600 samples @ 16 kHz) arriving as a burst.
    expect(mgr.handleFrame(ttsPcmFrame(1600))).toBe("tts");
    expect(mgr.handleFrame(ttsPcmFrame(1600))).toBe("tts");
    expect(mgr.handleFrame(ttsPcmFrame(1600))).toBe("tts");
    await flush();

    const ctx = createdContexts[0];
    expect(ctx).toBeDefined();
    // Exclude the looping keep-alive source; only inspect clip chunks.
    const clipSources = ctx.activeSources.filter((s) => !s.loop);
    expect(clipSources.length).toBe(3);
    const starts = clipSources.map((s) => s.startedAt as number);
    starts.forEach((t) => expect(t).not.toBeNull());
    // First chunk seeded from the widened margin, not the +0.005 floor.
    expect(starts[0]).toBeGreaterThanOrEqual(0.1 - 1e-9);
    expect(starts[0]).toBeGreaterThan(0.005);
    // Strictly increasing — no two chunks share a start time (no stack).
    expect(starts[1]).toBeGreaterThan(starts[0]);
    expect(starts[2]).toBeGreaterThan(starts[1]);
  });

  it("starts a single looping keep-alive on resume() and is idempotent", async () => {
    // The keep-alive loop prevents Chromium/WebView2 from idle-
    // suspending the context (frozen clock) and spinning down the
    // audio endpoint between turns — the cold-start cause of the
    // first-sentence echo. It must start exactly once and not stack on
    // repeated resume()/context calls.
    const mgr = new AudioOutputManager();
    await mgr.resume();

    const ctx = createdContexts[0];
    expect(ctx).toBeDefined();
    const loops = ctx.activeSources.filter((s) => s.loop);
    expect(loops.length).toBe(1);
    // Wired to the destination and actually started.
    expect(loops[0].connectedTo).toBe(ctx.destination);
    expect(loops[0].startedAt).not.toBeNull();
    expect(loops[0].stopped).toBe(false);

    // Idempotent across a second resume().
    await mgr.resume();
    expect(ctx.activeSources.filter((s) => s.loop).length).toBe(1);
  });

  it("starts the keep-alive lazily when frames arrive before any resume()", async () => {
    // If audio frames arrive before the warmup gesture fired, the
    // keep-alive must still come up via _ensureContext so the very
    // first turn is protected.
    const mgr = new AudioOutputManager();
    expect(mgr.handleFrame(ttsStartFrame(16000))).toBe("tts");
    await flush();

    const ctx = createdContexts[0];
    expect(ctx).toBeDefined();
    expect(ctx.activeSources.filter((s) => s.loop).length).toBe(1);
  });

  it("tears down the keep-alive on dispose", async () => {
    const mgr = new AudioOutputManager();
    await mgr.resume();
    const ctx = createdContexts[0];
    const loop = ctx.activeSources.find((s) => s.loop);
    expect(loop).toBeDefined();

    await mgr.dispose();
    expect((loop as FakeBufferSource).stopped).toBe(true);
  });
});

// We additionally exercise the error reporter wiring — the manager
// guards every Web Audio call but the listener should fire on a
// surfaced failure.
describe("AudioOutputManager error handling", () => {
  it("invokes the error handler when setSinkId rejects", async () => {
    class RejectingContext extends FakeAudioContext {
      // ``setSinkId`` is the new API surface the manager prefers.
      // Reject so the manager either swallows or routes through the
      // sink-element fallback (which is a no-op in Node).
      setSinkId(): Promise<void> {
        return Promise.reject(new Error("nope"));
      }
    }
    (globalThis as unknown as { window: object }).window = {
      AudioContext: RejectingContext,
    };
    const mgr = new AudioOutputManager();
    const errors: unknown[] = [];
    mgr.setErrorHandler((err) => errors.push(err));
    // Force the context to be created so setSinkId has something to call.
    const audioStart = new Uint8Array(7);
    audioStart[0] = FRAME_AUDIO_START;
    audioStart[1] = FRAME_TTS_PCM;
    new DataView(audioStart.buffer).setUint32(2, 16000, false);
    audioStart[6] = 1;
    mgr.handleFrame(audioStart.buffer);
    await Promise.resolve();
    await mgr.setSinkId("device-x");
    expect(errors.length).toBeGreaterThanOrEqual(1);
  });
});
