# Belief quality proposal — 5 November 2026

Proposal only. Live diagnostics, the local SQLite source of truth and owning
code were inspected read-only. No beliefs, runtime settings or application code
were changed. Private row snapshots are in the ignored
`tmp/belief-quality-review-2026-11-05/` directory.

## Findings

The store currently contains 413 beliefs: 271 mood and 142 opinion; 200 active,
81 confirmed, 127 contradicted and five stale. Seventeen rows cite the same
12-message source window, with observation stamps spanning approximately five
hours. Their latest variants include the incomplete subjects shown by the user
and many overlapping affectionate relationship readings. Shared source windows
are context receipts, not evidence that every row is independently supported.

The live outcome diagnostic contains 91 recorded outcomes: 76 contradictions
from mood comparison, one from the opinion heuristic, ten automatic
confirmations and four manual confirmations. These are method counts, not an
accuracy score and not a semantic adjudication of historical beliefs.

The main problems are structural:

1. **The representation requires fragments.** The worker prompt asks for a
   2–60 character topic and a short phrase completing a sentence, usually under
   eight words. The parser accepts at most 12 state words. The UI renders those
   two fields directly. There is no structured full interpretation, rationale,
   applicability, unresolved uncertainty or intended conversational use. Longer
   card styling cannot recover information never retained.
2. **Novelty is mostly a topic-string problem.** The store matches exact
   normalized topics or same-kind topic embeddings at cosine 0.88. It does not
   decide whether two full claims add a meaningful distinction. Pronouns,
   incomplete objects and alternative relationship labels make one recurring
   idea look new; short topic validation currently rejects dates but permits
   dangling phrases such as “caring for”. Mood and opinion variants are also
   separate candidate pools.
3. **Reprocessing can look like corroboration.** A heartbeat can re-mine an
   unchanged window; a new turn also leaves older messages in the lookback.
   `_resolve_reobservation` increments the observation count on each same-state
   upsert, without checking distinct supporting messages or episodes. Two such
   observations can confirm a belief. Slightly different state wording instead
   resets the count. The worker records a whole source window and its newest ID,
   not a validated supporting quote for each individual claim.
4. **The automatic mood comparator uses the wrong subject.** The post-turn
   caller passes `AffectStore.get(user_id)` to the belief detector. `AffectState`
   is explicitly Aiko's emotional state, driven primarily by her reactions with
   weaker user cues and emotional carryover. It is not an independent estimate
   of the user's topic-specific feeling. Topic overlap cannot repair that actor
   mismatch. The 76 comparison outcomes should therefore not be treated as
   reliable user-state verification.
5. **Freshness and authority are conflated.** Unrelated mood checks refresh
   `last_checked_at`; staleness uses that field and only ages active rows.
   Confirmed mood readings can remain standing context, and trusted-belief
   rendering explicitly says they can be assumed. High model confidence,
   repeated extraction, user confirmation and fresh supporting evidence need
   separate meanings. Lexical overlap also does not establish agreement.

Owning code: [worker](../app/core/relationship/belief_worker.py),
[store](../app/core/relationship/belief_store.py),
[detector](../app/core/relationship/belief_gap_detector.py),
[post-turn caller](../app/core/session/post_turn_mixin.py),
[affect state](../app/core/affect/affect_state.py),
[trusted rendering](../app/core/session/inner_life_part2.py),
[panel](../web/src/features/settings/memory/BeliefsPanel.tsx).

## Intended role

| Layer | What it should hold |
| --- | --- |
| Memory | What was actually said or happened, with source and time. |
| Belief | Aiko's tentative, situation-specific interpretation of what that means now. |
| Concept | More durable understanding supported across original sources and contexts. |

Stable relationship warmth should not be rediscovered as many new mood beliefs.
Concepts can supply the known baseline and help identify a new nuance or change.
They must not count as fresh evidence for a belief. A later concept must likewise
remain grounded in original sources, rather than becoming “confirmed” because a
belief repeated an existing interpretation.

## Proposed record and worker contract

Keep a short canonical subject for retrieval, but retain a complete claim and:

- the exact supporting user message IDs and validated quotes;
- a short reason explaining how the interpretation follows, explicitly marked
  as inference where appropriate;
- the relevant situation, people and time, including what is unknown;
- qualifications, alternatives or exceptions that materially change the reading;
- what new evidence could strengthen, revise or retire it;
- an optional soft implication for response, used only in matching context.

Allow mixed feelings and separate a temporary state from a durable relationship
stance. Frustration about an exchange can coexist with affection. An inference
should not invent a psychological explanation merely to sound deeper.

Require genuinely new supported meaning to emit a belief. Supply existing
relevant beliefs and grounded concept baselines as dedupe/context only. Default
to zero output; permit one or two useful changes, with no quota to fill. Old
turns may resolve references but cannot create another observation of themselves.
Minimal adjacent assistant context can resolve “her/you/that” while remaining
reference context rather than evidence of the user's feelings.

Dedupe the full claim with its subject, situation and qualifiers, both within a
batch and against stored beliefs. Combine paraphrases and attach independent
evidence to an existing reading. Preserve genuinely different meanings: affection,
feeling secure, and considering someone's comfort are not interchangeable merely
because all are positive.

## Verification and lifecycle

First remove Aiko's own affect as an automatic verifier of user mood. Strong
status changes should require explicit user statements/corrections, separately
supported episodes, or a scoped semantic check using the actual evidence.
Related words, model agreement and overlapping windows are insufficient.

Distinguish tentative inference, user-confirmed claims and corroboration across
separate episodes. Give short-lived feelings an appropriately short applicability
window; review durable opinions on relevant new evidence. Age by last genuine
support, not by the last time a detector inspected the row. Confirmation should
not mean a temporary mood remains applicable forever.

Keep contradiction history without imposing a permanent penalty on a genuinely
new situation. The current “ever had a gap” flag prevents automatic confirmation
after revival; a changing feeling needs a new scoped reading or revision, not an
endless manual-review loop for the old wording.

Render beliefs as context-sensitive understanding, not a declaration that Aiko
can assume the user's internal state. Use them to listen and respond more
appropriately, without repeatedly announcing a reading or asking for approval.

## Example of useful depth

This is an illustrative example, not a claim about the user's actual state.
Given a message such as “I'm tired, but stay with me for a bit”:

- **Subject:** Quiet company before sleep.
- **Reading:** They may want companionship without a demanding conversation.
- **Basis:** The actual linked message, with its date.
- **Scope:** This exchange; not a general preference to avoid conversation.
- **Possible response:** Stay warm and present, leave room for silence, keep
  questions light. Tiredness alone does not imply emotional distance.
- **Revision:** Follow their lead if they request another subject or active help.

If a source only establishes affection, keep one simple affectionate reading.
Do not embellish it into certainty about security, motives or unspoken needs.

## UI and implementation order

Show the full reading, its reason, source/date and applicability, with evidence
expandable. Group semantic variants and offer correction of scope or wording,
not just a binary confirmation. Display evidence strength in ordinary language;
keep valence/arousal numbers in diagnostics until they have a valid verification
meaning. Default to a small useful set rather than a large review workload.

1. Fix evidence identity, repeated-window intake, the mood comparator and
   support-based freshness before increasing expressiveness.
2. Add full claims, source-backed rationale, scope and semantic novelty; keep
   durable understanding aligned with the existing concept layer.
3. Update prompt consumption and the review UI together. Review legacy rows
   against their sources; do not preserve old confidence/status as certification.

Acceptance contrasts: replaying the same window creates no extra observation;
independent evidence strengthens a matching scoped claim; paraphrases do not
multiply rows; real qualifiers survive dedupe; ambiguous subjects are resolved
or deferred; affection and situational frustration can coexist; Aiko's mood does
not contradict the user's; expired feelings do not remain current; and the
resulting context produces useful response guidance rather than repeated
confirmation questions. Structural tests must be followed by fresh ordinary-use
review of support, novelty, uncertainty, omissions and conversational usefulness.
