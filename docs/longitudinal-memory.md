# Longitudinal memory evidence

This implements the four follow-ups in [memory-topic-correctness.md](memory-topic-correctness.md).
These records enrich the basis of an interpretation; they are not confidence votes.

## Topic evidence profiles

`topic_evidence_profile` is a bounded read model over current eligible topic members.
It separates note count, recorded observation groups, actor/source-role mix, unknown
lineage, questions, recording dates, observed event dates and planned dates. A group
is recorded source separation, not proof of independence, truth or recurrence.
Plans and Aiko's reflections do not supply observed event recurrence. Missing and
truncated inputs remain visible. Question resolution is explicitly not assessed
until a real resolution receipt exists.

Profiles are exposed in topic-graph snapshots and appended to the actual focus
packets in concept synthesis, including reconsideration. They are context, not
additional citeable evidence, and do not change promotion gates or confidence.
No model call or live-data rewrite is needed to compute them.
