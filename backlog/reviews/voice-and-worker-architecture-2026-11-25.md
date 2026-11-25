# Voice and worker architecture review, 25 November 2026

## Overall structure

The browser owns audio devices and playback scheduling. On the server, the mic
source owns PCM buffering and resampling, the brain loop dispatches conversation
events, the turn runner owns generation, and the TTS queue owns sentence order.
Each of those boundaries was usable in the same test with a disposable database
and a replaced model client. [Voice transport](../../docs/voice-mode.md) and
[brain orchestration](../../docs/brain-orchestration.md).

The integration cost is concentrated in the session controller. It inherits
30 mixins and its constructor spans 886 lines including comments. Even a test
that only needs a voice conversation has to initialize fields used by history,
metrics, merge handling and WebSocket startup. Moving methods to more mixin
files would leave those initialization dependencies in place. A small voice
session object with explicit capture, turn submission and playback dependencies
would let callers build and test this flow independently of avatar and idle-life
startup. [Session controller](../../app/core/session/session_controller.py)
and [round-trip fixture](../../tests/test_voice_roundtrip.py).

## Worker scheduling

The idle scheduler separates compute and inference budgets, runs selected
workers sequentially, and checks for returning user activity between jobs.
Voice mode holds new idle work while the mic is active. A job already running
can finish after the user returns. Separate threads handle capture, brain
events and TTS playback, so audio I/O can already overlap a conversational
turn. [Idle scheduler](../../app/core/proactive/idle_worker_scheduler.py)
and [idle admission](../../app/core/session/lifecycle_mixin.py).

Of 158 idle tick records retained across the local application logs, 26 from
22 November included per-lane counters. Those recorded 114 LLM budget
deferrals, zero compute deferrals and zero global-cap deferrals. A candidate
can be deferred on several ticks. Two concept-synthesis ticks took 67 and 147
seconds. This sample supports investigating long model calls before adding
compute workers. If compute pressure emerges, start with one calculation on
an immutable snapshot, and let the owning worker commit the result. The
existing proposal tracks shared store and cache constraints. [Parallel
compute-lane proposal](../personality/perf.md#p46-parallel-compute-lane-drain).

The installed worker and workflow routes use one 9B model, and Live policy
uses a 4B model on the same Ollama endpoint. The previous resource key let
Live policy bypass the worker gate. Ungrouped Ollama worker routes now share
the endpoint key. At the default capacity of one, a pending policy request can
start after the active worker releases its stream. The gate orders waiting
work without interrupting an in-flight call. [Gate and stream ownership](../../app/llm/llm_gate.py).

## Voice turn ownership

The separation of mic transport, recognition, prompt assembly, turn execution,
and synthesis made an offline replay possible. The harder boundary is between
`LiveSession` and `SessionController`. Capture returns a path and elapsed
time, the turn blocks on a brain-loop future, and client playback ends on a
separate acknowledgement. Each describes a different point at which the user
can interrupt. Two defects surfaced while wiring the replay. The transcript
event followed the generated answer, and each WAV allocation left its original
file descriptor open. Both were corrected in this pass. [Voice capture](../../app/core/session/voice_capture_mixin.py)
and [microphone source](../../app/audio/client_mic_source.py).

## Interruptions

- **Speaking over the tail of a reply.** In an isolated probe with barge-in
  enabled, a qualifying 1.2-second phrase already queued and the client drain
  acknowledgement withheld, the drain loop consumed all 600 simulated 50 ms
  waits. It issued zero stop calls and left the phrase queued until timeout.
  [Live session](../../app/core/session/live_session.py).
- **Distinguishing speech from time spent listening.** For a completed capture,
  the minimum-speech check used wall time from the start of listening
  through endpoint silence. A long pause before a short sound could therefore
  satisfy the duration check.
  [Capture timing](../../app/core/session/voice_capture_mixin.py).

The voice follow-up addressed both findings. The mic source reports detected
speech duration as it captures. The live session keeps that duration with
each queued WAV, decides when to cancel buffered playback, and hands the same
WAV to the next turn. A two-turn WebSocket replay covers speaking over the
playback tail with interruption enabled and disabled. [Current behavior and
replay instructions](../../docs/voice-mode.md#interrupting-a-spoken-reply).

## Replay scope

The WebSocket regression uses disposable SQLite and a scripted streaming chat
model. It exercises transcript ordering, a follow-up prompt with prior history,
saved rows, binary audio framing and a simulated client drain acknowledgement.
The opt-in variant prewarms RealtimeSTT and Pocket-TTS, uses them for both
turns, then recognizes the received PCM to check the words. Prompt
assembly and queuing run in process. The fixture omits unrelated companion
workers. Earlier runs that synthesized new microphone input each time produced
“round fox” and “round box” for “brown fox” with Whisper base. In the reverse
direction, base also decoded “The fox is brown” as “The box is brown” on one
rendition. Whisper large-v1 recognized “That fox is brown!” on the fixed-seed
reply. The replay requires the full subject and predicate on acoustic readback
and exact streamed and saved reply text. It uses fixed synthetic recordings,
a fixed synthesis seed and Aiko's default Whisper large-v1. Room echo, actual speaker
audibility and browser device permissions still need a device session.
[Replay and commands](../../docs/voice-mode.md#testing).
