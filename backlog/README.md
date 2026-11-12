# Aiko backlog and history

This folder owns unfinished work, future plans, reviews and historical rollout
records. Current behavior belongs in [`docs/`](../docs/README.md). An old review
describes its dated baseline; it is not a specification of the latest code.

| Area | Start here |
| --- | --- |
| Workflow follow-ups and validation | [Current workflow backlog](plans/agent-workflows.md) |
| Personality and subsystem work | [Open index](personality/index.md) |
| Completed personality work | [Shipped index](personality/shipped.md) |
| Broader plugin/job platform design | [Platform roadmap](personality/plugin-system.md) |
| Ordinary-use quality findings | [12 November review](reviews/ordinary-use-review-2026-11-12.md), [earlier follow-up](reviews/ordinary-use-follow-up.md) |

## Reviews

- Workflows: [6 November harness audit](reviews/agent-harness-audit-2026-11-06.md),
  [8 November Hermes comparison](reviews/hermes-harness-review-2026-11-08.md).
- Memory and workers: [producer audit](reviews/memory-producer-audit-2026-11-05.md),
  [boundary audit](reviews/memory-boundary-audit-2026-11-06.md),
  [worker audit](reviews/worker-quality-audit-2026-11-06.md),
  [memory/topic review](reviews/memory-topic-correctness.md),
  [fast-tool audit](reviews/fast-brain-tools-audit-2026-11-06.md).
- Understanding: [belief proposal](reviews/belief-quality-proposal-2026-11-05.md),
  [concept grounding evaluation](reviews/concept-grounding-evaluation.md),
  [strategy/lifecycle review](reviews/strategy-lifecycle-review.md).

- Voice: [engine research and audition notebook](reviews/tts-engine-options.md).

## Historical plans and rollouts

- [7 November harness assessment](plans/agent-harness-next-steps-2026-11-07.md)
  and [plugin settings design](plans/plugin-settings-runtime-plan-2026-11-07.md).
- [Concept/pattern rollout](rollouts/concept-pattern-rollout.md),
  [memory grounding rollout](rollouts/memory-grounding-rollout.md),
  [memory quality second pass](rollouts/memory-quality-second-pass.md).

Keep dated evidence and its limitations when recording a review. Track remaining
work in the owning backlog entry; when it ships, update the current subsystem
guide and mark the backlog item accordingly. Avoid a new current-behavior guide
for each implementation pass.
