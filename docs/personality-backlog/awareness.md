# Awareness + grounding

The goal of this section is to reduce confident hallucination by making
Aiko's uncertainty visible to herself — both as structured state she
can act on and as background work that closes gaps over time. F1
(background fact-checker), F2 (knowledge-gap journal), and F3
(confidence column) shipped together as the foundation; see
[`shipped.md`](shipped.md) for the implementation summary. The two open
follow-ups below build on that foundation.

---

## F4. Source-cited memories

When a memory originates from a tool call (`web_search` / `recall` /
document upload), persist the source URL or document id in
`metadata.source_url` (reuses the v7 generic metadata column). Aiko
cites naturally ("according to a thing I read last week..."). The
Memory tab grows a "from web" badge that links out. Key files:
[`app/core/memory_store.py`](../../app/core/memory_store.py),
[`app/llm/tools/web_search.py`](../../app/llm/tools/web_search.py),
Memory tab in [`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx).
Pairs naturally with F1, which would stamp its own `source_url` on
fact-check rewrites, and with G3's `curiosity_finding` memories which
already know the search query but don't yet record the winning URL.

---

## F5. Conflicting-memory detector

Periodic background worker (registers with the shipped
`IdleWorkerScheduler`) that scans pairs of memories with high cosine
similarity but lexically contradicting content (`hates X` vs
`loves X`). Surfaces in a "Conflicts" sub-tab of the Memory tab for
the user to resolve, with a one-click "keep this, drop the other"
action. Persona allows `[[conflict:reason]]` self-tag for Aiko to
flag a contradiction she notices in flight. Key files: new
`app/core/memory_conflict_worker.py`, [`app/core/memory_store.py`](../../app/core/memory_store.py),
[`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)
Memory tab. Confidence (F3) gives the resolver a tiebreaker — when
two memories disagree, prefer the higher-confidence one and demote
the loser rather than deleting outright.
