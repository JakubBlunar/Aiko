# Live speaking: push-to-talk and voice detection

## Current state

- **Record button**: Fixed 5s recording → RealtimeSTT `record_until_silence` → chat → transcript + TTS. Blocked while Live mode is on.
- **Live mode** (existing): Continuous listening with **voice-activity detection (VAD)**. `LivePracticeWorker` runs a loop that:
  1. Calls `session.capture_live_phrase()` → mic capture with VAD (level threshold, silence duration, optional WebRTC VAD) → writes phrase to WAV.
  2. Pushes `(wav_path, capture_ms)` to a queue; main loop calls `session.process_live_capture()` (transcribe WAV → `chat_once_streaming` → TTS).
  3. UI shows “You (live)” and streams assistant reply; TTS plays. Capture pauses while a turn is processing so TTS is not re-captured.

- **VAD settings** (already in config): `audio.vad_level_threshold`, `audio.vad_silence_seconds` (used by live and by Record). No UI yet for “push-to-talk vs voice detection” in live mode.

## Goal

- **Live mode** remains the main “speaking” mode: when STT detects what you said, it sends it to the model and you hear the reply.
- Make it **configurable**:
  - **Voice detection** (current): continuous listen; VAD segments speech and sends each phrase to the model.
  - **Push-to-talk (PTT)**: record only while the user holds a key (or button); on release, send that segment to the model.

## Plan

### 1. Config and settings

- Add a **live input mode** setting:
  - Options: `"voice_detection"` | `"push_to_talk"`.
  - Persist in config (e.g. `audio.live_input_mode` or `audio.live_mode`) and in `AudioSettings` / `AppSettings`.
- **Push-to-talk** sub-options (when mode is PTT):
  - **Activation**: key (e.g. space, F2) and/or a visible “Hold to talk” button in the UI.
  - Optional: “Press once to start, press again to stop” (toggle) as an alternative to “hold”.
- Expose in **Settings UI**: dropdown or radio for “Live mode: Voice detection” vs “Push-to-talk”, and (for PTT) key binding and/or toggle option.

### 2. Session / capture layer

- **Voice detection path** (current): keep `capture_live_phrase()` as-is; it already uses VAD and returns when a phrase is done. No change except it’s now explicitly “live mode = voice detection”.
- **Push-to-talk path**:
  - New API (or mode) that records **only while PTT is active** (key held or button held; or from start press to stop press if toggle).
  - Options:
    - **A)** Reuse `record_until_silence()` (RealtimeSTT) with a “stop when PTT released” callback instead of “stop on silence” — requires RealtimeSTT to support an external stop signal, or a wrapper that stops feeding after release.
    - **B)** Use the same mic capture as in `mic_capture.py` (or a small buffer) and record until PTT release, then write WAV and pass to the same pipeline as live (`process_live_capture` or a variant that takes WAV + optional capture_ms).
  - Prefer **B** if RealtimeSTT doesn’t support external stop; then PTT is “record from press to release → WAV → transcribe → chat → TTS”, same downstream as current live.

- **SessionController**:
  - For live mode, accept a “live_input_mode” (or read from settings) and either:
    - run the existing capture loop (voice detection), or
    - run a PTT loop that waits for “PTT start” → record until “PTT end” → enqueue (wav_path, capture_ms) for the same `process_live_capture` pipeline.

### 3. UI

- **Live mode entry**: ensure there is a clear “Start Live” (or “Live”) button that starts the existing live loop; disable Record and type-to-send while live is on (already done). Add a “Stop Live” button (already referenced as `_stop_live_button`); create it if missing.
- **Settings**:
  - “Live input”: “Voice detection” / “Push-to-talk”.
  - If PTT: “Push-to-talk key” (key binding) and/or “Use Hold-to-talk button” and optionally “Toggle instead of hold”.
- **Push-to-talk in UI**:
  - If PTT key: register a global or window key listener; “key down” = start recording, “key up” = stop and send (or toggle logic).
  - Optional visible “Hold to talk” button: mouse press = start, mouse release = stop and send. Helps users who don’t want to use a key.

### 4. Implementation order

1. **Config + settings**: add `live_input_mode` (`voice_detection` | `push_to_talk`) and PTT options to `AudioSettings` and config load/save; no behavior change yet.
2. **Settings UI**: add controls for live mode and PTT options; persist on save.
3. **PTT capture**: implement “record from start to stop” (by callback or key/button) and feed result into existing `process_live_capture` (or equivalent) so one code path handles both “VAD phrase” and “PTT segment”.
4. **Live worker**: in `LivePracticeWorker`, branch on `live_input_mode`: if voice detection, keep current `capture_live_phrase()` loop; if PTT, run a loop that waits for PTT start → record until PTT end → enqueue (wav_path, capture_ms) and reuse the same processing.
5. **UI Live/Stop buttons**: ensure “Start Live” and “Stop Live” are visible and wired; add PTT key binding and optional “Hold to talk” button when in PTT mode.

### 5. Edge cases

- **TTS playback**: already handled — capture is paused while a turn is processing, so TTS is not re-captured. Same for PTT: while processing, PTT can be ignored or queued.
- **PTT key vs other shortcuts**: avoid conflicts with Send, Record, Clear, Settings; document the chosen PTT key (e.g. F2 or a user-chosen key).
- **Microphone permission**: same as today; if mic is disabled, show clear error when starting Live or PTT.

### 6. Testing

- Voice detection: start Live, speak; after silence, phrase is sent and reply plays.
- Push-to-talk: start Live (PTT mode), hold key/button, speak, release; phrase is sent and reply plays.
- Switch mode in settings and restart Live; confirm correct behavior for each mode.

---

**Summary**: Add configurable “voice detection” vs “push-to-talk” for live mode. Keep current VAD-based flow as the default; add a PTT path that records only while the user holds a key/button (or toggles), then reuses the same STT → chat → TTS pipeline. Expose mode and PTT options in Settings and wire a visible Live start/stop and optional “Hold to talk” button.
