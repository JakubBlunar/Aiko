/**
 * AudioWorklet that converts the input audio (Float32, native sample rate)
 * into Int16 LE PCM frames of a fixed wall-clock duration and ships them
 * to the main thread via `port.postMessage`.
 *
 * The main-thread `AudioInputManager` is responsible for resampling the
 * captured chunks if the worklet's native rate is not exactly 48 kHz,
 * but in practice Chromium/Firefox honour the `sampleRate` constraint
 * we pass through `getUserMedia`, so this worklet ships whatever rate
 * the context was created at.
 *
 * Frame size: ~50 ms per chunk. At 48 kHz that's 2400 samples.
 */

class MicPcmWorklet extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const params = (options && options.processorOptions) || {};
    this._frameSamples = Math.max(
      128,
      Math.floor(((params.frameDurationMs || 50) / 1000) * sampleRate),
    );
    this._buffer = new Float32Array(this._frameSamples);
    this._written = 0;
    this._rms = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) {
      return true;
    }
    // Down-mix any stereo capture to mono — the audio constraints
    // request mono but some virtual cables hand us stereo regardless.
    const channelCount = input.length;
    const samplesPerChannel = input[0].length;
    for (let i = 0; i < samplesPerChannel; i++) {
      let acc = 0;
      for (let c = 0; c < channelCount; c++) {
        acc += input[c][i];
      }
      const mono = acc / channelCount;
      this._buffer[this._written++] = mono;
      if (this._written >= this._frameSamples) {
        this._emit();
      }
    }
    return true;
  }

  _emit() {
    const frame = new Int16Array(this._written);
    let sumSq = 0;
    for (let i = 0; i < this._written; i++) {
      const s = Math.max(-1, Math.min(1, this._buffer[i]));
      frame[i] = (s * 0x7fff) | 0;
      sumSq += s * s;
    }
    this._rms = Math.sqrt(sumSq / Math.max(1, this._written));
    // Transfer the underlying buffer so the main thread doesn't have
    // to copy. The frame's .buffer is exactly `this._written * 2`
    // bytes after we re-allocate per frame above.
    this.port.postMessage(
      { pcm: frame.buffer, rms: this._rms, sampleRate: sampleRate },
      [frame.buffer],
    );
    this._written = 0;
  }
}

registerProcessor("mic-pcm-worklet", MicPcmWorklet);
