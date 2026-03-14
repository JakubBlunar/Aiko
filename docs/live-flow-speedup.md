# Live flow speed analysis and proposals

## Current pipeline (sequential)

```
Capture (VAD/PTT) → WAV → STT (transcribe) → Prosody (optional) → LLM (full response) → TTS (full text)
```

**Time to first spoken word** = capture_done + STT_ms + prosody_ms + LLM_full_ms + TTS_first_chunk_ms.

Everything runs one after the other; the user hears nothing until the entire LLM reply is generated and TTS has started.

---

## Bottlenecks

| Stage | What happens | Typical cost | Notes |
|-------|----------------|---------------|--------|
| **Capture** | VAD or PTT records until silence/release | 1–6 s | User-controlled; can tune silence threshold. |
| **STT** | `RealtimeSTT.transcribe(wav_path)` (Whisper) | 0.5–3 s | Depends on length and model (e.g. large-v1). |
| **Prosody** | `_analyze_prosody(wav_path, text)` | ~50–200 ms | Only if prosody enabled; blocks before LLM. |
| **LLM** | `run_agent(..., stream=False)` | 2–15+ s | Full reply before any output. **Largest single delay.** |
| **TTS** | `speak_async(full_response)` | Starts after full reply | Kokoro speaks the whole string; first audio after LLM done. |

The dominant latency is **LLM + TTS**: we wait for the complete answer before starting speech.

---

## Proposals (by impact)

### 1. Stream LLM → TTS (stream-to-speak) — **highest impact**

**Idea:** Use `agent.run(stream=True)`, consume content chunks as they arrive, split into speakable sentences (e.g. with `drain_tts_stream_chunks`), and call TTS for each chunk while the rest of the reply is still generating.

**Effect:** Time to first spoken word becomes:  
capture + STT + prosody + **time_to_first_sentence** + TTS_first_chunk,  
instead of capture + STT + prosody + **full_reply** + TTS_first_chunk.  
Often saves **2–8+ seconds** for long answers.

**Changes:**

- **agno_agent.py**: When `stream=True`, do not use a single `agent.run()` returning a string. Call `agent.run(..., stream=True)`, get an iterator of events, and for each event with content (e.g. `RunEvent.run_content`), call an `on_token`/`on_content` callback with the delta. Optionally return the full concatenated content at the end for transcript/metrics.
- **session_controller.chat_once_streaming**: For live (or all) mode:
  - Call `run_agent(..., stream=True, on_content=...)`.
  - Maintain a buffer of streamed content; on each chunk call `drain_tts_stream_chunks(buffer, flush=False)` and for each drained sentence call `_tts.speak_async(chunk)` (or enqueue so TTS runs in order). On stream end call `drain_tts_stream_chunks(buffer, flush=True)` and speak any remainder.
  - Keep `on_token` for UI (e.g. transcript) if needed; it can receive the same deltas or the accumulated text.
- **TTS ordering:** Kokoro’s `speak_async` runs in a thread; if we call it multiple times in quick succession we need to ensure chunks are played in order (queue in TTS or in the session layer). Today Kokoro may effectively “replace” current speech; we need a small queue so chunk 2 starts after chunk 1 finishes (or use an explicit queue in Kokoro / session).

**Risks:** Tool calls during streaming may change how we buffer content (e.g. wait for tool result before speaking). We can either (a) buffer until no tool call in progress, or (b) only stream content events and speak those; tool calls already show status in the UI.

---

### 2. Make prosody optional or skip in live mode — **low effort, small gain**

**Idea:** Prosody adds 50–200 ms and is used only to add a short “[Vocal: …]” hint to the LLM. In live mode we can skip it or run it in parallel with the very start of the LLM (more complex).

**Effect:** Saves prosody time every turn when disabled.

**Changes:**

- Add a config/setting, e.g. “Use prosody in live mode” (default True for backward compatibility), or reuse existing prosody enable. In `process_live_capture`, if “live and prosody disabled for live”, set `prosody = None` and do not call `_analyze_prosody`; pass no vocal hint to `chat_once_streaming`.
- Alternatively, document that turning off prosody in Settings reduces latency.

---

### 3. Faster STT for live mode — **medium impact, configurable**

**Idea:** RealtimeSTT uses a Whisper model (e.g. `large-v1`). Smaller models (e.g. `base`, `small`) are faster but less accurate. Allow a “live” or “fast” STT profile that uses a smaller model.

**Effect:** Can cut STT time roughly in half (or more) at the cost of some accuracy.

**Changes:**

- Settings already have STT model; add a “Live mode STT model” override (e.g. `small` or `base`) used only in `process_live_capture` / RealtimeSTT when in live mode. Or a single “fast” profile that switches the model for all STT. Implementation depends on how RealtimeSTT is constructed (per-call model or shared instance).
- If the service is shared, we could create a separate “fast” RealtimeSTt instance for live and use it only in the live path.

---

### 4. TTS warmup and chunk queue — **support for stream-to-speak**

**Idea:** Ensure the first TTS chunk doesn’t pay one-off model load, and that multiple chunks play in order without overlapping or being skipped.

**Changes:**

- Keep or add TTS warmup at app start (or when entering live mode) so the first `speak_async` is fast.
- When we add stream-to-speak, implement a small queue in the session (or in Kokoro): “pending TTS chunks”. Each time a sentence is drained from the stream, append it; a worker or callback runs `speak_async` for the next chunk when the previous one finishes (using `on_done` or similar). Kokoro’s `speak_async` already has `on_done`; we can chain: on_done → pop next chunk and speak, or enqueue in Kokoro if it supports a queue.

---

### 5. VAD / silence tuning — **already configurable**

**Idea:** Lower `vad_silence_seconds` so we send the phrase to the pipeline sooner after the user stops speaking. This reduces perceived latency but may cut off slow speakers.

**Changes:** No code change; document in Settings (Audio tab) that “Silence to stop” affects how quickly the assistant responds vs. risk of cutting off speech. Optionally expose a “Live mode silence” override.

---

## Recommended order of implementation

1. **Stream LLM → TTS** (stream-to-speak) — biggest win; requires Agno stream handling and a TTS chunk queue.
2. **Prosody optional in live** — quick config/skip to save a small fixed delay.
3. **Faster STT profile for live** — optional setting for users who prefer speed over accuracy.
4. **TTS queue and warmup** — do as part of (1) and at startup.

---

## Summary

| Proposal | Effort | Latency saved | Notes |
|----------|--------|----------------|--------|
| Stream LLM → TTS | Medium | 2–8+ s (time to first sentence) | Need stream handling in agno_agent + session and ordered TTS queue. |
| Skip/optional prosody in live | Low | ~50–200 ms | Config or live-only skip. |
| Faster STT model for live | Low–medium | ~0.5–1.5 s | Optional “fast” / “live” STT model. |
| TTS queue + warmup | Low (with 1) | First chunk faster, no overlap | Part of stream-to-speak. |
| VAD/silence tuning | None (doc) | User-dependent | Document only. |

The single change that most reduces perceived latency is **streaming the LLM output into TTS by sentence** so the user hears the first part of the answer while the rest is still being generated.
