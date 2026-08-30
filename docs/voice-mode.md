# Voice mode (client-owned audio)

This document describes how voice input and output flow between Aiko
and her clients (browser tab or the Tauri desktop window). It replaces
the earlier "server-side `sounddevice`" design — the backend no longer
talks to the host's audio hardware at all.

For the *engine* that produces the samples — what pocket-tts can and
cannot control, and what the alternatives look like — see
[`tts-engine-options.md`](tts-engine-options.md).

## Why client-owned audio?

The original prototype captured the microphone and played TTS on the
server using `sounddevice`. That worked for a single-machine setup but
broke down quickly:

- **Multi-window experience.** Both the chat view and the floating
  persona window can be open simultaneously; only one of them can
  hold the mic, but both should hear Aiko speak.
- **Remote browser clients.** Running the backend on a workstation
  and the UI in a laptop browser meant the wrong machine's
  microphone was active.
- **Audio quality.** Server-side capture had no access to modern
  browser DSP (echo cancellation, noise suppression, AGC).
- **Permission story.** Browsers gate microphone access behind a
  user gesture; the server had no analogue.

The refactor moves all audio I/O into the client. The server keeps
ownership of the speech pipeline (STT, the agent, TTS synthesis), but
the audio bytes themselves now travel over the existing WebSocket as
binary frames.

## Architecture

```
Client (browser / Tauri webview)              Server (FastAPI)
+---------------------------------+           +-----------------------------+
| AudioInputManager               |           | _Hub (voice_owner_id)       |
|  - getUserMedia (48 kHz mono)   |  binary   |   - mic_start / mic_pcm     |
|  - mic-pcm-worklet -> Int16 LE  | --------> |   - resample 48k -> 16k     |
|  - mic_start / mic_pcm frames   |    WS     |   - feeds ClientMicSource   |
+---------------------------------+           |                             |
                                              |  ClientMicSource            |
                                              |  -> RealtimeSttService      |
                                              |  -> SessionController       |
                                              |  -> PocketTtsService        |
+---------------------------------+   binary  |  PocketTtsService.pcm_listener
| AudioOutputManager              | <-------- |  EarconPlayer.pcm_listener  |
|  - AudioContext + scheduler     |    WS     |   - tts_pcm / earcon_pcm    |
|  - setSinkId(output_device)     |           |   - audio_start / audio_end |
+---------------------------------+           +-----------------------------+
```

## Binary WebSocket frame protocol

All audio data travels on the same WebSocket as JSON envelopes — text
frames stay JSON, binary frames are PCM. Each binary frame starts with
a 1-byte type discriminator (see `app/web/audio_frames.py` and
`web/src/audio/protocol.ts`):

| Byte | Direction         | Name           | Payload                                          |
|------|-------------------|----------------|--------------------------------------------------|
| 0x01 | client -> server  | `mic_pcm`      | Int16 LE PCM samples                             |
| 0x02 | client -> server  | `mic_start`    | `[u32 sample_rate][u8 channels][u8 dsp_flags]`   |
| 0x10 | server -> client  | `tts_pcm`      | Int16 LE PCM samples                             |
| 0x11 | server -> client  | `earcon_pcm`   | Int16 LE PCM samples                             |
| 0x12 | server -> client  | `audio_start`  | `[u8 stream][u32 sample_rate][u8 channels]`      |
| 0x13 | server -> client  | `audio_end`    | `[u8 stream]`                                    |

- All multi-byte integers are **big-endian** (network order).
- `dsp_flags` is a bitset: bit 0 = echo cancellation, bit 1 = noise
  suppression, bit 2 = auto gain control. The server uses it for
  logging only; the actual DSP runs in the browser.
- `stream` in `audio_start` / `audio_end` is either `0x10` (TTS) or
  `0x11` (earcon) so the client can route the chunks to separate
  scheduling queues.

The wire format is intentionally trivial — there is no length prefix
because WebSocket frames are message-framed already, and there is no
sequence number because the underlying TCP stream preserves order.

## Voice ownership

Multiple clients connect to the same server (chat tab + persona
window, or two browsers on different machines). The server assigns
each socket a random `client_id` in the `hello` envelope and
maintains a single `voice_owner_id` slot on the hub.

- Calling `voice_start` claims the slot. If another client owned it,
  it is preempted (takeover). The server broadcasts a
  `voice_owner_changed` JSON event so every connected client knows.
- Calling `voice_stop` releases the slot. Disconnecting releases it
  too — the `finally` block in `websocket_endpoint` calls
  `_broadcast_voice_owner_async` inline so the other windows learn
  about the release before the next event loop tick.
- The server **drops** `mic_pcm` / `mic_start` frames from any
  client whose id does not match `voice_owner_id`. The UI gates this
  too (the mic button on a non-owner window renders the
  "take over" affordance instead of the active state).

Transcripts and every other JSON state event go to **all** connected
clients regardless of ownership. The `voice_owner_id` lock only governs
microphone *input*. Audio **output** has its own owner — see below.

## Audio output ownership

TTS / earcon PCM is **not** broadcast to every client. Without a lock,
the desktop shell's hidden persona webview AND the visible main window
both receive and play each clip ~tens of ms apart — audible as an
echo/mumble on the first sentence of every turn. So the hub elects a
single **audio owner** and sends binary audio frames only to it
(`_Hub.send_audio_bytes` drains a single ordered queue so a slow hop
cannot deliver clip N's last PCM after clip N+1's `audio_start`; lipsync
`audio_amplitude` JSON still broadcasts so a hidden persona can keep
animating its mouth).

The election (`_Hub._elect_audio_owner_locked`) is **most-recently-active
wins**:

- Each client's "active" stamp (`_last_active_by_client`) is bumped when
  its window goes visible/focused (a `presence` `visible: false -> true`
  transition) or when the user unmutes it. Connecting alone does **not**
  stamp — so a second, still-hidden window can't steal audio from the
  incumbent before it reports activity (ties break toward the current
  owner, then insertion order).
- The owner is the highest-stamped **unmuted** client, preferring a
  currently-visible one and falling back to any unmuted client (incl.
  hidden) so audio is never silently lost when no window is foregrounded.
- Result: audio follows whichever device you most recently picked up,
  while still only ever playing on **one** device.

**Per-device mute.** Each client carries an `audio_muted` flag, toggled
from Settings → Sound (all platforms) and persisted per-device in
`localStorage` (key `aiko.audio.muted`). The client pushes it up via the
`audio_mute` WS command on every (re)connect and whenever it toggles. A
muted client is dropped from the election entirely: another device keeps
playing, or — if every device is muted — `owner=None` and everything
goes silent. A muted client also gates playback locally
(`shouldPlayAudio`) so the gap between toggling and the re-election
landing is silent too. Owner changes broadcast `audio_owner_changed`.

Tests: `tests/test_web_server_audio_owner.py` (`AudioOwnerElectionTests`
+ `AudioMuteTests`).

## Client side

- `web/src/audio/AudioInputManager.ts` — owns `getUserMedia`, the
  `mic-pcm-worklet`, and the `mic_start` / `mic_pcm` framing. Pulls
  device id + DSP toggles from `DeviceManager`.
- `web/public/mic-pcm-worklet.js` — `AudioWorkletProcessor` that
  converts the worklet's float32 input to Int16 LE in ~50 ms frames
  and posts them back to the main thread with an RMS hint for the
  level meter.
- `web/src/audio/AudioOutputManager.ts` — single shared
  `AudioContext` with a chained `AudioBufferSourceNode` scheduler.
  Honours `setSinkId` so the user-picked speaker is respected.

  **Backgrounding on iOS, and why the context is replaced unasked.**
  `onBackground()` releases the inaudible keep-alive loop and the lipsync
  RAF, which are otherwise a permanently awake audio endpoint plus a 60 Hz
  wakeup for as long as the tab exists. That leaves the graph producing
  nothing, and iOS reclaims the audio unit of a backgrounded PWA that is
  idle. What comes back is a **context that reports `"running"` and plays
  silence**: the `_needsResume` check (which covers iOS's non-standard
  `"interrupted"` state) is satisfied, `_enqueuePcm` schedules every frame
  instead of dropping it, and restarting the keep-alive relights the
  phone's media indicator — so the device looks like it is playing and the
  only cure the user can find is force-quitting.

  This was first addressed with a liveness probe: a dead context was
  assumed to have a frozen `currentTime`, so `onForeground()` watched the
  clock for 250 ms and rebuilt when it had not moved. **That is not
  sufficient, and the reason is worth keeping.** iOS also returns contexts
  whose clock advances normally while the output no longer reaches the
  speaker. A live clock proves the graph is *rendering*, not that anything
  is *audible*, and Web Audio exposes nothing downstream of the render
  that a page can read — the analyser sits before `destination` and
  happily reports the samples being fed into a void. So the probe was
  removed in favour of replacing the context after **any** background
  stint, via `_replaceContext("foreground")`. `restart()` is the same
  operation on an explicit "Restart sound" tap.

  The cost is stated rather than hidden: a replacement context needs a
  gesture to start unlocked on iOS, so returning to the app can land on
  "Sound is off" until the first tap (the persistent gesture unlock in
  `useAssistantSocket` then resumes it, so any tap will do). Honest
  silence with a readout and a one-tap fix beats a context that swallows
  every frame for the rest of the session.

  Two invariants to preserve if you touch this:
  - **Do not reintroduce a health check as the gate.** Every signal a
    page can read says "healthy" in the failure this exists for. If a
    future WebKit exposes a real output-liveness signal, gate on that —
    not on the clock.
  - **Timestamps do not survive a context swap.** `nextStartTime` is
    absolute on the context's own clock and a fresh context restarts at 0,
    so `_ensureContext` resets the per-stream schedules. Carrying one over
    schedules every chunk a session-length into the future, which trades a
    dead context for a silent one.
- `web/src/audio/DeviceManager.ts` — `enumerateDevices`,
  permission queries, and the localStorage persistence for the
  input/output device ids plus the three DSP toggles.
- `web/src/hooks/useMicCapture.ts` — Zustand glue: tears the
  `AudioInputManager` up when this client owns the mic, tears it
  down when ownership is lost.
- The mic button (`MicButton.tsx`) and both ChatView / PersonaWindow
  consume `clientId` + `voiceOwnerId` from the store to render the
  ownership state.

## Server side

- `app/audio/client_mic_source.py` — replaces the old
  `MicrophoneCapture`. Exposes the same surface
  (`capture_phrase`, `read_chunk`, ...) but reads from an internal
  queue fed by the WebSocket layer. The `_QueuedInputStream` helper
  mimics `sounddevice.InputStream` so the existing capture loops in
  `RealtimeSttService` keep working unchanged.
- `app/tts/pocket_tts_service.py` and `app/audio/earcons.py` — both
  now emit Int16 LE PCM through a `pcm_listener` callback instead of
  calling `sounddevice.play`. The `SessionController` wires those
  listeners to `_emit_audio_frame`, which builds the appropriate
  binary frame and hands it to the hub for broadcast.
- `app/web/audio_frames.py` — single source of truth for the wire
  format on the Python side. The TypeScript twin lives in
  `web/src/audio/protocol.ts`.
- `app/core/infra/settings.py` — `AudioSettings` no longer carries
  `microphone_device` / `output_device` / `live_ptt_*`. `load_settings`
  migrates the old keys out of `user.json` on first run so the file
  stays clean.

## Audio quality

- Microphone capture runs at **48 kHz Int16 mono**. The server
  resamples to 16 kHz for STT using `scipy.signal.resample_poly`.
- TTS clips are emitted at whatever sample rate the synthesis model
  produces (currently 22050 Hz for the bundled "pocket" TTS, scaled
  by the user's speed setting). The client retains that rate when
  constructing each `AudioBuffer`, so no extra resample is needed.
- Browser DSP defaults: echo cancellation, noise suppression, and
  auto gain control are all **on** out of the box. The user can
  toggle each one independently in the Settings drawer; the choices
  persist in `localStorage` and surface to the server in the
  `mic_start.dsp_flags` byte.

## Testing

- Backend: `tests/test_audio_frame_protocol.py`,
  `tests/test_client_mic_source.py`,
  `tests/test_web_server_voice_owner.py`,
  `tests/test_tts_pcm_listener.py`.
- Frontend: `web/src/audio/protocol.test.ts`,
  `web/src/audio/DeviceManager.test.ts`,
  `web/src/audio/AudioOutputManager.test.ts`.

Run with `python -m pytest tests/` and `npm test --prefix web`.

## Removed surfaces

The following were deleted as part of the refactor; do not reintroduce
them without revisiting this design:

- `GET /api/audio/devices` endpoint.
- `audio.microphone_device` / `audio.output_device` in `AppSettings`.
- `audio.live_input_mode`, `audio.live_ptt_*` keys.
- `app/audio/mic_capture.py` as a real module — it is now a
  compatibility shim that re-exports `ClientMicSource`.
- The `sounddevice` Python dependency.
