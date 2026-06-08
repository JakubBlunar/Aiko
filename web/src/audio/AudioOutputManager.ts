/**
 * Client-side TTS / earcon playback.
 *
 * The server streams Int16 LE PCM chunks as `0x10 tts_pcm` /
 * `0x11 earcon_pcm` binary WS frames; we schedule them onto a
 * Web Audio context using a chained `AudioBufferSourceNode` queue
 * so each chunk plays back-to-back without gaps.
 *
 * One context handles both streams. We track per-stream metadata
 * (sample rate, schedule time) because `audio_start` may announce
 * different rates per clip, but in the current backend TTS and
 * earcons both ship at 22050 Hz / 16000 Hz so the chained scheduler
 * just resets ahead of each new clip.
 */

import { debugLog } from "../log";
import {
  FRAME_AUDIO_END,
  FRAME_AUDIO_START,
  FRAME_EARCON_PCM,
  FRAME_TTS_PCM,
  parseAudioEnd,
  parseAudioStart,
  streamName,
} from "./protocol";

type StreamTag = "tts" | "earcon";

/**
 * Extra lead time (seconds) seeded ahead of the *first* clip of a turn —
 * i.e. when the previous schedule has already elapsed (the stream went
 * idle between turns). A turn's first ``audio_start`` is immediately
 * followed by a burst of PCM chunks while the main thread is also busy
 * re-rendering the chat list / persona on message arrival. With only the
 * old ``+0.005`` floor, that jank could push a buffer's computed start
 * time behind ``ctx.currentTime`` by the time ``source.start()`` actually
 * runs; Web Audio then clamps it to "now" and the burst stacks (echo +
 * mumble on sentence one). A ~100 ms cushion absorbs that jitter. It only
 * applies to the first clip after idle — steady-state chaining between
 * sentences inside a turn is left tight, so there is no added mid-reply
 * latency.
 */
const FIRST_CLIP_IDLE_MARGIN_SEC = 0.1;

interface PerStream {
  sampleRate: number;
  channels: number;
  /** Absolute audio-context time at which the next chunk should start. */
  nextStartTime: number;
  /** Sources currently scheduled — kept so we can stop on takeover. */
  active: AudioBufferSourceNode[];
}

export interface AudioOutputOptions {
  /** Output device id (`MediaDeviceInfo.deviceId`); empty = default. */
  sinkId?: string;
}

export class AudioOutputManager {
  private _ctx: AudioContext | null = null;
  private _streams: Record<StreamTag, PerStream> = {
    tts: this._emptyState(),
    earcon: this._emptyState(),
  };
  // Per-stream "audio_start applied" gate. ``_enqueuePcm`` awaits this
  // before scheduling so PCM never reads a stale sample rate or a frozen
  // clock — the sample rate / resume / carry-over set in ``_onAudioStart``
  // is always live by the time the first chunk lands.
  private _ready: Record<StreamTag, Promise<void> | null> = {
    tts: null,
    earcon: null,
  };
  private _sinkId: string = "";
  // ``HTMLAudioElement`` companion used to route audio to a non-default
  // device. ``AudioContext.setSinkId`` exists in newer Chromes but is
  // not yet universal; we keep a sink-element pattern as a fallback.
  private _sinkElement: HTMLAudioElement | null = null;
  private _onError: ((err: unknown) => void) | null = null;
  // Always-on inaudible keep-alive source. Chromium (and, worse,
  // WebView2 in the Tauri shell) auto-suspends an idle AudioContext
  // between turns and lets the OS audio endpoint spin down; the first
  // clip of the next turn then plays into a cold/just-waking device and
  // the first sentence comes out echoey/mumbled while later sentences
  // (warm device) are clean. A continuous ~-90 dBFS loop keeps the
  // context running and the endpoint open so every turn's first clip
  // lands warm. Started lazily on the first context create/resume,
  // stopped only on ``dispose``.
  private _keepAlive: AudioBufferSourceNode | null = null;
  // Diagnostics: when a clip's ``audio_start`` fires we arm this so the
  // first PCM scheduled for that clip logs its ``startAt`` (the value
  // that reveals overlap / past-scheduling). Cleared after the one log.
  private _pendingFirstPcmLog: Record<StreamTag, boolean> = {
    tts: false,
    earcon: false,
  };

  constructor(options: AudioOutputOptions = {}) {
    this._sinkId = options.sinkId ?? "";
  }

  /**
   * Eagerly initialise the AudioContext. Browsers require a user
   * gesture before audio plays; call this from the first onboarding
   * click so subsequent TTS clips don't hit autoplay blocks.
   */
  async resume(): Promise<void> {
    const ctx = await this._ensureContext();
    if (ctx.state === "suspended") {
      try {
        await ctx.resume();
      } catch (err) {
        this._reportError(err);
      }
    }
    // The gesture-gated warmup path satisfies autoplay, so this is the
    // ideal moment to (re)start the keep-alive loop.
    this._startKeepAlive(ctx);
  }

  /** Subscribe to playback errors (decode failures, sink misroutes, …). */
  setErrorHandler(handler: ((err: unknown) => void) | null): void {
    this._onError = handler;
  }

  /**
   * Switch the output device. `deviceId === ""` resolves to the OS default.
   * Falls back to the sink-element route on browsers that don't expose
   * `AudioContext.setSinkId`.
   */
  async setSinkId(deviceId: string): Promise<void> {
    this._sinkId = deviceId || "";
    const ctx = this._ctx;
    if (!ctx) return;
    const ctxAny = ctx as unknown as {
      setSinkId?: (id: string) => Promise<void>;
    };
    if (typeof ctxAny.setSinkId === "function") {
      try {
        await ctxAny.setSinkId(this._sinkId);
        if (this._sinkElement) {
          this._sinkElement.remove();
          this._sinkElement = null;
        }
        return;
      } catch (err) {
        this._reportError(err);
        // fall through to the element-based fallback below
      }
    }
    await this._routeViaSinkElement();
  }

  /**
   * Feed a raw binary frame straight from the WebSocket. Returns the
   * tag of the stream the frame belongs to, or `null` if the frame is
   * not an output type we own.
   */
  handleFrame(buffer: ArrayBuffer): StreamTag | null {
    if (buffer.byteLength < 1) return null;
    const data = new Uint8Array(buffer);
    const type = data[0];
    const body = data.subarray(1);
    if (type === FRAME_AUDIO_START) {
      const parsed = parseAudioStart(body);
      if (!parsed) return null;
      const tag = streamName(parsed.stream);
      if (tag === "unknown") return null;
      // Store the in-flight promise so the PCM that follows this
      // audio_start serializes behind it (resume + sample rate +
      // carry-over all applied before the first chunk schedules).
      this._ready[tag] = this._onAudioStart(
        tag,
        parsed.sampleRate,
        parsed.channels,
      );
      void this._ready[tag];
      return tag;
    }
    if (type === FRAME_AUDIO_END) {
      const streamByte = parseAudioEnd(body);
      if (streamByte === null) return null;
      const tag = streamName(streamByte);
      if (tag === "unknown") return null;
      this._onAudioEnd(tag);
      return tag;
    }
    if (type === FRAME_TTS_PCM) {
      void this._enqueuePcm("tts", body);
      return "tts";
    }
    if (type === FRAME_EARCON_PCM) {
      void this._enqueuePcm("earcon", body);
      return "earcon";
    }
    return null;
  }

  /** Stop everything currently queued. Used on takeover / disconnect. */
  flush(): void {
    for (const tag of ["tts", "earcon"] as StreamTag[]) {
      this._stopStream(tag);
    }
  }

  /** Tear down the audio context entirely. */
  async dispose(): Promise<void> {
    this.flush();
    if (this._keepAlive) {
      try {
        this._keepAlive.stop();
      } catch {
        /* already stopped / never started */
      }
      this._keepAlive = null;
    }
    const ctx = this._ctx;
    this._ctx = null;
    if (this._sinkElement) {
      this._sinkElement.srcObject = null;
      this._sinkElement.remove();
      this._sinkElement = null;
    }
    if (ctx) {
      try {
        await ctx.close();
      } catch (err) {
        this._reportError(err);
      }
    }
  }

  private async _ensureContext(): Promise<AudioContext> {
    if (!this._ctx) {
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      if (!AC) {
        throw new Error("Web Audio API is not available in this browser.");
      }
      this._ctx = new AC();
      if (this._sinkId) {
        await this.setSinkId(this._sinkId);
      }
    }
    // Safety net: a WebView (WebView2 / WKWebView in the Tauri shell)
    // auto-suspends an idle AudioContext between turns, which freezes
    // ``currentTime``. If we schedule the next turn's pre-roll burst
    // against a frozen clock every chunk computes the same start time
    // and they play on top of each other (echo + mumble on the first
    // sentence). Resume here so every caller gets a live clock before
    // it reads ``currentTime`` or schedules a buffer.
    if (this._ctx.state === "suspended") {
      try {
        await this._ctx.resume();
      } catch (err) {
        this._reportError(err);
      }
    }
    // Idempotent: keeps the context + audio endpoint warm so the first
    // clip of every turn never plays into a cold device.
    this._startKeepAlive(this._ctx);
    return this._ctx;
  }

  private async _onAudioStart(
    tag: StreamTag,
    sampleRate: number,
    channels: number,
  ): Promise<void> {
    // Capture the pre-resume state for diagnostics: this is the only
    // place we can tell whether the context had idle-suspended between
    // turns (the suspect behaviour) before ``_ensureContext`` resumes it.
    const wasSuspended = this._ctx?.state === "suspended";
    // ``_ensureContext`` resumes a suspended context, so ``currentTime``
    // is guaranteed live below.
    const ctx = await this._ensureContext();
    const prev = this._streams[tag];
    const now = ctx.currentTime;
    // Preserve the running schedule across back-to-back clips. The
    // server emits ``audio_end`` + ``audio_start`` between sentences
    // because :class:`PocketTtsService` fires its ``clip_end_listener``
    // at the end of every ``_emit_pcm`` call. If we naively reset
    // ``nextStartTime`` to ``ctx.currentTime`` here, the next
    // sentence's chunks land before the previous one's tail finishes
    // and the user hears two sentences on top of each other. Carrying
    // the previous schedule forward chains them seamlessly.
    //
    // ``idle`` is true when the previous schedule has already elapsed
    // (the stream went quiet between turns, or this is the very first
    // clip). For that *first clip after idle* we seed a small lead
    // (``FIRST_CLIP_IDLE_MARGIN_SEC``) so a burst of main-thread work
    // on message arrival can't push the first buffer's start into the
    // past. While the previous clip is still queued ahead (mid-turn
    // chaining) we keep the schedule tight at ``prev.nextStartTime`` so
    // there is no added latency between sentences and the
    // no-overlap invariant holds.
    const idle = prev.nextStartTime <= now;
    const carryOver = idle
      ? now + FIRST_CLIP_IDLE_MARGIN_SEC
      : prev.nextStartTime;
    this._streams[tag] = {
      sampleRate: Math.max(8000, sampleRate || ctx.sampleRate),
      channels: Math.max(1, channels || 1),
      nextStartTime: carryOver,
      active: prev.active.filter(
        (src) => (src as unknown as { _stopped?: boolean })._stopped !== true,
      ),
    };
    // Arm the one-shot first-PCM startAt log for this clip.
    this._pendingFirstPcmLog[tag] = true;
    // Diagnostics (no-op unless Debug logging is on). The pre-resume
    // ``state`` + ``resumed`` flag reveal whether the context had
    // idle-suspended; ``idle`` + ``carryOver`` show which scheduling
    // branch we took. Lands in data/app.log as ``[ui] audio clipStart``.
    debugLog.log({
      source: "audio",
      kind: "clipStart",
      payload: {
        tag,
        announcedRate: sampleRate,
        ctxRate: ctx.sampleRate,
        state: wasSuspended ? "suspended" : ctx.state,
        resumed: wasSuspended,
        currentTime: Number(now.toFixed(4)),
        idle,
        carryOver: Number(carryOver.toFixed(4)),
      },
    });
  }

  private _onAudioEnd(tag: StreamTag): void {
    // Nothing to flush here — the chained sources finish on their own.
    // We could prune the ``active`` list but it's bounded by the
    // clip length and the GC reclaims the buffers shortly after each
    // ``onended`` fires.
    this._streams[tag].active = this._streams[tag].active.filter(
      (src) => (src as unknown as { _stopped?: boolean })._stopped !== true,
    );
  }

  /**
   * Start the always-on inaudible keep-alive loop. Idempotent — a
   * second call while one is running is a no-op, so it's safe to invoke
   * from both ``resume`` and ``_ensureContext``.
   *
   * A pure-silence (all-zero) buffer can be optimised away by the
   * browser's silence detector, which still lets the context idle-
   * suspend; we fill the buffer with ~-90 dBFS dither (nonzero but
   * ~30 dB below anything audible) so the output graph keeps producing
   * real samples and the audio endpoint stays open. Runs in both the
   * browser and the Tauri shell: the browser shows a milder version of
   * the same cold-start artifact, and an inaudible loop is harmless
   * there (at worst the tab's "audio playing" indicator lights up).
   */
  private _startKeepAlive(ctx: AudioContext): void {
    if (this._keepAlive) return;
    try {
      const frames = Math.max(1, Math.floor((ctx.sampleRate || 48000) * 0.5));
      const buf = ctx.createBuffer(1, frames, ctx.sampleRate || 48000);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < ch.length; i++) {
        ch[i] = (Math.random() * 2 - 1) * 3e-5;
      }
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.loop = true;
      src.connect(ctx.destination);
      src.start();
      this._keepAlive = src;
    } catch (err) {
      this._reportError(err);
    }
  }

  private async _enqueuePcm(tag: StreamTag, body: Uint8Array): Promise<void> {
    if (body.byteLength < 2) return;
    // Serialize behind the stream's ``audio_start`` so we never schedule
    // against a stale sample rate or a frozen clock. The promise
    // resolves once ``_onAudioStart`` has resumed the context and seeded
    // ``nextStartTime``; if no audio_start preceded (earcon PCM before
    // its start frame) ``_ready`` is null and we fall straight through.
    const ready = this._ready[tag];
    if (ready) {
      try {
        await ready;
      } catch {
        /* failure already surfaced via _reportError in _onAudioStart */
      }
    }
    const ctx = await this._ensureContext();
    const state = this._streams[tag];
    // PCM is signed 16-bit little-endian; respect the body's byteOffset
    // so the underlying ArrayBuffer (which holds the full frame) doesn't
    // include the type byte in the Int16 view.
    const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
    const sampleCount = body.byteLength >> 1;
    if (sampleCount === 0) return;
    const buffer = ctx.createBuffer(1, sampleCount, state.sampleRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < sampleCount; i++) {
      const sample = view.getInt16(i * 2, true);
      channel[i] = Math.max(-1, Math.min(1, sample / 32767));
    }
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    // Compute the start time: never schedule in the past, otherwise
    // the Web Audio scheduler silently drops the buffer.
    const startAt = Math.max(state.nextStartTime, ctx.currentTime + 0.005);
    state.nextStartTime = startAt + buffer.duration;
    // Diagnostics: log the first scheduled chunk of each clip. ``startAt``
    // vs ``currentTime`` is the value that exposes overlap / past-
    // scheduling (the echo signature). No-op unless Debug logging is on.
    if (this._pendingFirstPcmLog[tag]) {
      this._pendingFirstPcmLog[tag] = false;
      debugLog.log({
        source: "audio",
        kind: "firstPcm",
        payload: {
          tag,
          startAt: Number(startAt.toFixed(4)),
          currentTime: Number(ctx.currentTime.toFixed(4)),
          sampleRate: state.sampleRate,
          lead: Number((startAt - ctx.currentTime).toFixed(4)),
        },
      });
    }
    source.onended = () => {
      (source as unknown as { _stopped: boolean })._stopped = true;
    };
    state.active.push(source);
    try {
      source.start(startAt);
    } catch (err) {
      this._reportError(err);
    }
  }

  private _stopStream(tag: StreamTag): void {
    const state = this._streams[tag];
    for (const src of state.active) {
      try {
        src.stop();
      } catch {
        /* already finished */
      }
    }
    state.active = [];
    const ctx = this._ctx;
    state.nextStartTime = ctx ? ctx.currentTime : 0;
  }

  private async _routeViaSinkElement(): Promise<void> {
    const ctx = this._ctx;
    if (!ctx) return;
    if (!("MediaStream" in window) || !("setSinkId" in HTMLMediaElement.prototype)) {
      // Browser doesn't support per-element sink selection; nothing to do.
      return;
    }
    // Build a ``MediaStreamAudioDestinationNode`` route so we can pin
    // the output device on the resulting ``<audio>``. Older path that
    // works on Firefox + Safari where ``AudioContext.setSinkId`` is
    // missing but ``HTMLMediaElement.setSinkId`` exists in some builds.
    const destination = ctx.createMediaStreamDestination();
    // Re-route the context's existing destination -> our element. We
    // don't disconnect the original because that'd stop the scheduled
    // sources; instead we connect *also* to the destination node and
    // mute the default output once the element is wired.
    try {
      ctx.destination.disconnect();
    } catch {
      /* not connected to anything yet */
    }
    const passthrough = ctx.createGain();
    passthrough.connect(destination);
    // No way to redirect future ``ctx.destination`` writes — we can
    // only mute it. We muddle through by reconnecting all *current*
    // active sources to the destination node, which works for the
    // next clip but stale-routes any in-flight ones.
    for (const tag of ["tts", "earcon"] as StreamTag[]) {
      for (const src of this._streams[tag].active) {
        try {
          src.disconnect();
          src.connect(destination);
        } catch (err) {
          this._reportError(err);
        }
      }
    }
    const element = document.createElement("audio");
    element.autoplay = true;
    element.srcObject = destination.stream;
    const elementWithSink = element as unknown as {
      setSinkId?: (id: string) => Promise<void>;
    };
    if (typeof elementWithSink.setSinkId === "function") {
      try {
        await elementWithSink.setSinkId(this._sinkId);
      } catch (err) {
        this._reportError(err);
      }
    }
    document.body.appendChild(element);
    if (this._sinkElement) {
      this._sinkElement.remove();
    }
    this._sinkElement = element;
  }

  private _emptyState(): PerStream {
    return {
      sampleRate: 22050,
      channels: 1,
      nextStartTime: 0,
      active: [],
    };
  }

  private _reportError(err: unknown): void {
    if (this._onError) {
      try {
        this._onError(err);
      } catch {
        /* ignore listener errors */
      }
    }
  }
}
