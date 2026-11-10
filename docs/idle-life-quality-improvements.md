# Away life: continuity, choice and saved work

Implemented 2026-11-06. Restart required; ordinary-use behavior is pending.

## What the audit found

Read-only live diagnostics showed an awake, quiet session at overnight idle depth,
with the full 60-second LLM budget. Away activity, hobby, sleep lifecycle, room
evolution and plant growth had run. This was not a general scheduler starvation
problem. The garden worker had no runs in that snapshot, while Aiko was staying
in an authored scene without its own garden.

The eight-entry activity ring contained repeated desk tidying, drawing ideas,
wandering about the user, a snack and an imagined outing. The current hobby was
an old basil-vein research idea; a label alone did not establish useful progress.

Source tracing found several concrete limits:

- Startup registered idle workers, then detector initialization overwrote eleven
  worker handles with `None`. Scheduler ownership survived; diagnostics and
  providers using those handles could not see their workers.
- Free activity choice requested arbitrary verbs but admitted only three item
  effects or generic thinking/rest. Unsupported actions were correctly rejected,
  but there was no supported way to make an original piece of writing.
- Wandering explicitly meant thinking about the user. Tidying was offered for a
  clean desk, and a hobby intention could be satisfied by tidying that desk.
- A single reading step could satisfy an intention to finish the whole book.
- Open beats lost their completed-step receipts, ignored journal-write failure
  and later called an interruption a resumption without executing further work.
- A slow model choice could apply after a scene or item changed.
- Garden visits were scoped to the current scene. Relaxation claimed tea or
  reading without checking supplies; return poses could project sleep or reading.
- Room maintenance independently consumed tea and advanced books, competing with
  the explicit activity path.
- Plant growth already covered all scenes, but mutated live mirror rows before
  saving. A failed save could leave only the mirror advanced.

## Implemented behavior

### Work she can actually make and revisit

The existing model-choice path can choose an original poem, short fictional
scene, character sketch or wordplay. It must return the actual text. It can
choose its own subject, start a work, revise it, retitle it or leave it alone.
It can also stretch, think or rest; there is no requirement to produce something.

`idle_creations.py` validates the output and saves it as a world item. Every
successful revision retains the earlier text, title, form, timestamp and text
hash. Updates use an expected timestamp so an intervening edit cannot be
overwritten. Duplicate text, invalid output and failed saves earn no creation.
The bounds are 40–1,400 characters, eight revisions per work and three saved
writing steps per local day. Earlier works are retained.

The journal records a canonical title/revision receipt, not the model's claim
about what happened. Fiction stays in the work, outside autobiographical
memory. A pursuit note can record the act of saving a revision with its receipt.
`inspect_item` opens the latest or an exact revision with bounded text paging;
the World tab has a reader and revision selector. Ambient item lists omit the
full revision archive. Inspection marks text whose saved hash no longer matches.
Writing does not add a subject-specific offer to the legacy idle-seed ring;
future proactive offers must use the cue pool and its consumption tracking.

This is a completed-text creation slice of IM4. It is not a general artifact
job system, gift delivery, a drawing engine or unrestricted world authoring.

### A life with continuity and room to wander

Wandering is independent of the user. Tidying requires actual displaced items.
Drawing ideas remain ideas, rather than a claim that a drawing exists. Awake
rest uses an awake world activity; SleepStore/SleepLifecycleWorker still own
actual sleep. Their scheduler admission policies and compute budgets are unchanged.

Open beats retain completed-step receipts. Their elapsed window does not earn
additional chapters, research or creative revisions. An interruption records a
pause, not an invented return to work. Failed journal writes keep a recoverable
beat rather than replaying the world action. Scene, item and mutation-guard
checks are repeated after model choice. Movable reading material follows the
chosen spot; watering and tea use their item's actual location.

Hobby intentions require a newly saved research finding. Finishing a book
requires the actual final supplied passage. Completion phrasing avoids bare
relative dates. Room maintenance refills an empty pot, replenishes low cookies
and consolidates food; reading and drinking belong to explicit activities.

### Access to her own garden

After at least two hours of quiet absence, the garden worker can visit the known
home garden while Aiko is staying in another scene. It still respects daylight,
cooldown, sleep, deliberate placement and active shared conversation. An open
away activity takes precedence. It creates no scenes and visits no arbitrary
authored destination. After the visit she settles in her home scene; a later
deliberate placement cancels automatic return as before.

Outdoor relaxation no longer invents refreshments, books or weather findings.
Plant promotion works on a detached proposal, so failed persistence leaves the
live mirror unchanged. Passive growth remains independent of Aiko's location
and may continue during sleep.

## Verification and follow-up

Focused regressions cover saved revisions/reopen, stale edits, output rejection,
duplicate/quota limits, tool paging, journal recovery, receipt preservation,
earned intentions, worker identity, cross-scene garden visits and sleep/placement
vetoes. The related frontend tests cover reader rendering and version choices.
The affected-test selector expands through shared session initialization; a
representative world/idle/sleep/prompt/tool set is used instead of the full suite.

Results: 943 affected tests and 319 subtests passed; the explicit linked/final
integration set passed 452 tests and 24 subtests. Focused runs after the
writing/persistence adjustments passed 115 tests, then 26 writing/seed tests
verified the cue-accounting boundary. These sets overlap.
Related frontend checks passed 13 tests, and `npm run lint` passed. All Python
checks used the managed runner and disposable databases. No live model generation
or ordinary-use naturalness is claimed from these results.

After restart and several normal away periods, check:

1. Worker handles agree with scheduler registration and day-intention diagnostics.
2. Garden visits occur from an authored room without interrupting shared activity.
3. Writing remains optional and varied; a claimed revision opens the exact saved text.
4. Pauses remain pauses, book completion is earned, and awake rest never implies sleep.
5. Sleep suppresses waking activities while permitted passive/synthesis work continues.

The 2026-11-09 hobby follow-up adds a seven-day stall exit without awarding timer
progress, omits stale standing updates, and rejects close rewrites of recent
project subjects. A late saved finding is applied before stall retirement. See
`idle-workers.md` for the behavior and limits; ordinary-use pacing still needs
observation after restart.
General draft/ready/failed artifacts and intentional gifts remain in IM4/H12.
