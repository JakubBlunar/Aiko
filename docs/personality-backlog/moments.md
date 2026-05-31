# Shared-moments follow-ups

Promoted from the shared-moments + relationship-axes shipped entry
(see [`shipped.md`](shipped.md)). All three items below are deferred
follow-ups, not new work.

---

## J1. Multi-user moments / participant attribution

Today every moment is keyed implicitly to Jacob. A future extension
would attribute moments to multiple participants (`participants:
[user_id, ...]` already exists in the metadata shape but is never
read) so a multi-user setup (Jacob + a partner, or a family
deployment) can have separate timelines. Key files:
[`app/core/relationship/shared_moments.py`](../../app/core/relationship/shared_moments.py),
[`app/web/server.py`](../../app/web/server.py) `/api/together` filter,
Together tab UI.

---

## J2. Exportable timeline

Markdown or PDF export of the moments timeline so Jacob has a
keepsake of the relationship arc he can read outside the app. Key
files: new `app/core/shared_moments_export.py`,
[`app/web/server.py`](../../app/web/server.py) (new
`GET /api/together/export?format=md|pdf`), Together tab UI (export
button).

---

## J3. Axes-aware proactive nudges

The relationship axes are read-only into the prompt today. A clean
follow-up is letting `ProactiveDirector` consume them — e.g.
`comfort < -0.3` -> bias the next nudge toward checking in on Jacob
rather than picking up a thread. Don't let the axes *trigger* a nudge
on their own (would feel like surveillance); just colour the topic
selection when a nudge fires for other reasons. Key files:
[`app/core/proactive/proactive_director.py`](../../app/core/proactive/proactive_director.py)
`_pick_topic`, [`app/core/relationship/relationship_axes.py`](../../app/core/relationship/relationship_axes.py).
