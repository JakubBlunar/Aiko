# Further worker quality improvements

Implementation of the [6 Nov worker audit](worker-quality-audit-2026-11-06.md).
The existing stores remain authoritative; historical records are not rewritten.

## Internal growth reflections

Mood and relationship-signal changes describe Aiko's recorded experience, with
explicit baseline and later-window dates. They do not establish user growth,
causes, or a change since first meeting. Aiko goals are not used as corroboration
of the user's work. Delivery uses the cue pool with a single-item inventory,
expiry, freshness checks and consumption accounting. The old journal is retained
as diagnostics; legacy ring entries cannot surface through this provider.

Focused growth/policy checks: 136 tests and 93 subtests passed. Live phrasing is
an ordinary-use check after restart, not established by these structural tests.

## Background diary and thread inputs

Transcript packing keeps a contiguous suffix of whole messages. A final negation
or imagined-scene qualification survives intact, or the message is omitted whole.
The background diary freezes the source messages, user name and session before
generation, records continuity inputs as subjective derivation, and rechecks its
sources after generation and embedding. A session switch cannot relabel the old
draft as a new session's experience. Thread resummary uses the same packing with
its existing history-edit save guard. Post-sleep diary output is accepted whole
or rejected, and retains the session captured before generation.

Regression coverage includes source edits at both generation boundaries, session
switches, long-message final negation and overlong sleep-diary output. The combined
focused worker set passed 242 tests and 6 subtests; lint passed.
