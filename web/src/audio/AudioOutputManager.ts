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
  private _sinkId: string = "";
  // ``HTMLAudioElement`` companion used to route audio to a non-default
  // device. ``AudioContext.setSinkId`` exists in newer Chromes but is
  // not yet universal; we keep a sink-element pattern as a fallback.
  private _sinkElement: HTMLAudioElement | null = null;
  private _onError: ((err: unknown) => void) | null = null;

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
      void this._onAudioStart(tag, parsed.sampleRate, parsed.channels);
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
    if (this._ctx) return this._ctx;
    const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!AC) {
      throw new Error("Web Audio API is not available in this browser.");
    }
    this._ctx = new AC();
    if (this._sinkId) {
      await this.setSinkId(this._sinkId);
    }
    return this._ctx;
  }

  private async _onAudioStart(
    tag: StreamTag,
    sampleRate: number,
    channels: number,
  ): Promise<void> {
    const ctx = await this._ensureContext();
    const prev = this._streams[tag];
    // Preserve the running schedule across back-to-back clips. The
    // server emits ``audio_end`` + ``audio_start`` between sentences
    // because :class:`PocketTtsService` fires its ``clip_end_listener``
    // at the end of every ``_emit_pcm`` call. If we naively reset
    // ``nextStartTime`` to ``ctx.currentTime`` here, the next
    // sentence's chunks land before the previous one's tail finishes
    // and the user hears two sentences on top of each other. Carrying
    // the previous schedule forward chains them seamlessly; the
    // ``max(..., ctx.currentTime)`` guard keeps the value sane when
    // a long pause between turns let the previous schedule fall into
    // the past.
    const carryOver = Math.max(prev.nextStartTime, ctx.currentTime);
    this._streams[tag] = {
      sampleRate: Math.max(8000, sampleRate || ctx.sampleRate),
      channels: Math.max(1, channels || 1),
      nextStartTime: carryOver,
      active: prev.active.filter(
        (src) => (src as unknown as { _stopped?: boolean })._stopped !== true,
      ),
    };
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

  private async _enqueuePcm(tag: StreamTag, body: Uint8Array): Promise<void> {
    if (body.byteLength < 2) return;
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
