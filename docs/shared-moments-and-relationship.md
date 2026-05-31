# Shared moments & relationship depth

This doc covers the v7 schema bump — the `shared_moment` episodic memory
kind, the four-axis `relationship_axes` store, the anniversary surfacing
path, and the new "Together" UI tab.

## Why

Before v7 the relationship surface was flat:

- `RelationshipTracker` only tracked **phase + total turns + a handful
  of calendar milestones**.
- `relationship_pulse` wrote one `self_tagged` memory per week.
- Episodic memory had no `(when, what, vibe)` shape — `event` /
  `callback` / `reflection` rows were loosely episodic but
  unstructured.

The plan was to give Aiko **structured episodic memory** and a
**multi-dimensional relationship state** so that the prompt can surface
"a month ago today …" naturally and Aiko's tone can drift slowly with
the relationship instead of snapping between hard-coded phases.

## Schema (v6 → v7)

`app/core/infra/chat_database.py` bumps `_SCHEMA_VERSION` from `6` to `7` and
applies two changes:

1. **`memories.metadata TEXT` column** — a nullable JSON blob. Today
   it carries the shared-moment structured payload
   (`when`, `what`, `vibe`, `participants`, `source_message_ids`,
   `last_anniversaried_at`) but the column is intentionally generic so
   future structured kinds can ride the same column without another
   schema bump.
2. **`relationship_axes` table** — one row per user keyed by `user_id`
   with four `REAL` columns (`closeness`, `humor`, `trust`, `comfort`)
   and an `updated_at` ISO timestamp.

Old v6 databases get both via `ALTER TABLE memories ADD COLUMN
metadata TEXT` and a `CREATE TABLE IF NOT EXISTS` for the axes table.
Existing rows survive intact (their `metadata` defaults to `NULL`).

## Detection (three tracks)

Detection is **belt-and-braces** so we get coverage without spamming
LLM calls:

- **Track 1 — inline tag.** Aiko emits `[[moment:vibe:short summary]]`
  inline during a reply (max one per turn). The reply stripper hides
  the tag from the user-visible transcript. `extract_inline_tags` in
  `app/core/relationship/shared_moment_extractor.py` pulls every match.
  Vibe is normalised onto a closed vocabulary
  (`VIBE_VOCABULARY`); unknown labels collapse to `general`.

- **Track 2 — speaking-window LLM detector.** Mirrors
  `PromiseExtractor`: `MomentDetector.should_run_llm` gates each call
  on `min_turn_gap` user turns + `cooldown_seconds` wall-clock + at
  least **one** of: a moment-grade reaction tag
  (`[[reaction:laugh|tender|love|awe|surprise|joy|proud|vulnerable|…]]`),
  a milestone crossed this turn, a promise transitioning to `kept`, or
  a gift handed to Aiko via the World. The LLM is asked to return one
  JSON object describing the moment, or `null` when nothing genuinely
  worth remembering happened. Strict-by-default; the detector is
  designed to **miss** small moments rather than over-tag.

- **Track 3 — manual "Mark as moment".** A hover action on chat
  message bubbles in `ChatView.tsx` calls
  `POST /api/chat/messages/{id}/mark-moment` with a vibe. These are
  auto-pinned (user-curated > AI-curated).

## Storage (`SharedMomentsStore`)

`app/core/relationship/shared_moments.py` is a thin lens over `MemoryStore` that
serialises / deserialises the structured metadata. CRUD surface used
by `SessionController` + the REST layer:

- `add(summary, vibe, ..., pinned=False)` / `add_from_candidate(cand)`
- `update(moment_id, **fields)` — including `pinned`
- `stamp_anniversary(moment_id, when_iso)` — merge-only patch on
  `metadata.last_anniversaried_at`
- `delete(moment_id)`
- `list(offset, limit, vibe=None) -> (rows, total)`
- `iter_all()`

## Anniversary surfacing

`app/core/relationship/anniversary.py` is pure functions. `pick_anniversary` walks
the list of `SharedMomentRow`s and matches against calendar windows
(`1 month`, `3 months`, `6 months`, `1 year`, then yearly) with a
±1-day tolerance. Tie-breakers, in order:

1. **Window precedence** — longer windows first (`1y` beats `1mo`).
2. **Pinned > unpinned**.
3. **Newer `when` first** (within the same window).
4. **Higher salience**.

A 6-hour `last_anniversaried_at` rate-limit prevents the same moment
from firing the anniversary block on every turn during a long chat.

`SessionController._render_anniversary_block` produces a terse prompt
line — `"On your mind today — a month ago today: …"` — placed right
after `relationship_block` in `PromptAssembler` and dropped in
`aggressive` mode.

Small bonus: `RagRetriever` adds `+0.05` to a shared-moment row's
score when its `metadata.when` is hitting an anniversary today, so
the standard memory block gravitates toward the same row the
anniversary block called out.

## Relationship axes

`app/core/relationship/relationship_axes.py` defines:

- `RelationshipAxesState` — `{user_id, closeness, humor, trust,
  comfort, updated_at}`, all four axes clamped to `[-1, 1]`.
- `RelationshipAxesStore` — SQLite CRUD with **decay-on-read**
  (exponential, ~30-day half-life, only applied past a 60-second
  staleness threshold so we don't decay on every tick).
- `RelationshipAxesUpdater.apply_turn(...)` — cheap, no LLM. Combines
  reaction-tag deltas (`_DELTAS_REACTION`), moment-vibe deltas
  (`_DELTAS_MOMENT_VIBE`), milestone bumps, gift / promise-kept
  flags, and a tiny user-text keyword hint. Every per-axis delta is
  capped at `±0.08` per turn so no single bursty turn can pin an
  axis at saturation.

Rendering: `render_axes_block` returns `""` unless at least one axis
crosses `±0.5`. When it does, the renderer surfaces **at most the top
two** axes, never the full dashboard, so the LLM doesn't start
narrating numbers.

## "Together" UI tab

Lives inside `SettingsDrawer.tsx`. Shows:

- **Header** — phase chip, days known, total turns, total sessions.
- **Anniversary card** — highlighted when `summary.anniversary_today`
  is set, with vibe + window label.
- **Axes bars** — four horizontal bars, live-updated from a debounced
  `relationship_axes_updated` WebSocket event.
- **Milestones** — vertical list with crossed-off dates.
- **Moments timeline** — paginated list (page size 20). Each card is
  a date pill + vibe pill + summary. Row actions: edit, delete,
  pin / unpin. Vibe filter dropdown above the list. "+ Add manually"
  opens an inline form for a manual moment.

Backed by `togetherView` slice in `web/src/store.ts` and the
`/api/together` + `/api/shared-moments` + `/api/chat/messages/{id}/mark-moment`
REST surface in `app/web/server.py`.

## Privacy + cost posture

- **Track 2 LLM cost gate.** The Track-2 detector is *off* by default
  unless a moment-worthy signal fires AND cadence/cooldown both
  allow. In a casual chat session it should fire **zero** LLM calls.
  Per-turn stats expose `llm_scheduled`, `llm_completed`,
  `llm_returned_null`, `llm_skipped_throttled`,
  `llm_skipped_no_signal` so any regression in gating is observable.
- **Persona never recites the system line.** The persona file
  explicitly tells Aiko to bring up an anniversary naturally if it
  fits, never to force a "remember when" or quote the system line.
- **All disabling switches live in settings.** Disabling
  `shared_moments_enabled` cuts inline tag persistence and the
  Track-2 detector; disabling `shared_moments_llm_enabled` keeps
  the inline-tag path but skips the LLM call;
  `anniversary_surfacing_enabled` mutes the prompt block;
  `relationship_axes_enabled` mutes the axes block and updater.

## Disabling switches (`AgentSettings`)

| key                            | default | effect |
|--------------------------------|---------|--------|
| `shared_moments_enabled`       | `true`  | master switch for moment persistence and the Together tab |
| `shared_moments_llm_enabled`   | `true`  | enables the Track-2 LLM detector specifically |
| `shared_moments_min_turn_gap`  | `5`     | minimum user turns between Track-2 runs |
| `shared_moments_cooldown_seconds` | `300` | wall-clock cooldown between Track-2 runs |
| `anniversary_surfacing_enabled`| `true`  | turns the anniversary inner-life block on/off |
| `relationship_axes_enabled`    | `true`  | enables both axes updater and prompt block |

## Out-of-scope follow-ups

- **Multi-user moments / participant attribution beyond Jacob.**
  Single-user is fine for v1; everything keys on `user_id = "jacob"`.
- **Exportable timeline (Markdown / PDF).** Adding now would balloon
  the UI work; deferred.
- **Axes-aware proactive nudges.** The axes are read-only into the
  prompt for v1; consuming them in `ProactiveDirector` is a clean
  follow-up.
- **Multi-room / outdoor scene linkage** for moments — out of scope
  for the world feature already, and stays out of scope here.

## Where to look next

- Schema: `app/core/infra/chat_database.py`
- Memory layer: `app/core/memory/memory_store.py`, `app/core/relationship/shared_moments.py`
- Detection: `app/core/relationship/shared_moment_extractor.py`
- Axes: `app/core/relationship/relationship_axes.py`
- Anniversary: `app/core/relationship/anniversary.py`
- Prompt assembly: `app/core/session/prompt_assembler.py` (`_anniversary_provider`, `_axes_provider`)
- Session wiring: `app/core/session/session_controller.py`
  (`_post_turn_inner_life`, `_maybe_schedule_moment_llm_job`,
   `_render_anniversary_block`, `_render_axes_block`)
- REST + WS: `app/web/server.py` (`/api/together`, `/api/shared-moments`,
  `/api/chat/messages/{id}/mark-moment`, broadcasts)
- Frontend types + store: `web/src/types.ts`, `web/src/store.ts`,
  `web/src/api.ts`, `web/src/hooks/useAssistantSocket.ts`
- UI: `web/src/components/SettingsDrawer.tsx` (Together tab),
  `web/src/components/ChatView.tsx` (mark-as-moment hover action)
- Persona: `data/persona/aiko_companion.txt`
- Tests: `tests/test_memory_store_metadata.py`,
  `tests/test_shared_moment_extractor.py`,
  `tests/test_shared_moment_detector_llm.py`,
  `tests/test_relationship_axes.py`,
  `tests/test_anniversary_provider.py`,
  `tests/test_web_server_together.py`,
  `web/src/store.together.test.ts`,
  `web/src/components/TogetherTab.test.tsx`
