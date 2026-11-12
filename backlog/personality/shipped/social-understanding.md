# Social understanding and specific repair: bounded slice, 24 Oct 2026

## K102 pragmatic frames

The existing [situation observer](../../../app/core/conversation/conversation_situation_worker.py)
can retain one optional pragmatic frame: participants/referents, local
literal/quoted/pretend scope, a tentative reading and an unresolved alternative.
Each note has 1-3 supplied source IDs, at most 160 characters, within the shared
eight-ID envelope. Notes use absolute stored-time language. This is conversational
context, not a user trait, memory admission, action permission or world authority.
There is no additional inference call or scheduling change; the observer keeps
its 1,536-token base output budget and existing shadow allowance.

The [existing situation row](../../../app/core/conversation/conversation_situation.py)
stores these optional fields in its JSON, with no database migration. Each
observation replaces them; omission or a clear drops them. Stale/ended state,
deleted or foreign-session sources and more than two subsequent user messages
prevent the social projection. Current user input always outranks prior readings.
Working-set sources have priority when retaining bounded observer history.
Malformed optional social fields are discarded independently, never at the cost
of an otherwise valid question or constraint set. Rejection codes are observable.

The frame is part of the existing T6 `conversation_situation_block`. It offers no
proactive stance. Conditional guidance lives in
[conditional_handling.txt](../../../data/persona/conditional_handling.txt) under
the registered `Reading context and repairing a misunderstanding:` header;
the always-on companion persona was not edited.

## K103 specific conversational repair

[The projection helper](../../../app/core/conversation/social_understanding.py)
uses the exact recent assistant span and current correction in the correction's
own reply. The cheap gate extends K17's explicit cues to plain referent/joke/audio
corrections, ignores quoted/code-only triggers, and makes no model call or
replacement guess. It quotes evidence and asks the main model to resolve the
affected point in the full conversation. The gate is deliberately incomplete;
ordinary model comprehension and the observer still handle other expressions.

The situation observer can also propose one unresolved target, later user
correction and replacement/remaining ambiguity. The correction must cite only
user messages later than the target; the replacement must cite the correction.
The observer omits the target once addressed, dropped or on a pivot; corrected
referents can remain in the frame while relevant. Neither mood improvement nor
a delivery receipt establishes understanding. The projection does not itself
resolve a target from positive affect.

With the situation path available, post-turn no longer arms a redundant K17 cue
on the next turn. Without that path, the existing K17 fallback remains. The
existing clarification setting gates immediate repair. Durable correction and
relationship-learning owners were not changed. J6's separate valence-based
relationship-recovery heuristic is not validated by this feature.

## T5 baseline and reproducible probes

[The runner](../../../scripts/evaluate_social_reasoning.py) uses the real
`PromptAssembler`, optionally the real observer, and fresh SQLite databases under
an owned `.tmp-tests/social-eval-*` directory. It closes and removes them on exit,
including generation failures. Config is parsed from read-only copies instead
of `load_settings()`, which can migrate live overrides. No live history, memories,
affect or relationship state enters the probes. Provider credentials and persona
content are excluded from reports. Model calls are opt-in; the persona still
forms part of the prompt sent to the selected provider.

[The frozen v1 corpus](../../../tests/fixtures/social_reasoning_t5.json) contains
12 synthetic seven-message episodes and 15 checkpoints. It covers ironic versus
sincere praise, banter with a boundary, speaker attribution, pretend exit, retained
server corrections including Slovak, joke repair/pivot, unheard versus misunderstood
speech, uncertain sarcasm and a quiet goodbye. The responses are continuations
of scripted prefixes, not free-running conversations. Lexical indicators,
apology/question counts, errors, first-token latency, token usage and observer
validation are recorded separately. Human semantic/style review is explicitly
pending; these indicators are not a naturalness score or independent ground truth.

Example (uses the configured provider and local persona; review that destination
before running):

```powershell
python scripts/evaluate_social_reasoning.py --label current --output .tmp-tests/social-current.json
python scripts/evaluate_social_reasoning.py --observer --label observed --output .tmp-tests/social-observed.json
```

`--prepare-only` saves local prompt inputs without calls. `--observer-only` probes
only worker schema/evidence behavior; `--observer-prompt-file` supports an explicit
frozen-prompt ablation. The call/time budgets stop new probes; a running call is
bounded by the provider timeout. Reports include corpus, persona, conditional
handling and code/prompt hashes plus model/route budgets. No credentials are
reported. Retained reports are caller-owned outputs, separate from cleaned scratch.

## Initial observations and limits

The pre-change configured-model baseline completed 15 probes using Venice
`deepseek-v4-1-flash-uncensored`, 256k context, a 1,024-token response ceiling,
temperature zero and the route's low reasoning effort. It already followed the
obvious irony, corrected server, quoted speaker and unheard-span examples. The
first candidate also met those narrow checks. A baseline lexical miss was a
harmless paraphrase, so its disappearance is not an improvement in reasoning.
A candidate joke correction overextended the teasing; the conditional guidance
was tightened, without changing the frozen checks or hiding that result.
The final main-model run completed all 15 probes with no forbidden-pattern hits
or repeated apologies. Its first-token median was 1.24 seconds versus 1.37 seconds
in the baseline; these single runs do not establish a latency improvement.

The local `baytout3/qwen3.5-uncensored:9b` observer exposed malformed optional notes
and existing working-set evidence/time rejections. That motivated independent
optional-field rejection rather than weakening citation checks or raising the
budget. A cold observer timeout and a later main-provider response with no visible
answer were recorded as failures. The observed continuation runs are not an equal-
call causal comparison with the main-model-only baseline.

The equal-call, local-only observer schema comparison accepted 11/15 proposals
with both the frozen pre-change prompt and the final prompt. Four proposals in
each run failed existing core evidence/time checks. The final parser separately
discarded five optional social fields while preserving the valid base proposals;
neither run hit the output ceiling. Only one usable frame and no unresolved repair
target were emitted in that final run. Most corrections in these scripted prefixes
are already addressed, so this is a schema smoke test, not a repair-recall metric.

There is no demonstrated general naturalness gain. Repeated/counterbalanced runs,
blinded human review, longer history displacement, material ambiguity and real
voice playback/interruption remain open. Audio fixtures test transcript behavior;
they do not establish how speech sounded. The running app's MCP endpoint was
unavailable during this task, so no live-app end-to-end claim is made. Existing
L55 evidence/decision evaluation remains separate and its regression guards are
retained.
