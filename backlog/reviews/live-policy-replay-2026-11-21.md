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
