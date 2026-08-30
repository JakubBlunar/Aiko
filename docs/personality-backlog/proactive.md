# Proactive + presence follow-ups

Deferred follow-ups from the typed-proactive / activity awareness pass
(C1, see [`shipped.md`](shipped.md)). C5 (per-tab presence aggregation)
was dropped during the May 2026 cleanup — re-derive it from a real bug
report if multi-tab presence ever becomes a real complaint.

**Start with [C6](#c6-companion-mode--the-desktop-as-a-sensory-channel).**
C2-C4 are small deferred knobs; C6 is a design for the whole channel and
it subsumes C2.

---

## C2. Window-title-aware activity

**Subsumed by [C6](#c6-companion-mode--the-desktop-as-a-sensory-channel) phases 1–2.**
Titles are now collected behind `activity.title_allowlist`, redacted
before persist, and shown in the settings readout. They still do **not**
enter the prompt `activity_block` (app name only). Remaining C6 work is
interpretation, cues, UIA, the live pull in
[C7](#c7-live-activity-pull--get_activity-tool), OS idle as a
gap-cue qualifier in [C8](#c8-os-idle-as-a-gap-cue-qualifier), and
duration as wellbeing evidence in
[C9](#c9-activity-duration-as-wellbeing-evidence-k72).

---

## C3. Persisting last-fired typed cooldown to disk

**Motivation.** Today the typed-proactive cooldown lives in process
memory (`_last_typed_run_monotonic`) and resets on backend restart.
Fine for the 80% case but a quick restart in the middle of a typed
session can re-arm an immediate proactive nudge, which reads weirdly.

**Key files.** [`config/user.json`](../../config/user.json) (alongside
`last_active_id`), [`app/core/proactive/proactive_director.py`](../../app/core/proactive/proactive_director.py)
`_last_typed_run_monotonic` plus a `_last_typed_run_iso` mirror,
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
boot hook that loads the persisted timestamp.

**Sketched approach.** On every successful typed-proactive fire, write
`last_typed_proactive_at: <iso>` to `config/user.json` (debounced ~5s).
On boot, load it; convert to a monotonic offset so the existing
cooldown maths still work.

**Open questions.** Does it matter if the wall-clock between sessions
exceeds the configured cooldown by a large margin (e.g. a week)? We
already have the typed-proactive eligibility predicate guarding the
rest; this is purely about not re-firing back-to-back across a
restart.

---

## C4. TTS-on-typed-proactive toggle

**Motivation.** Typed proactive nudges are text-only by design today.
A "speak typed proactive nudges aloud" knob is cheap to add when the
use case appears (e.g. Jacob wants ambient audio presence even while
typing).

**Key files.** [`app/core/proactive/proactive_director.py`](../../app/core/proactive/proactive_director.py)
`_run_typed` (currently bypasses the TTS pipeline),
[`app/core/infra/settings.py`](../../app/core/infra/settings.py) `AgentSettings`
(new `proactive_typed_speak: bool = False`),
[`web/src/components/SettingsDrawer.tsx`](../../web/src/features/settings/SettingsDrawer.tsx)
Proactive section.

**Sketched approach.** A boolean switch in settings that, when on,
routes the typed-proactive reply through the regular TTS path. Keep
the prepared-nudge fast-path text-only either way (those are barely
worth speaking).

**Open questions.** Do we keep the default OFF (current behaviour) or
flip the default ON so the feature is discoverable? Probably OFF
forever — typed-proactive is *meant* to be text-only.

---

## C6. Companion mode — the desktop as a sensory channel

**Phases 1–2 shipped** (collection pipeline). Write-up:
[`shipped/proactive-tasks.md`](shipped/proactive-tasks.md#c6-companion-mode-collection-pipeline-phases-12).
Still open: phases 3–6 below, plus [C7](#c7-live-activity-pull--get_activity-tool)
(live pull / agent tool), [C8](#c8-os-idle-as-a-gap-cue-qualifier)
(OS idle as a gap-cue qualifier), and
[C9](#c9-activity-duration-as-wellbeing-evidence-k72)
(duration as K72 evidence).

**Motivation.** Every signal Aiko has about Jacob arrives through the
chat box. She knows what he *says* and when he says it, and past that
the world is dark. Companion mode gives her a second, much
lower-bandwidth sense: what he is doing on the machine she is running on.
Not so she can narrate it — so that the wellbeing, relationship and
concept machinery she already has finally has something to work with
between turns. The payoff is not "Jacob is in Cursor" in the prompt,
which already ships. It is that he opens the editor at 23:40 on the
project he said he was resting from, and the machinery that already
knows both of those facts can put them together.

The shape below came out of a design conversation and is deliberately
layered so that no expensive model sits in the perception loop:

```
OS events -> cheap collectors -> normalised activity events
          -> sessionizer ("coding, Aiko project, 18 min")
          -> local interpretation ("debugging, and stuck")
          -> her normal cognition (cue / memory / concept / nothing)
          -> she speaks only if she decides to
```

**Verdict: doable, and the fit is better than it has any right to be.**
Four of those five stages already exist in some form, and the one
architectural property the design needs most — *perception runs when he
is working, not when he is talking* — is already how the scheduler
behaves. The storage layer (phases 1–2) now exists. The remaining work
is aggregation / interpretation and the interruption bar on top, which
this codebase has repeatedly got wrong.

### What already exists

- **Level 0 collectors + event store.** Isolated Tauri
  `CollectorRuntime`, title allowlist, OS idle, session lock, schema
  v40 `activity_events` / `activity_sessions` with prune. Prompt
  `activity_block` is still app-name only. Privacy contract:
  [`presence-and-activity.md`](../../docs/presence-and-activity.md).
- **The demand-driven scheduling the design asks for.** "Only process
  activity when there is enough accumulated context, and skip it while
  he is talking to her" is a description of the idle-worker scheduler:
  `demand()` returning a `WorkSignal`, compute vs LLM lanes, depth tiers
  widening the budget the longer he is away. A perception worker does
  not need a scheduler built for it.
- **The cognition layer, entirely.** Cue pool with cooldowns and
  question balance, `MemoryStore.add`, concept synthesis, gap cues on a
  priority mutex. An observation has at least four legitimate doors in
  and none of them need inventing.
- **A local-model perception precedent.** Immersion **H25** already runs
  a local vision model over user-supplied images off the hot path, gated
  through `LlmPriorityGate`, with the result distilled into one memory.
  Level 2 is the same shape with a different sensor.

(Note for readers of this file: `H` is two series. Immersion H-numbers
live in [`immersion.md`](immersion.md); audit H-numbers live in
[`health.md`](health.md). Both are cited below and each is named.)

**One accident to protect.** `_touch_user_activity` has exactly one
caller — `chat_turn_mixin.py`, on a chat turn. Held by
`tests/test_activity_touch_invariant.py`. If anyone ever makes
`user_activity` frames reset the idle gate, this entire pipeline
silently stops running.

**And one ambiguity this fixes.** Idle depth conflates *away from the
keyboard* with *here but not talking to me* — both are just "no messages
for N minutes". `GetLastInputInfo` separates them for the first time,
which is worth something beyond companion mode: `sleep_return` fires on
a 5 h message gap and currently cannot tell a night's sleep from a long
afternoon in another window, and it is the highest-priority gap cue
after `turning_over`.

### The three things the sketch does not account for

**1. The store exists; the aggregator does not.** Phase 2 shipped
`activity_events` / `activity_sessions` with retention. Phase 3 still
needs a compute-lane worker whose `demand()` is "has anything
meaningful changed" — rollups over those sessions, not a second table.

**2. A new cue lands in an oversubscribed pool and will starve
invisibly.** The delightful version of this feature — *"you're doing the
thing again"* — is a gap-cue-shaped object competing in a six-way
priority mutex, and the audit file is a catalogue of exactly that going
wrong: **H7** (16 hypotheses invented, 0 ever asked), **H29** (the wants
ledger draining before pressure could accumulate), **H30** and **H32**
(two cue-accounting metrics that were both artefacts). None of those
were visible without instrumentation. So a companion cue gets a
`CueSpec`, a `CuePolicy` and a row in `cue_decisions` **in its first
commit**, and its arming signal is its provider's own first real gate
rather than the nearest available slot (audit shape 13). Assume the
first measurement says it never fires, and plan to find out why.

**3. Titles and UI Automation are content, not buckets — and what she
writes down is durable.** "Chrome" is a coarse category. "Barclays —
Payments" is a fact about his finances, and a UIA text node is the
sentence on his screen. Collection now redacts before persist and
stores titles only for allowlisted apps. The sharper remaining problem
is downstream: anything the interpretation worker concludes becomes a
**memory** — mirrored into LanceDB, retrievable by RAG, surfaceable
months later. Phase 4/5 must keep that bar.

### The collector is its own thing, and must fail on its own

The single most important structural point, and it is about blast radius
rather than cost. **Nothing in the perception path may be able to stall
the shell, the WebSocket, or a turn.** The desirable failure mode is
that a hostile application costs Aiko one sensory detail and nothing
else — she keeps talking, and the collector shrugs. That means the
collector is a background worker inside the Tauri process with its own
queue, never anything the UI thread or the WS send path awaits, and
everything downstream treats its output as *optional and possibly
stale*. Get this wrong once and the feature becomes "Aiko freezes when I
open Outlook".

Three rules follow, and they cost almost nothing if designed in:

- **Escalate, never sweep.** Always-on is only the cheap tier: app,
  title, process, idle. Anything expensive is triggered *by a change*
  in the cheap tier, not by a clock. This is the same "has anything
  meaningful changed" predicate phase 3 already needs as its `demand()`
  probe, so it should be **one detector with two consumers** — deeper
  inspection and model interpretation — rather than two thresholds that
  drift apart.
- **Cache per surface, and be honest about what invalidates.** Keyed by
  window handle plus title plus focus-entry, with a TTL. The trap: a
  title is a poor change signal for exactly the case that motivates
  deeper inspection — a terminal's contents change constantly while its
  title does not. The v1 answer is a TTL and a re-read on refocus,
  explicitly **not** UIA event subscriptions, which are cross-process
  callbacks that keep the observed app's accessibility engine hot and
  are a well-known source of pathological slowdowns.
- **Target the handful of apps actually used**, everything else falls
  back to window and process. This turns out to be the *same list* as
  the privacy allowlist, which is a happy result: one positive,
  user-visible list decides both what is safe to read and what is worth
  the effort of reading. Two lists here would eventually disagree.

### On UI Automation specifically

A reality check, because it is the most exciting part of the sketch and
the least likely to pay. UIA is COM: apartment threading, never on a UI
thread, and every property read is a cross-process call, so a naive tree
walk of a browser or editor runs to hundreds of milliseconds and must be
batched through `CacheRequest` / `FindAllBuildCache`. Bound it with
`IUIAutomation2`'s connection and transaction timeouts rather than
hoping — a target that has stopped pumping messages will otherwise hang
the call indefinitely, and a hung COM call on a Rust thread cannot be
cancelled or killed, only leaked.

**Caching does not fix the cost that matters, because the cost is not
ours.** Electron apps (Cursor, VS Code, Discord, Slack) expose a tree
only once their accessibility engine is switched on, and Chromium
generally leaves it on for the remaining lifetime of the process. So the
penalty is paid at *first contact* and persists whether or not we ever
walk again — caching saves our milliseconds, not the user's editor.
Which is the argument for the per-app allowlist being the real control,
and for never touching an app on speculation. Games expose nothing at
all, so the coverage gradient runs opposite to interest.

Against all that, note what a window title alone already gives you. VS
Code puts `rag_store.py — assistant` in its title bar; that is the
sketch's "he's looking at concept-worker.ts", for a `GetWindowTextW`
call. Most of the worked examples are reachable from level 0. The
escalation design above makes UIA meaningfully safer to attempt than it
would be as a sweep, but it does not change the **sequencing**: you
cannot know which questions titles left unanswered until phases 1-5 have
run and produced a list. **Defer, and expect that list to be short.**

### Phases

1. **Level 0 collectors (Rust).** ✅ Shipped. Isolated `CollectorRuntime`,
   window title behind a positive allowlist, `GetLastInputInfo`, session
   lock/unlock. EscalationBus fires with zero subscribers (the UIA lock).
2. **Event store + sessionizer (Python, schema v40).** ✅ Shipped.
   Redact-before-persist, unknown sources dropped, focus-flicker
   collapse, `ActivityPruneWorker` from day one.
3. **Level 1 aggregation worker.** Compute lane, no model — rollups and
   a "has anything meaningful changed" signal, which is also the
   `demand()` probe for phase 4. The store now exists for this to read.
4. **Level 2 interpretation worker.** LLM lane, local worker model,
   triggered only by phase 3's change signal. Must be able to return
   *nothing* and must carry a confidence, because a confident wrong
   reading ("you've been gaming for three hours" — he was watching a
   tutorial) is worse than silence by a wide margin.
5. **Level 3 intake.** One memory path plus one gap cue, both
   instrumented. Stop here and measure for a fortnight.
6. **UIA.** Only if phase 5's data names questions titles could not
   answer. When built: new Rust `ActivitySource` (`Escalated` +
   `Dedicated`) subscribed to the EscalationBus, Python `SourceHandler`
   on the same allowlist, bounded digest in `payload`. Cache key:
   `surface_id + title + focus-entry`, TTL, re-read on refocus — **not**
   UIA event subscriptions. `IUIAutomation2` timeouts; hang = leak that
   thread. Do not stub snapshot APIs until this phase.

**Cost.** Levels 0-1 are free in any meaningful sense — a title read
and an idle query are microseconds, and the poll already runs. Level 2
is the only real expense, and it is bounded by being change-triggered
rather than periodic. Note it is nearly free *specifically in this
setup*: with chat on a remote provider, local worker inference grades as
`none` on the contention scale, so interpretation competes with the
other workers rather than with her ability to answer him.

**Key files (remaining).**
[`app/core/activity/`](../../app/core/activity/) (store + handlers),
[`app/core/proactive/idle_worker.py`](../../app/core/proactive/idle_worker.py)
(worker protocol, lanes),
[`app/core/proactive/cue_accounting.py`](../../app/core/proactive/cue_accounting.py)
(`CueSpec`, `CuePolicy`, `GAP_CUE_ORDER`),
[`app/core/vision/image_describe.py`](../../app/core/vision/image_describe.py)
(local-model + GPU-gate precedent),
[`docs/presence-and-activity.md`](../../docs/presence-and-activity.md)
(the privacy doc this must extend, not bypass).

**Open questions.**
- Does the interpretation layer write **memories** (durable, RAG-visible,
  and therefore a retention and redaction problem) or only **cues**
  (ephemeral, expire unsaid)? Cheapest honest answer is cues first,
  memories only once a pattern repeats — which is also how concepts
  are supposed to form.
- Where does the "he's been at it too long" judgement live? Extracted
  to [C9](#c9-activity-duration-as-wellbeing-evidence-k72) — evidence
  *into* K72, not a new companion cue.
- Should OS idle replace message-gap timing in the existing gap cues,
  or sit beside it? Extracted to [C8](#c8-os-idle-as-a-gap-cue-qualifier).
- Is the second-monitor posture (immersion H27) a prerequisite or a
  consumer? It reads as a consumer, but H27 is itself blocked on H10.
- Thread or sidecar process for the deep-inspection worker? A thread is
  far simpler and `IUIAutomation2` timeouts probably make it sufficient,
  but a wedged COM call on a thread can only be leaked, so repeated
  wedges accumulate. Start with the thread, and treat "we leaked two"
  as the trigger to move rather than deciding up front.
- Is there a case for one narrow UIA probe earlier than phase 6 — a
  terminal's last lines, so she can tell a passing test run from a
  failing one? It is the single highest-value structured read and also
  the highest-risk content. Probably worth a spike once the privacy
  layering from phase 1 has actually been used in anger.

**Cross-refs.** Subsumes **C2** (window titles) as its phase 1.
Immersion **H27** co-presence mode is the posture this is most useful in
and the natural home for a glanceable readout; it depends on immersion
**H10** (avatar idle-life). **K16**'s unified grounding line is where a
level-1 summary would land in the prompt without adding a block. **D7**
(anticipatory routine assistance) is the same idea sourced from K3
learned routines rather than from the OS, and the two should share an
intake. Immersion **H25** is the local-perception precedent. Audit
**H33** shape 14 is why phase 2 ships with retention.

---

## C7. Live activity pull / `get_activity` tool

**Motivation.** Push samples are change-detected and can be seconds
stale. A turn like "what are you looking at?" wants a forced sample on
the same redact path, not a second pipeline.

**Depends on.** C6 phases 1–2 (shipped). Do not start this until the
push store has been used in anger — the collection envelope, handler
registry, and `ActivityStore` are the reuse.

**Sketched approach.** Add `ActivitySource.snapshot()`, optional
`request_id` on the envelope, a WS `activity_request`, a bounded wait
(~150–300 ms) on the **same** ingest + redact path, and a `_TOOL_FAMILY`
tool. Timeout or no desktop → last stored session, not an error. UIA
pull would be `snapshot()` on the dedicated thread, never COM on the
turn thread. Do not stub those APIs ahead of this item.

**Key files.** [`web/src-tauri/src/activity/`](../../web/src-tauri/src/activity/),
[`app/core/activity/`](../../app/core/activity/),
[`app/core/session/tool_pass_gate.py`](../../app/core/session/tool_pass_gate.py)
`_TOOL_FAMILY`.

**Open questions.** Does the tool return the last session summary, the
raw last envelope, or both? Probably last session plus "as of Ns ago"
so she can hedge when the collector is stale.

---

## C8. OS idle as a gap-cue qualifier

**Motivation.** Idle depth currently conflates *away from the keyboard*
with *here but not talking to me* — both are just "no messages for N
minutes". `GetLastInputInfo` (now stored as `source: idle` events)
separates them. `sleep_return` fires on a 5 h message gap and cannot
tell a night's sleep from a long afternoon in another window; it is the
highest-priority gap cue after `turning_over`.

**Depends on.** C6 phases 1–2 (the idle source and store). Do not
replace message-gap timing in five shipped cues at once.

**Sketched approach.** Sit OS idle **beside** message-gap, as a
qualifier the existing gap cues can read: "no messages AND no OS input
for N hours" vs "no messages but the keyboard is busy". Start with
`sleep_return` only.

**Key files.** [`app/core/activity/store.py`](../../app/core/activity/store.py),
gap-cue providers, `GAP_CUE_ORDER`.

**Open questions.** Is a locked session (`source: lock`) a stronger
sleep signal than idle, or the same one?

---

## C9. Activity duration as wellbeing evidence (K72)

**Motivation.** K72's late-nights detector infers "he was up at 3am"
from *chat message* timestamps. The activity store now has OS sessions
with real start/end, so "he was in the editor from 01:00–04:00 without
talking to me" is a first-class fact the detector cannot see. C6's
open question was whether that judgement is a new companion cue or
evidence into existing wellbeing machinery. It is the latter: a new
cue in the oversubscribed pool would starve (C6 point 2). K72 already
has the delivery posture — one soft check-in, never a lecture.

**Depends on.** C6 phases 1–2 (the session table). Phase 3 rollups
would make the signal cheaper but are not required — session rows
already carry duration. Do not invent a parallel cue.

**Sketched approach.** Add an OS-session late-night / long-focus
detector beside `detect_late_nights` in
[`wellbeing_concern.py`](../../app/core/relationship/wellbeing_concern.py).
Feed [`ActivityStore.recent_sessions`](../../app/core/activity/store.py).
Same signature and cooldown gates the worker already uses. Distinct
from [C8](#c8-os-idle-as-a-gap-cue-qualifier) (C8 qualifies
`sleep_return`; this feeds K72). Distinct from
[D7](tools.md#d7-anticipatory-routine-assistance--act-on-what-shes-learned)
(D7 offers help at a learned slot; this is concern, not a task).

**Key files.** [`wellbeing_concern.py`](../../app/core/relationship/wellbeing_concern.py),
[`wellbeing_concern_worker.py`](../../app/core/proactive/wellbeing_concern_worker.py),
[`app/core/activity/store.py`](../../app/core/activity/store.py).

**Open questions.** Is a four-hour coding session itself a concern, or
only when it overlaps K72's small-hours window? Probably the latter
first — "up late in the editor" is the finding K72 already names, just
from a better sensor. Daytime long-focus stays a phase-5 companion cue
if it ever earns one.

