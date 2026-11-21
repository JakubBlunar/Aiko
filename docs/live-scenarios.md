# Live mode scenarios

The runner opens a fresh in-memory database for each scenario and drives the
same Live impulse, situation, cue, wait, budget, policy and dispatch code used
by the app. It advances a virtual clock and captures speech in the report.
The running app, its database, cloud chat, microphone and speakers are outside
the run.

## Run a scenario

Run these commands from the repository root with the project Python environment.

```powershell
python scripts/live_scenarios.py tests/scenarios/live/cue-and-floor.json
python scripts/live_scenarios.py tests/scenarios/live/wait-and-focus.json
python scripts/live_scenarios.py tests/scenarios/live/local-observation.json --local --json
python scripts/live_scenarios.py tests/scenarios/live/interruption.json --local --repeat 4 --json
```

To compare the same timeline with and without an established preference, use
`--concept-file` on one set of runs. The file holds one concept with the same
fields as a `concept` step. It is visible before the first impulse and
starts fresh in each trial.

```powershell
python scripts/live_scenarios.py tests/scenarios/live/requested-update.json --local --repeat 4 --json
python scripts/live_scenarios.py tests/scenarios/live/requested-update.json --local --repeat 4 --json --concept-file tests/scenarios/live/preferences/requested-update.json
```

Scripted mode is the default. It uses each step's `proposal` to reproduce a
decision without a model. Steps without one use `noop` if policy runs.
`--local` ignores scripted proposals and uses the configured `live_policy`
Ollama route, model and token limits. It checks that the model is installed
locally and rejects remote model entries and HTTP redirects. Both modes capture
main-speech requests without invoking a chat model for the spoken reply.

The text report shows virtual time, policy proposals and decisions, queued
topics, dispatch outcomes and script-delivered speech. The JSON report also
includes the wait, remaining budgets, cue state, policy concepts, outcome
ledger, embodiment plan and the actual talk-about block that reaches a
simulated main turn.

With `--repeat`, each trial starts with fresh state and the JSON output has a
`summary` and individual `runs`. The summary distinguishes policy calls,
speech proposals, admitted wakes, dispatch outcomes, and delivered micro
utterances. Autonomous proposal counts exclude submitted user turns.
`no_queued_wake` means a dispatch check had no pending request to exercise.
The interruption summary counts voice-start and transcript-final reactions
separately and records which interrupts began with an old wake in the queue.
It also counts decisions with at least one eligible urge and records rejection
reasons, so a wait at an open opportunity can be distinguished from a rejected
action.
Failed expectations or policy transport failures give a nonzero exit code.
Keep model-based expectations focused on invariants such as preserving the
user's floor. Choices and wording can vary across local runs.

## Author a scenario

Each JSON file has a `name` and a `steps` list. Optional `start_at` is an ISO
timestamp with a timezone. Use `max_wakes_per_hour` and `min_gap_after_speech_ms`
to compare cadence settings. Each step may include a `label`, a `state` update
and one command.

| Command | Scenario input |
| --- | --- |
| `impulse` | A Live event such as `user.typing_started`, `activity.session_changed` or `silence.wake`. Add `payload.text` for `user.message_sent` or `user.speech_final`. |
| `advance_ms` | Advance both clocks and run one heartbeat at the destination. Use repeated steps to sample intermediate behavior. A wait expiry can produce a real `idle.reconsider` impulse. |
| `cue` | Add a pool cue with `type`, `subject` and `text`. Optional `ttl_hours` and `hold_hours` exercise timing. |
| `concept` | Seed a learned policy concept with `id`, `kind` and `label`. Optional `subject` and `confidence` let the same activity run against different learned context. |
| `dispatch` | Deliver the oldest queued main wake through the real dispatch checks. Supply `{"text": "a scripted reply"}` or omit the text to simulate silence. Use `"if_queued": true` when a model might choose to stay silent before this step. |
| `reply` | Supply scripted assistant text after a submitted user message. It becomes part of the isolated transcript and updates the speech gap. |

`state` can change `app`, `typing`, `floor`, `present`, `connected`,
`playback`, `sleep`, `quiet`, `shared`, `activity`, `voice_session` or
`unprompted_speech`. Set `voice_session` to true when replaying microphone
events so `user.voice_start` can put the user on the floor. This is a
simulated capture state.
A step with just a state update refreshes the situation on a data-only heartbeat.
For a scripted request to discuss a specific seeded topic, use
`"proposal": {"intent": "request_main_speech", "select_subject": "balcony herbs"}`.
The subject resolves to an urge only if it is in the real policy menu.
The policy uses a reference such as `"2"` from that request's menu. The
decision trace shows the reference and its resolved internal urge ID.
Scenarios can also include an `initial_concepts` list to seed concepts before
the timeline starts.

An `expect` object matches a subset of that step's JSON. Arrays match in order
and length. For example, `{"new_policy_calls": 0, "speech_count": 0}` checks that
typing alone neither calls the policy nor emits speech. The existing fixtures
show expectations for queued topics, cue settlement, wait expiry and policy
modifiers when a learned ritual conflicts with the user's current activity.

Each scenario carries outcomes and cue feedback into its later steps. Concept
fixtures let you test how established learning changes behavior. The harness
does not run background learning workers, generate real main-chat prose or
judge delivery through real audio.
