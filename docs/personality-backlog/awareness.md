# Awareness + grounding

The goal of this section is to reduce confident hallucination by making
Aiko's uncertainty visible to herself — both as structured state she
can act on and as background work that closes gaps over time. F1
(background fact-checker), F2 (knowledge-gap journal), F3 (confidence
column), and F5 (conflicting-memory detector) shipped together; see
[`shipped.md`](shipped.md) for the implementation summary. The
remaining follow-ups below build on that foundation — everything that
landed, including the whole K-time family, lives in
[`shipped/awareness.md`](shipped/awareness.md).

**Web-search backend (2026).** Web search is now pluggable behind
[`app/llm/search/providers.py`](../../app/llm/search/providers.py):
DuckDuckGo (keyless default) or LangSearch (hybrid search + long-text
summaries, when an API key is configured under the `search` settings
block), with a DuckDuckGo fallback. F6 (query reformulation) shipped
with it; F7 (domain routing) is obsolete as a result. LangSearch's
**Semantic Rerank API** is intentionally **not** wired — Aiko's RAG is
already a local cosine pass and web results come back ranked +
summarized, so a second per-call API hit against the free-tier quota
isn't worth it. Revisit only if a concrete relevance problem appears.

---

## F4. Source-cited memories

When a memory originates from a tool call (`web_search` / `recall` /
document upload), persist the source URL or document id in
`metadata.source_url` (reuses the v7 generic metadata column). Aiko
cites naturally ("according to a thing I read last week..."). The
Memory tab grows a "from web" badge that links out. Key files:
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py),
[`app/llm/tools/web_search.py`](../../app/llm/tools/web_search.py),
[`app/core/proactive/idle_curiosity_worker.py`](../../app/core/proactive/idle_curiosity_worker.py)
(stamps the winning result URL onto each `curiosity_finding`),
[`app/core/memory/idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)
(stamps the citation source onto fact-check rewrites),
Memory tab in [`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx).
Pairs naturally with F1, which would stamp its own `source_url` on
fact-check rewrites, and with G3's `curiosity_finding` memories which
already know the search query but don't yet record the winning URL.

**Status nudge.** The metadata column is already live (schema v7);
this is pure plumbing through three writers + a UI badge. Cheaper
than the entry implies.

**Correction.** The path is `app/llm/tools/builtins.py`
(`WebSearchTool`), not the non-existent `app/llm/tools/web_search.py`
referenced above. The background search lane lives in
[`app/core/tasks/handlers/web_search.py`](../../app/core/tasks/handlers/web_search.py)
(`WebSearchHandler`), and `web_search` is no longer a brain builtin —
it's a workflow skill plus two private worker instances (F1
fact-checker, G3 curiosity). Keep this in mind for F6-F9 below.

---

## F7. Domain-aware source routing (MyAnimeList, music, games, film) — OBSOLETE

**Status: obsolete / superseded.** The web-search backend is now
pluggable and defaults to LangSearch when configured (a hybrid
keyword + vector search that returns clean long-text summaries from
billions of documents — see the `search` block in
[`docs/configuration.md`](../configuration.md) and
[`app/llm/search/providers.py`](../../app/llm/search/providers.py)).
That directly attacks the "generic web slop" problem this entry was
meant to solve, and LangSearch has no `site:` parameter to inject
anyway, so the per-domain routing mechanism does not port. If a future
need for structured per-source data (e.g. Jikan/MAL fields) reappears it
should be a dedicated fetch handler, not query routing. No code planned.

**Motivation.** Search is DuckDuckGo-only with no source steering, so
domain questions get generic web slop instead of the canonical source.
For anime specifically the user wants MyAnimeList; the same shape covers
music, games, and film. Better sources → more specific, more accurate
findings → directly attacks the "general response" problem.

**Key files.**
[`app/core/tasks/handlers/web_search.py`](../../app/core/tasks/handlers/web_search.py)
(`WebSearchHandler` — add a pre-search domain classifier + `site:`
injection), optionally a new `app/core/tasks/handlers/jikan.py` (the free
unauthenticated **Jikan** MyAnimeList API),
[`app/core/tasks/workflow/skill_registry.py`](../../app/core/tasks/workflow/skill_registry.py)
(register any new fetch skill), the two worker `WebSearchTool` callers.

**Sketched approach.** Start cheap: a small keyword/embedding classifier
maps a query to a domain and prepends a `site:` filter — anime →
`site:myanimelist.net`, music → MusicBrainz / `site:rateyourmusic.com`,
games → `site:igdb.com`, film/TV → Letterboxd / TMDB. Phase 2 (optional):
a dedicated `Jikan` fetch handler for structured MAL data (titles,
studios, scores, genres — no auth, generous rate limit) so anime
enrichment returns clean fields instead of scraped HTML. Config-gated per
source so a user can disable any of them.

**Effort.** Medium.

---

## F11. Relevance-driven memory resurrection (latent recall from the archive)

**Motivation.** Today "revival" is **citation-driven and passive**:
`revival_score` (schema v8) is bumped post-turn only for memories Aiko actually
cited (`_mark_revived_memories` in
[`post_turn_helpers_mixin.py`](../../app/core/session/post_turn_helpers_mixin.py)),
and `MemoryStore.decay()` gives a small rebate proportional to it so cited rows
resist fading. Nothing *proactively* reaches into the `archive` tier when a
dormant topic comes back to life. The payoff callback — "this might be
completely out of date, but I vaguely remember you once mentioning macro
photography two years ago; are you getting back into it?" — is exactly what
makes someone feel genuinely remembered, and it's driven by **latent
relevance**, not age. The thing to avoid is resurrecting a memory just because
it's old.

**Key files.**
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) (archive-tier scoring
+ the `(distant)` / `(faded)` K25 hedges already live here),
[`memory_decay_worker.py`](../../app/core/memory/memory_decay_worker.py) /
[`memory_promotion_worker.py`](../../app/core/memory/memory_promotion_worker.py)
(tier transitions into/out of `archive`),
[`topic_graph.py`](../../app/core/conversation/topic_graph.py) (the
re-engagement trigger — K6 "return-to-known" / cluster reactivation),
[`long_arc_callback.py`](../../app/core/conversation/long_arc_callback.py) +
[`callback_detector.py`](../../app/core/conversation/callback_detector.py)
(existing aged-callback lanes to build on, not duplicate).

**Sketched approach.** Trigger on **topic re-engagement**: when the live user
text re-activates a cluster that has been quiet for a long stretch (reuse the
K6 return-to-known signal + F10 cluster recency), run a scoped semantic search
of the `archive` tier for that cluster/topic. Surface a hit only when latent
relevance clears a bar (strong cosine to the current topic AND meaningfully
stale) and hand it up as a **hedged** callback cue — phrased with the K25
"(distant)" register and an explicit "may be out of date, treat as a soft
question" guard — rather than a confident statement. Standard anti-nag
signature + cooldown so a re-opened topic doesn't dredge the archive every turn.

**Relation to existing work.** This *reweights* revival toward relevance; it
doesn't replace the citation-driven `revival_score` rebate (that stays as the
"kept alive because we keep using it" signal). Pairs with the concept layer:
an `active` concept or cluster coming back into focus (L4 / L5) is a natural
resurrection trigger too.

**F15 is the other half of this.** Where F11 resurfaces an old memory's
*content* as a hedged callback, F15 surfaces the *hole* where a memory used to
be and asks him to refill it. They are the two honest responses to a degraded
memory and should almost certainly **share a cooldown** — doing both in one
session ("I vaguely remember you mentioning X" followed later by "I've lost the
detail on Y") reads as a companion preoccupied with her own memory rather than
with him. Build whichever first, but wire the shared gate at the same time.

**Open questions.** Latent-relevance threshold (cosine floor + minimum
staleness)? Proactive nudge (via `prepared_nudge`) vs. a passive RAG boost that
only fires when the topic is already live? Cap on resurrections per session?

**Effort.** Medium.

---

## F12. Revival is a bag-of-words test, and it credits the wrong party — ✅ SEMANTIC HALF SHIPPED

The semantic half shipped: see
[`shipped/awareness.md`](shipped/awareness.md#f12-semantic-echo--revival-stops-only-crediting-what-aiko-quotes).
Revival now falls back to cosine when the keyword test misses, through a
shared [`echo_detector`](../../app/core/memory/echo_detector.py) that also
decides the L37 ledger's `echoed` column.

Two things the implementation learned that this entry got wrong, both worth
keeping in view:

- **The cosine is measured on an already-topically-filtered set.** Surfaced
  memories are the top-k *nearest* to the turn and the reply is about that
  same turn, so a high cosine is close to guaranteed. It partly measures
  "was on topic" rather than "she used it", which makes the floor much less
  discriminative than it looks.
- **Full credit would have switched scratchpad TTL off.** The TTL gate was
  `revival_score == 0.0` exactly, so *any* bump made a row permanently
  exempt. A semantic hit therefore earns a smaller bump than a quote, and
  the gate became a threshold.

**Still open here:** the *user-side credit* half below — rewarding a memory
for the user's engagement rather than Aiko's own verbosity — plus open
question (3) on `tiers_enabled=false`. Whether a semantic hit should earn
full retention credit is now [F17](#f17-should-a-semantic-echo-earn-full-retention-credit).

<details>
<summary>Original entry (design reasoning for the shipped half)</summary>

**Motivation.** `revival_score` is the closest thing the memory layer has to a
learning signal — it earns a decay rebate and gates promotion out of
`scratchpad`, so it genuinely shapes which memories survive. It is decided by
`_mark_revived_memories`
([`post_turn_helpers_mixin.py`](../../app/core/session/post_turn_helpers_mixin.py)):
tokenise Aiko's reply and the surfaced memory, drop stopwords and anything
under four characters, and if at least `revival_min_word_overlap` (default 3)
content words are shared, bump the score.

Two problems, and the second is the important one.

First, it is lexical. Aiko paraphrases constantly — that is the entire point of
handing a memory to a language model rather than pasting it — and a
paraphrase shares no credit. "You mentioned wanting to get back into film
photography" against a stored "user shot 35mm in college and misses it" is a
perfect use of the memory and scores zero overlap. The test is strict enough to
rarely produce false positives, which means its errors are almost all misses;
the memories that survive are the ones Aiko happens to quote, not the ones she
uses well. The embedding to fix this is already computed for the turn.

Second, and worse: it measures whether **Aiko echoed** the memory, not whether
**the user cared**. So does the K22 callback detector, which bumps the same
field from a different angle. The system's one durable memory-quality signal is
therefore a measure of Aiko's own verbosity about a memory, and a memory she
mentions constantly to visible indifference accumulates exactly the same credit
as one that opens the user up. `EngagementTracker`
([`engagement_tracker.py`](../../app/core/affect/engagement_tracker.py))
already produces the missing half and nothing reads it here.

**Key files.**
- [`post_turn_helpers_mixin.py`](../../app/core/session/post_turn_helpers_mixin.py)
  — `_revival_tokens` / `_mark_revived_memories`, and the `_REVIVAL_STOPWORDS`
  set that exists only to make the lexical test tolerable.
- [`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — the
  engagement block, and the `_mark_revived_memories` call site that needs to
  move after it (or read the settled outcome from L37's ledger).
- [`memory_store.py`](../../app/core/memory/memory_store.py) —
  `mark_revived`, and the `decay` rebate / promotion gate that consume the
  score, which is what makes this worth getting right.
- [`memory_settings.py`](../../app/core/infra/memory_settings.py) —
  `revival_min_word_overlap`, `revival_per_hit`.

**Sketched approach.** Two independent halves; either ships alone.

*Semantic echo.* Keep the lexical test as a fast path (a quoted memory is
unambiguous), and when it misses, fall back to cosine between the reply
embedding and the stored memory embedding — both already available, no new
embed call on the hot path. A conservative floor, since the reply is a whole
turn and will have moderate similarity to almost any on-topic memory; this
wants calibrating against real transcripts rather than a guessed constant, and
`revival_per_hit` should probably be *smaller* for a semantic hit than a
lexical one.

*User-side credit.* Split the bump into two components: `echo` (Aiko used it,
what exists today) and `landed` (the user's engagement on the following turn).
The off-by-one attribution described in L37 applies exactly here — the
engagement recorded at post-turn *N* describes the reaction to reply *N-1* — so
this half is much cleaner built on the L37 ledger than bolted onto the current
call site. Only ever reward; never subtract for a `disengaged` label, or a
memory will lose standing because the user was tired.

**Open questions.** (1) Cosine floor, and whether it needs to be relative to
the turn's other memories rather than absolute. (2) Does `revival_score` remain
one number, or become two fields? One number is less disruptive to the decay
rebate and promotion gate; two is more honest and lets L38's memory analogue
read them separately. (3) Whether `tiers_enabled=false` should still record the
signal even though nothing consumes it — currently the whole path is skipped,
so a user with tiers off accumulates no history at all and gets no benefit if
they switch it on later.

**Effort.** Small (semantic echo alone) / Medium (with user-side credit via the
ledger).

**Depends on.** Nothing for the semantic half. L37 for the user-side half.

</details>

---

## F17. Should a semantic echo earn full retention credit?

**Motivation.** F12 shipped with a deliberate discount: a memory Aiko
*quoted* earns `revival_per_hit` (0.15) and one she merely came close to in
embedding space earns `semantic_revival_per_hit` (0.05), which sits below the
`scratchpad_ttl_min_revival` (0.10) bar that spares a row from cleanup. The
argument was that surfaced memories were selected for topical similarity to
the turn in the first place, so cosine against the reply partly measures
"was on topic" rather than "she used it" — and under full credit nearly every
scratchpad row would have become permanently exempt from TTL, switching
cleanup off rather than improving what survived.

That argument is plausible and **untested**. It was the right default
because it is the conservative one — it preserves existing turnover — but it
may be wrong in either direction, and the discount currently applies to
genuine paraphrase as well as to topical coincidence, which is exactly the
case F12 existed to reward.

**What decides it.** Schema v27 records the cosine of every comparison,
misses included, so this is a read rather than an experiment:

- `echo_breakdown` — engaged rate per `echo_kind`. If `semantic` rows engage
  about as often as `lexical` ones, the discount is unjustified and semantic
  hits should earn full credit. If they engage no better than rows with no
  echo at all, the signal is topical leakage and the discount was right.
- `semantic_floor_candidates` — replays each candidate floor over recorded
  cosines, restricted to rows the lexical test did not already claim. A floor
  whose engaged rate climbs with strictness is measuring use; one that is
  flat all the way up is measuring topic.

Both are already in `get_surfacing_outcomes`. **This item is waiting on
data, not on code** — it needs enough settled rows to read, which means real
conversation over weeks rather than a synthetic fixture.

**Sketched approach.** Read the two aggregates. Then one of:

1. **Raise the floor** if the rate climbs with strictness — the cheapest
   outcome, a settings change with the discount left intact.
2. **Grant full credit** if semantic and lexical engage alike, folding
   `semantic_revival_per_hit` back into `revival_per_hit`. Note this makes
   `scratchpad_ttl_min_revival` load-bearing in a new way and the TTL
   turnover rate should be watched after the change, not assumed.
3. **Make the credit continuous** in the cosine rather than a step at the
   floor — the most honest option and the most likely to be right, since the
   evidence really is graded, but it means the TTL bar becomes a statement
   about cosine and wants its own look.

Worth checking against `echo_rate` on the same items: high echo with low
engaged means she takes the bait and the user does not, which is a different
finding from the memory being useless.

**Open questions.** (1) Should the floor be *relative* to the turn's other
surfaced memories rather than absolute? A fixed floor inherits the retrieval
threshold's calibration; a relative one asks "did she use *this* one more
than the others she was shown", which is closer to the real question and
immune to the topical-similarity floor effect. (2) Does the answer differ by
memory `kind` — a one-line `preference` and a multi-sentence `event` are not
equally comparable to a whole reply. (3) Same question for concepts, which
F12 deliberately left lexical-only because a short label's embedding sits in
a different part of the space than prose.

**Effort.** Small (read + settings change) / Medium (option 3).

**Depends on.** F12 (shipped) for the recording; several weeks of ledger
data before the read means anything.

---

## F13. The contradiction family's missing fourth corner -- *he* corrects *her*

**Motivation.** The docstring at the top of
[`self_correction_detector.py`](../../app/core/conversation/self_correction_detector.py)
names the contradiction family explicitly, and it has a hole in it:

- **F5** ([`memory_conflict_worker.py`](../../app/core/memory/memory_conflict_worker.py))
  — two *stored* memories contradict each other.
- **K29** (opinion injection) — Aiko's stored stance vs. the *user's* claim.
- **K38** (here) — Aiko's just-spoken *reply* vs. her own stored fact.
- **Nothing** — the *user explicitly correcting her*. "No, it's not X, it's Y."
  "I never said that." "It's my sister, not my brother."

That last one is the **highest-quality signal this system will ever receive**:
an unambiguous supervisory label from the only authority on the subject, offered
for free, in the moment. Today it is treated as ordinary conversation. If the
extractor happens to notice, the correction lands as a new `fact` at the default
0.7 confidence — sitting *beside* the wrong memory, which is also at 0.7 and
still fully retrievable. So the most reliable evidence available arrives with the
same weight as a passing remark, and the thing it disproves is not touched.

This is the single clearest instance of the pattern the surfacing audit found:
a loop that terminates in a database write. Every other corner of the family got
a detector; the one that matters most got none.

**Key files.**
- [`conflict_heuristics.py`](../../app/core/memory/conflict_heuristics.py) —
  `classify_pair` is the shared contradiction heuristic the other three corners
  use, and is the natural basis for matching a correction to what it corrects.
- [`self_correction_detector.py`](../../app/core/conversation/self_correction_detector.py)
  — closest structural sibling; a correction detector wants the same shape (pure
  function over text plus candidate memories, post-turn hook decides what to do).
- [`memory_store.py`](../../app/core/memory/memory_store.py) — needs a
  supersede path: `add` at high confidence *plus* demotion of the corrected row,
  which is more than `mark_used` / `mark_revived` currently offer.
- [`concept_belief_reviser.py`](../../app/core/concepts/concept_belief_reviser.py)
  — if the corrected claim is evidence under a concept, the concept's confidence
  should move too; this is the existing revision path to reuse rather than
  duplicate.
- [`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — where the
  detector runs, next to the K38 hook.

**Sketched approach.** Detect on the *user's* message, against the memories that
were surfaced on the previous turn — a correction is almost always a response to
something she just said, which narrows the candidate set to something tiny and
makes this cheap. Explicit negation and repair patterns first ("not X, Y",
"I never said", "actually it's"), since precision matters far more than recall
here: a missed correction costs nothing beyond the status quo, while a false
positive rewrites a true memory with a wrong one.

On a confident match, do three things the current path does none of: write the
correction at high confidence, **demote the corrected memory** rather than
leaving it as an equal-weight competitor, and record the supersede link so the
history is auditable and reversible. Then let her acknowledge it naturally on
the next turn — being visibly corrected and taking it well is a better beat than
silently swallowing it.

The distinction to hold onto: **correction of fact, not disagreement of
opinion.** "I don't think that's right" about a stance is K29's territory and
must not trigger a memory rewrite. Getting this boundary wrong would let an
argument about taste overwrite her record of what he likes.

**Open questions.** (1) Explicit patterns only, or an LLM classifier on the
post-turn path? Patterns first — the LLM version has better recall but a false
positive here is unusually expensive. (2) Should a correction also *raise* the
confidence of neighbouring memories he did not dispute? Tempting and probably
wrong: silence is not endorsement. (3) How visible should the mechanism be —
"I've updated that" is reassuring but edges toward narrating machinery (see the
L35 rule in [`concepts.md`](concepts.md)); a plain "ah, my mistake" is safer.
(4) Does a correction deserve to propagate to the *concept* layer immediately,
or wait for the normal lifecycle pass? Immediately, probably, since a
contradicted concept surfacing again right after being corrected is exactly the
failure this item exists to prevent.

**Effort.** Medium. The detector is small; the supersede semantics in the store
and the concept propagation are where the care goes.

**Depends on.** Nothing hard. Shares `classify_pair` with F5 / K38. Pairs with
F14 (the other direction of "I was wrong").

---

## F14. "I was wrong about that" -- let fact-check reversals reach him

**Motivation.** The F1 idle fact-checker
([`idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)) does real
work: it researches stored claims against the web, adjusts confidence by up to
±0.3, and can rewrite a memory outright. Then the loop ends. The only outward
signal is `notify_memory_updated`, which refreshes a list in the UI.

So the sequence is: Aiko tells the user something, later discovers on her own
initiative that she had it backwards, silently fixes her notes, and **never
mentions it**. Every ingredient of the most trust-building thing a companion can
do is already in place and unconnected — coming back unprompted with "I looked
into that thing I told you about and I had it wrong, it's actually…". It is also
the only real evidence a user would ever get that the background machinery
exists at all; right now dozens of workers think about him and none of it is
visible except as vibes.

**Key files.**
[`idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py) (the
`delta` / `confidence_before` / `confidence_after` detail dict it already
builds is exactly the reversal record needed),
[`memory_facade_mixin.py`](../../app/core/session/memory_facade_mixin.py) (the
enqueue path and the personal-claim filter),
[`inner_life_part2.py`](../../app/core/session/inner_life_part2.py) (where a
one-shot T6 cue would render, alongside the other repair-family blocks), the
`prepared_nudge` path if it should be able to open a turn proactively rather
than waiting to be spoken to.

**Sketched approach.** Only a genuine **reversal** qualifies, not confidence
drift: the claim was stated to the user with reasonable confidence, and
research contradicted it or rewrote it. Confidence sliding 0.7 to 0.65 is
noise and must not produce a cue, or she becomes a pedant who is constantly
amending herself.

Additional bar worth enforcing: **she must actually have said it.** A
low-confidence memory quietly corrected before it ever surfaced is not
something to apologise for. That means checking the claim against surfacing
history, which the L37 ledger answers directly and nothing else does.

One at a time, hard rate limit, and expiring — a reversal discovered three
weeks ago is no longer a conversational event. Whether it can *open* a turn
(via `prepared_nudge`, so she raises it unprompted) or only rides along on the
next natural turn is the main design choice; unprompted is the stronger beat and
the higher nag risk.

**Open questions.** (1) Reversal bar — sign flip plus a minimum magnitude, or
does a rewrite always qualify regardless of delta? (2) The personal-claim filter
means most user-specific facts are never checked at all, so this will mostly
fire on world knowledge; is that the right scope, or does it make the feature
feel like a trivia correction service? (3) Interaction with F13 — if he already
corrected her, the fact-checker arriving later with the same news is redundant
and slightly insulting; the supersede link from F13 should suppress it. (4) Does
this want to cite its source (F4) to be credible?

**Effort.** Small. The data exists; this is a bar, a cue, and a cooldown.

**Depends on.** F1 (shipped). Wants L37 for the "did she actually say it" check
and F13 for suppression. F4 would make it more credible.

---

## F15. Memory repair requests -- admit the hole instead of getting vaguer

**Motivation.** The retrieval layer has a deliberate and well-argued invariant:
low confidence never *hides* a memory, it demotes it and tags it so Aiko hedges
— `(uncertain)` for a shaky claim, `(distant)` for raw age, `(faded)` for a
decayed low-salience row (see `_confidence_penalty` and the suffix logic in
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py), where the comment
states the principle: "never hiding things from Aiko is the simpler
invariant").

That is right for *using* a memory. But it means the only thing decay ever does
to her behaviour is **make her vaguer**. There is no path where the degradation
itself becomes the subject: "I know you told me something about your sister's
move and I've lost the detail — remind me?"

Two reasons that is worth building. First, it is the honest surface of a decay
system, and admitting fallible memory reads as *more* trustworthy than seamless
recall — a companion who never forgets anything is subtly inhuman, while one who
says "I've lost the thread on that, tell me again" is someone you believe when
she does remember. Second, and more practically: **asking is the only
rehydration mechanism the memory store would have.** Today a faded memory can
only be refreshed if the user happens to raise the topic himself. Nothing ever
reaches for the data it is losing.

This is distinct from its two nearest neighbours. F11 resurfaces an old
memory's *content* as a hedged callback ("I vaguely remember you mentioning
macro photography"). The `knowledge_gap` family tracks things she does not know about the *world*,
resolvable by web search.
F15 is about a decayed memory of *him*, resolvable only by asking him — the
`knowledge_gap` write path in
[`knowledge_gap_extractor.py`](../../app/core/memory/knowledge_gap_extractor.py)
is worth reading first to decide whether to reuse that store or parallel it.

**Key files.**
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) (the effective-
confidence and hedge machinery already computes everything needed to identify a
degraded-but-important row),
[`memory_decay_worker.py`](../../app/core/memory/memory_decay_worker.py) (knows
which rows crossed which thresholds and when — the natural place to nominate
candidates),
[`memory_store.py`](../../app/core/memory/memory_store.py) (the refresh write on
a successful answer: raise confidence and salience, restamp, and ideally record
that it was user-reconfirmed),
[`inner_life_part2.py`](../../app/core/session/inner_life_part2.py) (a T6 cue in
the gap-cue family), and
[`idle_gap_resolver.py`](../../app/core/conversation/idle_gap_resolver.py) as the
existing "did the answer arrive?" pattern to mirror.

**Sketched approach.** Nominate candidates on two axes at once — **degraded** (low
effective confidence, or faded by decay) and **still mattering** (it is about a
subject with high importance, or a cluster that is currently live). Degradation
alone is the wrong trigger; the archive is full of things that fading is the
correct outcome for. The interesting case is a memory she *should* still know.

Surface at most one, rarely, phrased as a genuine question rather than a quiz,
and — critically — **capture the answer**. A repair request that does not write
the reply back is worse than not asking, because it turns into asking the same
thing twice. The gap-resolver pattern already solves the "watch for the answer
on the following turns" problem and should be reused rather than reinvented.

Also worth encoding: never ask about something emotionally heavy this way.
"Remind me what your father died of" is a catastrophic version of this feature.
Restricting nominations to low-stakes kinds, or excluding anything with high
affective charge, is a hard requirement rather than polish.

**Open questions.** (1) How to score "still matters" — L32 concept importance is
the principled answer and is not built; cluster liveness is the available proxy.
(2) Does an unanswered repair request retry, or is one ask the whole budget? One
ask, probably — pressing twice is the nag failure. (3) Should this share a
cooldown with F11? Almost certainly: resurrecting an old memory and confessing
to losing one are both "old memory" beats and doing both in a session would be
odd. (4) Is there a version where she asks about something she *never* knew
rather than something she lost, and is that just the `knowledge_gap` path with a
personal subject?

**Effort.** Medium. Nomination scoring and the answer-capture loop are the work;
the cue itself is small.

**Depends on.** Nothing hard. Wants L32 (importance) to nominate well. Shares a
cooldown with F11. Answer capture mirrors the gap resolver, and is the same
problem as L30c.

---

## F16. Testimony vs. inference -- did he tell her, or did she guess?

**Motivation.** Nothing in the memory layer durably distinguishes **what the
user said** from **what Aiko concluded**. A `fact` written by the LLM extractor
from an explicit statement and a `fact` written from an inference across three
conversations are the same kind, at the same default confidence, rendered
identically into the prompt. The `self` / `self_tagged` split marks *who the
memory is about*, not *how it was learned*; F4 (source-cited memories) covers
external URLs, which is provenance for web knowledge rather than for
conversation.

The consequence is the classic uncanny failure: **asserting something he never
said.** "You told me you hate meetings" when he never said that — he said three
things that added up to it — is a small betrayal every time, and it is
unfalsifiable from his side in a way that makes the whole memory system feel
less trustworthy. The fix is not better accuracy, it is honest phrasing: "I get
the sense you'd rather skip meetings" is the same belief, correctly attributed,
and it *invites* correction (which feeds F13) instead of foreclosing it.

There is a second payoff. An inference is a much weaker claim than testimony and
should decay faster, be easier to contradict, and never be quoted back as fact.
Right now the ranking cannot tell them apart, so it cannot treat them
differently.

**Key files.**
[`memory_extractor.py`](../../app/core/memory/memory_extractor.py) (the LLM pass
that writes most `fact` / `preference` / `relationship` rows — the extraction
prompt is where testimony vs. inference would be labelled, and it already
classifies `temporal_type` so the pattern exists),
[`memory_store.py`](../../app/core/memory/memory_store.py) (`metadata` can carry
it without a schema change, though a real column would be better for filtering),
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) (`format_block`'s
suffix-tag machinery is exactly the right surface — this is another hedge tag
alongside `(uncertain)` / `(distant)` / `(faded)`),
[`concept_store.py`](../../app/core/concepts/concept_store.py) (concepts are
*inherently* inferential — a `generalization` is a conclusion by construction —
so the concept layer's rendering should adopt the same tentative voice).

**Sketched approach.** Have the extractor label each claim `stated` or
`inferred`, defaulting to `inferred` when unsure — the asymmetry matters,
because over-claiming testimony is the failure being fixed, while
under-claiming it is merely a bit tentative. Anything from a `[[remember:]]`
tag where the user asked to be remembered is `stated` by construction.

Then use it in two places: a phrasing hint on the rendered memory (the existing
suffix-tag mechanism, or better, a rendering voice — "you mentioned" vs. "I get
the impression"), and a small ranking distinction so testimony outranks
inference at equal cosine.

The concept-layer half is arguably the more valuable one and needs no new
labelling at all, since every concept is an inference by definition. Making
concept impressions read as tentative-by-default — which L41's reason-conditioned
phrasing is already shaped to do — would fix the over-claiming problem for the
whole L-series in one move.

**Open questions.** (1) `metadata` field or a real column? Column, if this is
going to be a ranking term. (2) Is a third value needed — `confirmed`, for an
inference the user has since agreed with? That is where this meets F13 and it is
probably the most useful state of the three. (3) Does the LLM extractor reliably
know the difference, given it only sees a window of transcript? Partly, and the
`inferred` default is the hedge against it not knowing. (4) Should a
long-standing inference that has never been contradicted eventually *earn*
testimony-like standing, or does that reintroduce exactly the over-claiming this
fixes? Leaning no — let it stay an impression forever, since that is what it is.

**Effort.** Small (label + phrasing) / Medium (with a column, ranking term, and
the concept-voice half).

**Depends on.** Nothing. Feeds F13 (an honestly-hedged claim is easier to
correct) and pairs with L41 (the concept-side rendering voice).
