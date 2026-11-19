# Awareness and grounding

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## F4. Source-cited memories

**Status: Partial.**

**Implemented / available:** Knowledge/curiosity findings already retain source metadata; Diary
UI/tool expose research sources.

**Remaining:** Carry usable citation provenance through fact-check rewrites and remaining
tool/document writers; add the general Memory-tab source badge/link. Metadata existence is not
end-to-end citation coverage.

[Implemented slice](shipped/audit-2026-10-24.md#awareness-f4).

**Source check:** [idle_knowledge_worker.py](../../app/core/proactive/idle_knowledge_worker.py),
[MemoryTab.tsx](../../web/src/features/settings/MemoryTab.tsx),
[DiaryTab.tsx](../../web/src/features/settings/DiaryTab.tsx),
[test_idle_curiosity_worker.py](../../tests/test_idle_curiosity_worker.py),
[memory_store.py](../../app/core/memory/memory_store.py),
[web_search.py](../../app/core/tasks/handlers/web_search.py).

### F4 follow-up. Validate and select topic digest claims at recall

**Status: Partial.** The
[18 November review](../reviews/grounded-cognition-review-2026-11-18.md#2-let-clusters-organize-recall-and-preserve-each-claims-authority)
found a mixed digest stored with claim-level authorities and an expired digest
accepted by cluster lookup despite the ordinary temporal gate. Another digest
added precise time and event details beyond its cited memory.

Ordinary recall, cluster expansion and the recall tool now check the saved
source IDs, revisions, eligibility and expiry before showing a clause. Subject,
scope, source domain and citation IDs stay attached to their clause. Cluster
expansion uses its ordinary sibling budget when no clause survives. Fixtures
cover source edits, deletion, supersession, expiry and an unchanged clause in a
partially stale digest. See [Topic digest recall](../../docs/longitudinal-memory.md#topic-digest-recall).

- Rank valid clauses by the authority and time scope the query needs so a
  relevant clause gets room in the turn's context budget.
- Contrast a history question with a world-knowledge question using the same
  mixed topic. Score unsupported dates, agency and details at digest construction
  as a separate check from retrieval freshness. Preserve topic assignments while
  assessing whether authority-aware selection fixes the observed problem.

**Acceptance.** Both read paths reject stale clauses. A valid clause remains
available after an unrelated member changes if its own support still applies.
When the summary is unavailable, an eligible direct source can still reach the
reply. An external generalization stays separate from a user's lived experience.

## F11. Relevance-driven memory resurrection (latent recall from the archive)

**Status: Open implementation.**

**Implemented / available:** RAG hedges, topic reactivation, aged callbacks and revival retention exist.

**Remaining:** Add topic-return-triggered archive nomination, relevance/staleness checks and a
hedged callback sharing F15 cadence. Existing age callbacks are not this archive resurrection path.

**Source check:** [post_turn_helpers_mixin.py](../../app/core/session/post_turn_helpers_mixin.py),
[rag_retriever.py](../../app/core/rag/rag_retriever.py),
[memory_decay_worker.py](../../app/core/memory/memory_decay_worker.py),
[memory_promotion_worker.py](../../app/core/memory/memory_promotion_worker.py),
[topic_graph.py](../../app/core/conversation/topic_graph.py),
[long_arc_callback.py](../../app/core/conversation/long_arc_callback.py).

<a id="f12-revival-is-a-bag-of-words-test-and-it-credits-the-wrong-party---semantic-half-shipped"></a>

## F12. Attributable user-side memory retention credit

**Status: Partial.**

**Implemented / available:** Shared lexical/semantic echo, discounted semantic revival and
threshold-based scratchpad retention are implemented.

**Remaining:** Design attributable user-side retention credit and decide tiers-disabled recording.
Shared following-turn engagement is not item-level usefulness; coordinate H34/RS9. F17 owns
semantic-credit calibration.

[Implemented slice](shipped/audit-2026-10-24.md#awareness-f12).

**Source check:** [echo_detector.py](../../app/core/memory/echo_detector.py),
[post_turn_helpers_mixin.py](../../app/core/session/post_turn_helpers_mixin.py),
[engagement_tracker.py](../../app/core/affect/engagement_tracker.py),
[post_turn_mixin.py](../../app/core/session/post_turn_mixin.py),
[memory_store.py](../../app/core/memory/memory_store.py),
[memory_settings.py](../../app/core/infra/memory_settings.py).

## F17. Should a semantic echo earn full retention credit?

**Status: Validation remaining.**

**Implemented / available:** Cosines, echo-kind breakdown and floor replay are recorded and inspectable.

**Remaining:** Separate real paraphrase use from topical leakage using source/item-level outcomes
before changing credit. Similar shared-turn engagement does not establish usefulness. No credit
policy was changed in this audit.

12 Nov: [the ordinary-use review](../reviews/ordinary-use-review-2026-11-12.md) found
and fixed lifetime header counts mixed with windowed rates. The recent cohort
has 14,860 item rows across 233 measured replies, with 190 unsettled rows.
This fixes denominators; item-level semantic-use validation remains open.

[Implemented slice](shipped/audit-2026-10-24.md#awareness-f17).

**Source check:** [surfacing_outcome_store.py](../../app/core/memory/surfacing_outcome_store.py),
[surfacing_outcome_tools.py](../../app/mcp/server_tools/surfacing_outcome_tools.py),
[test_semantic_echo.py](../../tests/test_semantic_echo.py).

## F15. Memory repair requests -- admit the hole instead of getting vaguer

**Status: Open implementation.**

**Implemented / available:** Retrieval computes faded/uncertain state; answer-capture patterns and
L32 importance already exist.

**Remaining:** Nominate low-stakes degraded-but-important memories, ask once, capture a
source-linked answer and refresh the right row. Share F11 cadence and exclude sensitive losses. The
old claim that L32 is unbuilt is stale.

**Source check:** [rag_retriever.py](../../app/core/rag/rag_retriever.py),
[knowledge_gap_extractor.py](../../app/core/memory/knowledge_gap_extractor.py),
[memory_decay_worker.py](../../app/core/memory/memory_decay_worker.py),
[memory_store.py](../../app/core/memory/memory_store.py),
[inner_life_part2.py](../../app/core/session/inner_life_part2.py),
[idle_gap_resolver.py](../../app/core/conversation/idle_gap_resolver.py).

## Moved out of the active queue

- **F7** — retired: [record](shipped/audit-2026-10-24.md#awareness-f7).
