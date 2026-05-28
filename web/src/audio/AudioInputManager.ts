/**
 * Captures microphone audio in the browser, converts it to Int16 LE PCM
 * frames, and pushes them down a sender callback as binary WebSocket
 * frames. The companion `mic-pcm-worklet.js` handles the actual
 * conversion in the audio thread; this manager owns the lifecycle.
 *
 * Audio quality goals:
 *   - 48 kHz mono Int16 LE (`getUserMedia` constraint).
 *   - Browser DSP enabled by default (echo cancellation, noise
 *     suppression, auto gain control) but every flag is toggleable
 *     so power users can disable them when a downstream model does
 *     its own DSP.
 *   - 50 ms framing → ~52 frames/sec; bandwidth ≈ 96000 B/s before WS
 *     overhead.
 */

import {
  buildMicPcm,
  buildMicStart,
  DSP_AUTO_GAIN_CONTROL,
  DSP_ECHO_CANCELLATION,
  DSP_NOISE_SUPPRESSION,
} from "./protocol";

export interface AudioInputConstraints {
  deviceId?: string;
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
}

export interface AudioInputCallbacks {
  /** Called with the binary frame ready to ship over the WebSocket. */
  send: (frame: Uint8Array) => void;
  /** Optional RMS-level callback for the UI level meter (0-1). */
  onLevel?: (rms: number) => void;
  /** Optional error callback for permission / hardware failures. */
  onError?: (err: unknown) => void;
}

const DEFAULT_SAMPLE_RATE = 48000;
const WORKLET_URL = "/mic-pcm-worklet.js";

export class AudioInputManager {
  private _ctx: AudioContext | null = null;
  private _stream: MediaStream | null = null;
  private _source: MediaStreamAudioSourceNode | null = null;
  private _worklet: AudioWorkletNode | null = null;
  private _running = false;
  private _constraints: AudioInputConstraints = {
    deviceId: undefined,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  };
  private _callbacks: AudioInputCallbacks;
  private _frameDurationMs = 50;

  constructor(callbacks: AudioInputCallbacks) {
    this._callbacks = callbacks;
  }

  get isRunning(): boolean {
    return this._running;
  }

  /** Replace the constraint bundle; takes effect on the next `start()`. */
  setConstraints(constraints: Partial<AudioInputConstraints>): void {
    this._constraints = { ...this._constraints, ...constraints };
  }

  get constraints(): AudioInputConstraints {
    return { ...this._constraints };
  }

  /**
   * Acquire the microphone, spin up the worklet, and start streaming.
   * Idempotent — calling `start()` twice in a row is a no-op.
   *
   * Resolves once the worklet is wired and the leading `mic_start`
   * frame has been dispatched; PCM frames will follow as they arrive
   * from the worklet thread.
   */
  async start(): Promise<void> {
    if (this._running) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: this._constraints.deviceId
            ? { exact: this._constraints.deviceId }
            : undefined,
          channelCount: 1,
          sampleRate: DEFAULT_SAMPLE_RATE,
          echoCancellation: this._constraints.echoCancellation,
          noiseSuppression: this._constraints.noiseSuppression,
          autoGainControl: this._constraints.autoGainControl,
        },
        video: false,
      });
      this._stream = stream;
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC({ sampleRate: DEFAULT_SAMPLE_RATE });
      this._ctx = ctx;
      await ctx.audioWorklet.addModule(WORKLET_URL);
      const source = ctx.createMediaStreamSource(stream);
      this._source = source;
      const worklet = new AudioWorkletNode(ctx, "mic-pcm-worklet", {
        processorOptions: { frameDurationMs: this._frameDurationMs },
      });
      worklet.port.onmessage = (ev: MessageEvent) => this._handleWorkletMessage(ev);
      source.connect(worklet);
      // Worklet output is unused (we ship the data via the message port),
      // but Web Audio requires the node to be in the graph or it
      // suspends. Route it through a muted gain to nowhere.
      const sink = ctx.createGain();
      sink.gain.value = 0;
      worklet.connect(sink);
      sink.connect(ctx.destination);
      this._worklet = worklet;
      this._running = true;
      // Announce the stream. The server uses this to size its
      // resampler and to attribute DSP behaviour for logs.
      this._callbacks.send(
        buildMicStart(ctx.sampleRate, 1, this._currentDspFlags()),
      );
    } catch (err) {
      this._teardown();
      this._reportError(err);
      throw err;
    }
  }

  /** Stop streaming and release the microphone hardware. */
  async stop(): Promise<void> {
    if (!this._running) return;
    this._teardown();
  }

  private _teardown(): void {
    this._running = false;
    if (this._worklet) {
      try {
        this._worklet.disconnect();
      } catch {
        /* already disconnected */
      }
      this._worklet.port.onmessage = null;
      this._worklet = null;
    }
    if (this._source) {
      try {
        this._source.disconnect();
      } catch {
        /* ignore */
      }
      this._source = null;
    }
    if (this._stream) {
      for (const track of this._stream.getTracks()) {
        try {
          track.stop();
        } catch {
          /* ignore */
        }
      }
      this._stream = null;
    }
    if (this._ctx) {
      const ctx = this._ctx;
      this._ctx = null;
      void ctx.close().catch(() => {
        /* already closed */
      });
    }
  }

  private _handleWorkletMessage(event: MessageEvent): void {
    if (!this._running) return;
    const data = event.data as { pcm?: ArrayBuffer; rms?: number };
    if (!data || !data.pcm) return;
    const frame = buildMicPcm(data.pcm);
    try {
      this._callbacks.send(frame);
    } catch (err) {
      this._reportError(err);
      return;
    }
    if (this._callbacks.onLevel && typeof data.rms === "number") {
      try {
        this._callbacks.onLevel(data.rms);
      } catch {
        /* ignore listener errors */
      }
    }
  }

  private _currentDspFlags(): number {
    let flags = 0;
    if (this._constraints.echoCancellation) flags |= DSP_ECHO_CANCELLATION;
    if (this._constraints.noiseSuppression) flags |= DSP_NOISE_SUPPRESSION;
    if (this._constraints.autoGainControl) flags |= DSP_AUTO_GAIN_CONTROL;
    return flags;
  }

  private _reportError(err: unknown): void {
    if (this._callbacks.onError) {
      try {
        this._callbacks.onError(err);
      } catch {
        /* ignore */
      }
    }
  }
}
