# Awareness + grounding

The goal of this section is to reduce confident hallucination by making
Aiko's uncertainty visible to herself — both as structured state she
can act on and as background work that closes gaps over time. F1
(background fact-checker), F2 (knowledge-gap journal), F3 (confidence
column), and F5 (conflicting-memory detector) shipped together; see
[`shipped.md`](shipped.md) for the implementation summary. The one
remaining follow-up below builds on that foundation.

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
