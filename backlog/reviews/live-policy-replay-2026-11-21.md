# Local Live policy replay, 21 November 2026

Ran four fresh trials of each fixture against the configured loopback Ollama
`qwen3.5:4b` Live policy. This is a controlled baseline over synthetic
exchanges. A policy call could receive zero, one or several eligible urges.
The calls and opportunities below are counts from these fixtures, so they
cannot establish an ordinary-use hourly rate.

| Scenario | Policy calls | Decisions with eligible urges | Autonomous full / micro speech requests | What the policy saw |
| --- | ---: | ---: | ---: | --- |
| [Two topics](../../tests/scenarios/live/topic-choice.json) | 12 | 4 | 0 / 0 | Balcony herbs and train puzzle, both eligible at the opening |
| [Quiet and cadence](../../tests/scenarios/live/quiet-cadence.json) | 20 | 12 | 0 / 0 | Sketchbook, garden sketch and window plants |
| [Voice interruptions](../../tests/scenarios/live/interruption.json) | 36 | 12 | 0 / 0 | Bookshelf and oolong tea across two openings |
| [Requested update](../../tests/scenarios/live/requested-update.json) | 8 | 4 | 0 / 0 | Bread ready to pack and the next chapter, both eligible |
| [Earlier observation fixture](../../tests/scenarios/live/local-observation.json) | 12 | 4 | 0 / 0 | Balcony herbs after the user stopped typing |
| **Total** | **88** | **36** | **0 / 0** | **20 completed trials** |

The policy proposed `wait` in 32 of the 36 decisions with eligible urges.
In the other four it proposed `remain_present` with an unknown urge reference.
The requested update is the sharpest miss. The user explicitly asked to be
interrupted when the bread was ready, and waiting won all four eligible calls.
There were no local policy transport failures. The quiet fixture also produced
zero policy calls in its eight no-cue heartbeat samples.

## Interruption

- At voice onset, the local policy proposed yielding six times and acknowledging
  twice. Five of those eight proposals passed the arbiter. During simulated
  playback specifically it proposed yielding in all four trials, with three
  accepted.
- On the twelve final voice messages it proposed six yields, five acknowledgments
  and one noop. The direct-user-intent admission fired all twelve times.
- The model queued no wake before either interruption check. The matched
  scripted path queued a bookshelf question, which dispatch blocked as
  `floor_busy`, and an oolong question, which dispatch blocked as
  `new_user_intent`. Both blocked topics stayed out of simulated speech.

## Follow-up

Fifteen model decisions were rejected with `unknown_urge`; fourteen returned
a numeric reference such as `1` or `2`. Test a menu format that makes the
copyable reference easier to distinguish from display order. Keep the arbiter's
exact-reference check and rerun the same fixtures to see whether selection
improves.

The requested update reached the model as a candidate subject and a share
purpose, and the user's invitation was in the recent transcript. The cue's
full text belongs to the speech handoff. For the next concept experiment,
replay matched urgent and optional
updates with and without a grounded user preference. Measure a useful offer
when the floor is open, restraint during interruption, and whether a topic
returns only after fresh evidence or a new opening. Separate a one-turn
preference from repeated evidence before testing persistent learning. A
reliable “requested update ready” impulse could be tested with the same pair
of scenarios once the model's candidate reference is stable.

```powershell
python scripts/live_scenarios.py tests/scenarios/live/requested-update.json --local --repeat 4 --json
python scripts/live_scenarios.py tests/scenarios/live/interruption.json --local --repeat 4 --json
python scripts/live_scenarios.py tests/scenarios/live/interruption.json --repeat 2
```

## Cue reference retest

Each menu row now exposes one request-local reference. The model's JSON
schema lists the displayed references and null. The controller resolves the
selected reference to the internal urge ID before arbitration.

Replayed the requested update four times per condition with the same local
`qwen3.5:4b` route, timeline, transcript and cues. The seeded condition
started with the synthetic preference “Share time-sensitive updates the user
requested when the floor opens.” The trace confirmed the preference was
visible to the policy on all four openings.

| Condition | Openings with both topics eligible | Policy choice at the opening | Full / micro speech requests |
| --- | ---: | --- | ---: |
| Without preference | 4 | `remain_present` with ref `"3"` (ambient presence) in all four | 0 / 0 |
| With preference | 4 | `wait` with no reference in all four | 0 / 0 |

The policy chose neither the bread update nor the next chapter. The eight
trials produced 16 policy calls, with zero transport failures or
`unknown_urge` rejections. Four additional interruption runs produced 36
policy calls with zero `unknown_urge` rejections. There were no autonomous
speech requests during those runs. A deterministic regression check also
sent ref `"2"` and confirmed that the bread update reached the speech
handoff. An out-of-menu reference was rejected.

A preference alone did not get the requested update spoken in this replay.
The next comparison can give a user-requested update an explicit readiness
signal and measure whether it surfaces on the first safe opening while the
casual cue stays queued for later. This retest seeded an established
preference. It did not exercise concept acquisition.

[Commands and preference fixture](../../docs/live-scenarios.md#run-a-scenario)

## First opening with an explicit readiness signal

The [readiness scenario](../../tests/scenarios/live/requested-update-ready.json)
publishes the bread cue at 11 seconds while the user is typing. The frame
still reports active typing at 14 seconds, then the user stops. The first
policy opportunity is the heartbeat at 15 seconds. Both topics are eligible
then. Controls replay the same timeline with the `requested_update_ready`
field removed from the bread cue.

| Readiness | Seeded preference | Local policy at the first opening | Bread updates handed off |
| --- | --- | --- | ---: |
| Present | Absent | Requested speech about the bread in 4 of 4 trials | 4/4 |
| Present | Present | Requested speech about the bread in 4 of 4 trials | 4/4 |
| Absent | Absent | Acknowledged the user in 4 of 4 trials | 0/4 |
| Absent | Present | Acknowledged the user in 4 of 4 trials | 0/4 |

The loopback `qwen3.5:4b` model made 32 policy calls without a transport
failure. All eight marked updates passed admission and reached simulated
delivery at the first opening. Each delivered bread cue was marked used.
No trial requested speech while the user was typing, surfaced the next
chapter, or repeated the bread update on the next heartbeat.

The cue marker and the prompt's explicit speech-action example were tested
together. The preference was seeded before the first impulse. The scenario
supplied readiness and substituted a scripted line for the main-model
response. This measured first-opening decisions and handoff.

## Learning coding-time preferences across sessions

The [two-session replay](../../tests/scenarios/live/learn-coding-timing.json)
teaches the coding-time preference in the first transcript. The following
virtual day starts a new transcript and observes a fresh coding session. Each
trial has a fresh in-memory database. The control receives the same user
message and activity timeline with preference recording disabled.

| Outcome on local `qwen3.5:4b` | Learned timing | Control |
| --- | ---: | ---: |
| Policy evaluated the optional sketchbook cue during a coding lull | 0/4 | 4/4 |
| Requested render update handed off at the first safe opening | 4/4 | 4/4 |
| Sketchbook cue eligible at the later opening outside coding | 4/4 | 0/4 |
| Model requested speech about the sketchbook outside coding | 0/4 | 0/4 |

All 64 policy calls completed. The model stayed quiet during the later
opening even with fresh activity evidence. In the control, the sketchbook
urge had already used both evaluations during coding. With learning enabled,
it reached the later opening with its evaluation budget intact. The scripted
test selected it there and verified delivery.

Both conditions used the existing readiness signal for the render. The
harness supplied the activity samples and simulated the speech handoff. This
pilot tests a narrow explicit instruction and its carryover.

## Visual floor manners

Four isolated [listening posture replays](../../tests/scenarios/live/listening-posture.json)
on `qwen3.5:4b` produced 24 policy decisions and zero autonomous speech
requests. All four runs showed the same visual transitions. Typing gave
space without a policy call, active speech leaned in, an utterance ending
settled, and speech resumed before the earlier endpoint could expire.
An interruption held still during simulated playback and returned to
active listening after playback drained. Quiet mode held still, and sleep
retained its own behavior.

Frontend channel tests exercise the gaze and lean used by the avatar. The
replay measures semantic plans with synthetic voice events.

## Return glance and avatar channel states

The 64 focused frontend tests passed on the original channels and again
after the state refactor. They cover gaze priority, saccade timing, and
blending the listening lean with the tired slump and valence tilt. The
broader run added the return-glance tests and passed all 266 tests.

Four isolated [return-glance replays](../../tests/scenarios/live/return-glance.json)
on `qwen3.5:4b` confirmed two return pulses per trial. The first lasted
900 ms at most. Another foreground sample left its original deadline
intact. The second was canceled by typing and stayed canceled afterward.
Switching conversations and quiet mode suppressed additional pulses.
There were zero autonomous speech requests in 40 policy calls.

## Circadian quieting

Four isolated [circadian replays](../../tests/scenarios/live/circadian-quieting.json)
on `qwen3.5:4b` crossed 22:00 local time. The transition alone produced
zero policy calls. The awake avatar's semantic breath changed from
`normal` to `slow` at night and `quiet` at late night. Early morning
restored the normal breath. Quiet mode and sleep retained their usual
speech restrictions.

The render update was requested and handed off at the first safe opening
in all four night trials and all four matched afternoon controls. Both
conditions had zero speech requests while the user was typing. The
scenario provided the ready result and simulated the spoken handoff.
The avatar check measured the semantic plan.
