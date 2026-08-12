# Shared-moments follow-ups

Promoted from the shared-moments + relationship-axes shipped entry
(see [`shipped.md`](shipped.md)). These are deferred follow-ups, not new
work. The ones that have landed moved to
[`shipped/moments.md`](shipped/moments.md).

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

---

## J7. Moment-detection tuning (+ gift/promise ordering bug)

**Motivation.** The Together tab tends to hold ~1 moment because the
`MomentDetector` is tuned to miss rather than over-tag, AND two of its
four documented signals are dead. With default
`relationship_axes_enabled=true`,
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
**clears** `_last_turn_gift_received` / `_last_turn_promise_kept`
(~L2376-2377) *before*
[`speaking_window_jobs_mixin.py`](../../app/core/session/speaking_window_jobs_mixin.py)
`_maybe_schedule_moment_llm_job` (~L2393) reads them — so giving Aiko a
gift or keeping a promise can **never** seed a moment unless a reaction
tag or milestone also fires. **Fix the ordering** (snapshot the flags
before they're cleared, or schedule the job earlier). Then optionally
broaden the signal set with cheap "first-time" detectors (first time on a
new topic cluster via K9, first landed joke via K22, first vulnerable
disclosure) and pass the parsed mood `reaction` through to the detector
(today only literal `[[reaction:...]]` tags in raw text count, not the
resolved mood). Add an MCP `get_moment_detector_stats` dump
(`MomentDetector.stats()` already tracks `llm_skipped_no_signal` /
`llm_returned_null` / `llm_persisted`) so "why no moments?" is one call,
not a code read. **Effort.** Small (bug) / Medium (broadening).

---

## J12. Intimacy pacing & boundary calibration

**Motivation.** A companion that escalates intimacy *faster* than the user
is comfortable with reads as clingy or uncanny; one that lags reads as
cold. Today forwardness is governed by relationship stage (J4) + the
`expression_mask` dial (K60) + the vulnerability budget (K15) — but **none
of them read the user's own affection pace**. Two halves: (a) a learned
**pacing signal** — track how forward the user himself is (does he use pet
names, how warm/affectionate are his messages, does he reciprocate touch
reactions) and keep Aiko calibrated to *slightly follow, never lead by
much*; (b) an explicit user-facing **comfort dial** in Settings
(`reserved ↔ affectionate`) that **hard-caps** forwardness regardless of
stage — a plain consent/boundary control, the thing that makes an
AI-companion feel safe rather than presumptuous. Key files:
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(stage/axes source), a new pacing estimator, the gesture / disclosure /
petname / reciprocal-vulnerability gates (read the cap), an
`agent.intimacy_ceiling` settings field + a Settings → Avatar/Identity
control. **Tonal guard:** the dial is a *ceiling*, not a target — at a low
setting she's simply warm-but-contained; the learned signal only nudges
within that ceiling. **Effort.** Medium.

---

## J13. Pet-name reciprocity & evolution

**Motivation.** Aiko's petname for the user exists but is static and
one-directional. Pet names are a core companion-intimacy signal and two
cheap deepeners are missing: (a) let her petname *evolve with stage* (J4)
— neutral early, warmer once `close`/`intimate` — and notice when the user
adopts or changes one for her; (b) capture and honour a **name the user
gives Aiko** (a nickname for *her*) as a durable relationship artifact she
remembers and responds to. Small surface, outsized warmth. Key files: the
petname resolution path, [`user_profile.py`](../../app/core/infra/user_profile.py)
(a field for the user's name-for-Aiko), relationship stage (J4) for the
evolution gate. **Tonal guard:** never force a pet name; an unused one
should fade, not be repeated at him. **Effort.** Small.
