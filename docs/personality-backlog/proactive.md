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

**Now phase 1 of [C6](#c6-companion-mode--the-desktop-as-a-sensory-channel).**
Read C6 first: titles are worth much more as the input to a perception
pipeline than as one more string in the ambient block, and C6 supplies
the privacy story this entry says it is waiting for.

**Motivation.** App name only ships in v1 of activity awareness; window
titles would let Aiko reference doc / file names she sees in Jacob's
foreground app, but leaks bank URLs and private chat targets if naively
forwarded. Worth picking up once we have a privacy story strong enough
to support it.

**Key files.** [`web/src-tauri/src/lib.rs`](../../web/src-tauri/src/lib.rs)
`get_active_app`, [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
`set_user_active_app` + `_render_activity_block`,
[`web/src/hooks/useActivityReporter.ts`](../../web/src/hooks/useActivityReporter.ts).

**Sketched approach.** Per-app allowlist (`activity.title_allowlist:
{"Cursor": true, "Code": true}`) gated on a settings toggle that's
*also* OFF by default. Forwarded titles get the same privacy footer
treatment as the live readout — visible to the user before they
opt in. Persona update tells Aiko she may reference the title casually
but never quote URLs or chat-target names verbatim.

**Open questions.** Allowlist by app name, or also by app + title-
regex pair so we can let "Cursor" through while still redacting an
incognito tab in the same browser?

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
behaves. The real work is not the pipeline. It is the storage layer
underneath it, which does not exist at all, and the interruption bar on
top of it, which this codebase has repeatedly got wrong.

### What already exists

- **Level 0, partially.** C1 ships presence plus the foreground **app
  name**, opt-in and off by default, 5 s poll, diff-only over the
  existing WebSocket. Two consumers: the T4 `activity_block` and K16's
  `GroundingContext.user_app`. `active_win_pos_rs` already returns
  `.title` in the same struct — we deliberately never read it, which is
  the load-bearing privacy decision in
  [`presence-and-activity.md`](../../docs/presence-and-activity.md).
  So phase 1 is a one-field change plus a privacy story, not new
  plumbing.
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
caller — `chat_turn_mixin.py:117`, on a chat turn. `user_activity`
frames do *not* reset the idle gate, so a user who is coding and not
chatting reads as **idle** to the scheduler, which is precisely when
perception should run. Its docstring claims it is also called from "WS /
REST traffic", which is stale. If anyone ever makes that docstring true,
this entire pipeline silently stops running. It wants an invariant test,
not a comment.

**And one ambiguity this fixes.** Idle depth conflates *away from the
keyboard* with *here but not talking to me* — both are just "no messages
for N minutes". `GetLastInputInfo` separates them for the first time,
which is worth something beyond companion mode: `sleep_return` fires on
a 5 h message gap and currently cannot tell a night's sleep from a long
afternoon in another window, and it is the highest-priority gap cue
after `turning_over`.

### The three things the sketch does not account for

**1. There is no history to interpret.** Today's signal is a single
`str | None` on the session object: no timeline, no durations, no
counts, not persisted, gone on restart. Every example the design turns
on — "VS Code 7 min, debugger opened 4 times", "he has been at this for
two hours" — needs a store that does not exist. That is the real
foundation and it is phase 2, not a detail. It also arrives with a
warning attached: per audit **H33**'s recurring shape 14, an append-only
event table with no retention policy is the exact failure we just spent a
day paying off in LanceDB. **Retention ships with the table, in the same
commit, or it never ships.**

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
sentence on his screen. That is a different category of data, not more
of the same one, and the current defence-in-depth doc is built around
the narrow reading. The sharper problem is downstream: today's app name
is transient, overwritten every 5 s and never stored, whereas anything
the interpretation worker concludes becomes a **memory** — mirrored into
LanceDB, retrievable by RAG, surfaceable months later. **Redaction has
to happen before persistence, not before rendering**, and the allowlist
has to be positive (named apps opt *in*), because the set of
applications whose titles are safe is small and enumerable while the set
that are unsafe is not.

### On UI Automation specifically

Worth a reality check, because it is the most exciting part of the
sketch and the least likely to pay. UIA is COM: apartment threading,
never on a UI thread, and every property read is a cross-process call,
so a naive tree walk of a browser or editor runs into hundreds of
milliseconds and must be batched through `CacheRequest` /
`FindAllBuildCache`. Coverage is uneven in a way that anticorrelates
with what we want: Electron apps (Cursor, VS Code, Discord, Slack)
expose a tree only once their accessibility engine is switched on, which
*measurably slows the observed application* — the user notices his
editor getting sluggish and blames the companion — and games expose
nothing at all.

Against that, note what a window title alone already gives you. VS Code
puts `rag_store.py — assistant` in its title bar; that is the sketch's
"he's looking at concept-worker.ts", for a `GetWindowTextW` call. Most
of the worked examples are reachable from level 0. **Defer UIA behind
phases 1-5 and re-evaluate with real data on what titles left
unanswered** — the honest expectation is "not much".

### Phases

1. **Level 0 collectors (Rust).** Window title behind a positive
   allowlist, `GetLastInputInfo` for true OS idle, session lock/unlock.
   Small; `active-win-pos-rs` already pulls the `windows` crate in.
2. **Event store + sessionizer (Python, schema v36).** Normalised
   events, focus-flicker collapse into sessions, durations and counts,
   retention from day one. The foundation; everything else is cheap
   afterwards.
3. **Level 1 aggregation worker.** Compute lane, no model — rollups and
   a "has anything meaningful changed" signal, which is also the
   `demand()` probe for phase 4.
4. **Level 2 interpretation worker.** LLM lane, local worker model,
   triggered only by phase 3's change signal. Must be able to return
   *nothing* and must carry a confidence, because a confident wrong
   reading ("you've been gaming for three hours" — he was watching a
   tutorial) is worse than silence by a wide margin.
5. **Level 3 intake.** One memory path plus one gap cue, both
   instrumented. Stop here and measure for a fortnight.
6. **UIA.** Only if phase 5's data says titles were not enough.

**Cost.** Levels 0-1 are free in any meaningful sense — a title read
and an idle query are microseconds, and the poll already runs. Level 2
is the only real expense, and it is bounded by being change-triggered
rather than periodic. Note it is nearly free *specifically in this
setup*: with chat on a remote provider, local worker inference grades as
`none` on the contention scale, so interpretation competes with the
other workers rather than with her ability to answer him.

**Key files.**
[`web/src-tauri/src/lib.rs`](../../web/src-tauri/src/lib.rs)
`get_active_app` (level 0),
[`web/src/hooks/useActivityReporter.ts`](../../web/src/hooks/useActivityReporter.ts),
`user_activity` handler in [`app/web/server.py`](../../app/web/server.py),
[`app/core/session/proactive_presence_mixin.py`](../../app/core/session/proactive_presence_mixin.py)
`set_user_active_app`,
[`app/core/session/inner_life_part4.py`](../../app/core/session/inner_life_part4.py)
`_render_activity_block` + `_build_grounding_context`,
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
(v36 table + retention),
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
- Where does the "he's been at it too long" judgement live? Wellbeing
  concern is relationship-shaped and there is existing machinery for
  it; this may be evidence *into* that rather than a cue of its own.
- Should OS idle replace message-gap timing in the existing gap cues,
  or sit beside it? Replacing is more correct and touches five shipped
  cues at once, so probably beside it, with the gap cues reading it as
  a qualifier.
- Is the second-monitor posture (immersion H27) a prerequisite or a
  consumer? It reads as a consumer, but H27 is itself blocked on H10.

**Cross-refs.** Subsumes **C2** (window titles) as its phase 1.
Immersion **H27** co-presence mode is the posture this is most useful in
and the natural home for a glanceable readout; it depends on immersion
**H10** (avatar idle-life). **K16**'s unified grounding line is where a
level-1 summary would land in the prompt without adding a block. **D7**
(anticipatory routine assistance) is the same idea sourced from K3
learned routines rather than from the OS, and the two should share an
intake. Immersion **H25** is the local-perception precedent. Audit
**H33** shape 14 is why phase 2 ships with retention.
