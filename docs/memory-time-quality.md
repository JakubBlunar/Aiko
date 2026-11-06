# Memory data quality and time

## Planning windows

Elapsed plans keep `future_plan`: this denotes the source's intention, not a
prediction that the planned date is still ahead. Maintenance records a bounded
`plan_retirement` receipt with outcome unknown, preserves the original intended
date and grants the existing seven-day retrospective relevance window. Repeated
sweeps do not renew it. Historical plans remain available in archive but cannot
stock new forward questions or pass a queued forward-cue claim. Retrieval says
outcome unknown. Actual action/completion reports remain separate source-cited
records; elapsed time never supplies one. Promise status retains its own lifecycle.

Focused and representative affected checks cover expiry, clockless grace,
idempotence, rescheduling, retrieval, queued cues and concept synthesis. No live
memory was changed during implementation. Historical rows already converted to
past events need source review; their original modality cannot safely be guessed
from their current date/type alone.

## Promise extraction

An exact same-speaker commitment quote still establishes source identity, not
an invented deadline. A separate literal `deadline_quote` is resolved by the
server against that message's timestamp. Unstated, vague and unparseable times
stay unknown; date-only wording cannot acquire an exact clock. The quoted
timing and its precision remain in metadata. Immediate enactment and casual
reassurance are excluded by the extraction contract.

A persisted source receipt and action comparison prevent an unchanged message
from becoming another commitment through paraphrasing, deadline changes or
terminal-status changes. A successful unchanged transcript window is skipped
before model generation. Failed writes/calls retain retry opportunity. New
messages and edited source windows are reconsidered, and explicit different
commitments or genuinely stated new deadlines remain possible. This changes
future writes, without deleting historical duplicates.
