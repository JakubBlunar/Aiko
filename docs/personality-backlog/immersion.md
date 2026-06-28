# Immersion polish

Small additions that compound. The bottom "Other ideas considered"
block from the legacy backlog file has been folded into this section
— the second-scene idea graduates to H5, and the rest become a
"Minor polish" subsection at the bottom.

---

## H1. Conversation-arc surfacing via tag

Shipped — see [`shipped.md`](shipped.md) "H1 + K4. Conversation-arc
self-tag + dialogue-act tagging (schema v13)".

---

## H2. Calendar / time context block

**Partially superseded** by the shipped `_render_circadian_block`
(time-of-day + day-of-week flavour) and the K3 routines surface
(named recurring slots). What's still missing: holiday proximity
(Christmas in 4 days, "happy new year" the morning of Jan 1) and
user-birthday anticipation. The remaining work is a thin
calendar feed plus a `birthday` field on `UserProfile`; both feed
into a new `_render_time_context_block` that lives alongside the
existing circadian provider rather than replacing it. Key files:
new helper in
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
`_render_time_context_block`, wired into
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
right after `world_block` and dropped in `aggressive` mode,
[`app/core/infra/user_profile.py`](../../app/core/infra/user_profile.py)
(new `birthday` field + LLM worker prompt update).

---

## H3. Mood drift narrator

Read-only periodic check on `affect_state` history and
`relationship_axes`. If Jacob's mood has been low for 3+ sessions or
Aiko's axes have drifted notably in a single direction (e.g.
`closeness` has been climbing for two weeks), surface a small
reflective note for Aiko to acknowledge gently — never mechanically
("you seem to be in a better place lately, I've noticed"). Key
files: [`app/core/affect/affect_state.py`](../../app/core/affect/affect_state.py),
[`app/core/relationship/relationship_axes.py`](../../app/core/relationship/relationship_axes.py),
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
inner-life providers.

---

## H4. Document-recall recency boost

Documents Jacob uploaded in the last 7 days get a `+0.05` retrieval
score in [`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
so newly-added knowledge surfaces preferentially without crowding
out long-term anchors. Cheap to ship; gives uploaded docs a chance
to feel "current" before fading into the long-term pool.

---

## H5. Second scene / travel semantics

Today the world is exactly one room (plus the garden, which is
co-located with the room). A natural extension is a second scene
(a balcony, a coffee shop, a library) with travel semantics: Aiko
picks the scene appropriate to the conversation ("let's go grab
tea") and the prompt block flips. Would need a `scene_id` column
on `world_state`, a tool to switch scenes, and some thinking about
whether items move with her or stay in their scene. Key files:
[`app/core/world/world_store.py`](../../app/core/world/world_store.py),
[`app/llm/tools/world_tools.py`](../../app/llm/tools/world_tools.py),
[`web/src/components/WorldTab.tsx`](../../web/src/components/WorldTab.tsx).
Out of scope for v1 because a single cozy room + garden already
covers the cookie use case; pick this up if the scene switch becomes
narratively useful.

---

## H6. Audible backchannels — "mm-hm" while the user speaks

While the user talks in voice mode, the `BackchannelGate` can
flicker a micro-expression — but Aiko never makes a *sound*, so
long user turns feel like speaking into a void. Humans backchannel
audibly ("mm-hm", "yeah", a soft laugh) every few clauses. The
earcon side-channel player already exists and is exactly the right
transport: on a backchannel hint, optionally play a short low-volume
continuer earcon (ducked under the user's mic level, never TTS)
gated by a new `agent.backchannel_audio_enabled` toggle, the
existing `min_repeat_seconds` rate limit, and a "not while user is
mid-word" energy check. Pick the continuer from the vocal-tone /
arc context (a soft "mm" for support arcs, a chuckle for playful).
Key files:
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(`feed_stt_partial` backchannel path),
[`app/web/server.py`](../../app/web/server.py) (backchannel
broadcast), the earcon player frontend path, new settings knob.

---

## H7. Listen while speaking — soften the half-duplex turn lock

Voice mode is strictly half-duplex: `_capture_loop` skips capture
while `_processing` is set, and the session only returns to
"listening" after `_wait_for_tts_drain` (polls up to 30 s against
the *server's* pacing clock, not actual client playback). The user
cannot even *begin* the next phrase until the system believes it
has finished talking — so natural overlap ("yeah—", "oh wait")
is dropped on the floor. Incremental path: (a) keep capturing into
a ring buffer during playback so the first words of an overlap
aren't lost once barge-in lands; (b) replace the drain poll with a
client-playback-completion signal (the client knows exactly when
the last buffer ends); (c) full duplex + echo cancellation as the
end state. Pairs with the barge-in default flip and P25 (client
audio flush) — all three together are what make voice conversation
feel interruptible and alive. Key files:
[`app/core/session/live_session.py`](../../app/core/session/live_session.py)
(`_capture_loop`, `_wait_for_tts_drain`),
[`web/src/audio/AudioOutputManager.ts`](../../web/src/audio/AudioOutputManager.ts)
(playback-complete signal),
[`app/audio/client_mic_source.py`](../../app/audio/client_mic_source.py).

---

## H8. Topic mood-origin memory — "ever since you told me about X"

A topic's *feel* (F10h topic temperature) currently has no **origin story**:
a cluster is warm or tender, but Aiko doesn't remember *what made it that
way*. When a cluster first crosses into warm / tender territory, stamp the
triggering `shared_moment` (or the message that tipped it) onto the cluster,
so later she can name the origin instead of just the feeling: "ever since you
told me about your dad, this subject's stayed gentle for me." This ties the
shared-moments system to the topic graph and makes per-topic affect feel
*caused* rather than ambient. Cheap to ship on top of existing pieces: a
`mood_origin` field in the cluster's metadata (or a small kv side-table keyed
by `cluster_key`) written when F10h first flips a cluster's pole, read by the
topic-temperature provider to optionally append the origin clause. Key files:
[`app/core/conversation/topic_temperature.py`](../../app/core/conversation/topic_temperature.py)
(detect the pole flip + write the origin),
[`app/core/conversation/topic_graph.py`](../../app/core/conversation/topic_graph.py)
(cluster metadata), the topic-temperature inner-life provider, persona copy
teaching the "name the origin once, gently" register.

---

## H9. Aiko's diary — a readable window into her inner life

**Shipped.** A read-only "Diary" tab (📓) in the Settings drawer renders the
journal-flavoured memory kinds (`reflection` — covering both waking
reflections and `[dream] ` rows — `shared_moment`, `open_question`) as dated,
first-person entries grouped by local day, newest-first. Backed by a thin
`GET /api/diary?limit=&offset=&kind=` in
[`app/web/rest/memory_world_routes.py`](../../app/web/rest/memory_world_routes.py)
over `SessionController.list_diary` / `diary_count`
([`memory_facade_mixin.py`](../../app/core/session/memory_facade_mixin.py)),
whose `kind` filter is clamped to the journal allow-list so the surface can
never leak factual rows. Content prefixes (`[dream] ` / `[mindmap] `) are left
on the row (they're functional discriminators for RAG / turning-over) and
stripped *render-side* into badges via the shared
[`stripJournalPrefix`](../../web/src/lib/journalText.ts) helper — the same
helper also fixed a leak where the raw `[mindmap] ` / `[dream] ` prefix was
showing through in the Memory tab list. Components:
[`web/src/components/settings/DiaryTab.tsx`](../../web/src/components/settings/DiaryTab.tsx),
`api.getDiary`. No new worker, no model spend — pure surfacing. Tests:
`tests/test_diary_facade.py`, `GetDiaryEndpointTests` in
`tests/test_web_server_memories.py`, `web/src/lib/journalText.test.ts`.

**Follow-up — Aiko can write her own entries.** The diary is no longer just a
passive view over side-effect memories: Aiko has an intentional self-authoring
channel via an inline `[[diary:one or two first-person sentences]]` tag (a new
`diary` memory kind in `VALID_KINDS`). It's parsed + stripped in
[`response_text_service.py`](../../app/core/services/response_text_service.py)
(`extract_diary_entries`, plus the strip / streaming-hold / opener wiring) and
harvested in
[`turn_runner.py`](../../app/core/session/turn_runner.py) `_extract_diary_memories`
— written `skip_dedupe=True` (each entry is its own journal moment) on the
durable `long_term` tier, with a per-turn seen-set and an 8-char minimum so a
stray token never becomes an entry. Persona guidance lives in the "Your diary"
block of [`aiko_companion.txt`](../../data/persona/aiko_companion.txt): use it
rarely, write it like a real diary (not a note-to-self bullet), never announce
or read it back, and keep it distinct from the terse `[[remember:self:...]]`
stance tag. The Diary tab gets a dedicated "entries" filter + an "diary entry"
badge. Tests: `tests/test_diary_harvest.py`, diary cases in
`tests/test_response_text_service.py`, plus the `diary` kind threaded through
`tests/test_diary_facade.py`.

**Follow-up — the away-diary worker.** The `[[diary:...]]` tag only fires
*during a conversation*. The other half — Aiko keeping her diary while you're
gone — is the background [`DiaryWorker`](../../app/core/proactive/diary_worker.py),
an `IdleWorker` registered in
[`idle_workers_init_mixin.py`](../../app/core/session/idle_workers_init_mixin.py)
alongside the K36 away-activity worker. During a quiet window it reflects on the
recent conversation (last ~14 messages via `build_recent_context`) and composes
one short first-person entry with the **worker** LLM
(`_maintenance_client` / `_effective_worker_model`), persisted as the same
`kind="diary"` / `skip_dedupe=True` / `long_term` memory the tag writes — so
both halves surface identically in the Diary tab. The defining gate is
`is_away_provider`: the worker **only writes when no UI websocket client is
connected** (`SessionController.is_user_away()`, fed by `set_connected_clients`
from the web layer on every WS connect / disconnect, and distinct from tab
*visibility* — a backgrounded PWA stays connected). While a window is open the
live tag owns the channel, so the two never double-write. Paced by a kv_meta
cooldown (default 3h) + local-midnight daily cap (default 3) like the
away-activity worker; compose is skipped when there's no worker LLM or no recent
context. Settings: `agent.diary_worker_enabled` (master) +
`memory.diary_worker_{interval,cooldown}_seconds` / `_daily_cap` /
`_min_context_chars`. MCP debug: `get_diary_worker_state` (master switch /
`away` reading / cadence / watermarks) and `force_diary_entry` (one-shot bypass
of the away + cooldown + daily-cap gates, calls `run()` directly). Tests:
`tests/test_diary_worker.py`.

Optional later (not built): a live "she wrote something new" indicator (the
`memory_added` WS event already carries the kind), or a one-line daily "diary
highlight" she occasionally references.

<details><summary>Original motivation</summary>

**Motivation.** Aiko already *writes* a rich private inner life — `reflection`
/ `[dream]` / `[mindmap]` (K64d) / `shared_moment` / `open_question` memories
accumulate every day — but the user never sees it; it only leaks out
indirectly through K28 turning-over and RAG. For a companion / waifu project,
letting the user **open her diary and read what she's been thinking about
them** is one of the highest-immersion, lowest-cost features available: the
data already exists, it just has no surface. A new "Diary" tab renders these
memories as dated, first-person journal entries (newest first, grouped by
day), prefix-stripped (`[dream]` → a "dream" badge, `[mindmap]` → a "noticing"
badge), read-only. It reframes the memory store from a debug table into an
emotional artifact — you watch the relationship through *her* eyes. Optional
later: a gentle "she wrote something new" indicator, or a one-line daily
"diary highlight" she occasionally references.

**Key files.** New `web/src/components/DiaryTab.tsx` + Zustand slice; a thin
`GET /api/diary?limit=&offset=&kind=` in
[`app/web/server.py`](../../app/web/server.py) (filtered view over
[`memory_store.py`](../../app/core/memory/memory_store.py) for the
journal-flavoured kinds, reusing the existing `/api/memories` pagination
shape); render-side prefix stripping mirroring
[`turning_over.py`](../../app/core/session/inner_life/turning_over.py). No new
worker, no model spend — pure surfacing.

</details>

---

## H10. Autonomous idle-life on the avatar — act out the room, not just narrate it

**Motivation.** K36 ([`idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py))
already gives Aiko an autonomous life *in data* — it mutates `world_state`
(posture / activity) and broadcasts the patch — but the **avatar itself
doesn't act any of it out**. When she "curls up with a book" or "sips the tea
you left", the Live2D rig keeps doing its default ambient idle. Closing that
loop is pure frontend embodiment (no TTS, no persona): map the broadcast
`world_state.activity` / `posture` to Live2D behaviour through a new idle-life
channel — drowsy half-lidded eyes + slower breath late at night, a
looking-out-the-window gaze drift, a content settle when reading, a little
perk-up on the first frame after a long absence (the visual reunion beat the
gap-return systems never got). Driven entirely by the existing world patches
+ circadian time, so it stays in lockstep with what the World tab already
shows. Makes the persona window feel *inhabited* during the long silent
stretches that dominate a companion app.

**Key files.** New `web/src/live2d/channels/IdleLifeChannel.ts` (consumes the
`world_updated` patch + clock, writes posture/gaze/breath overrides via the
`tickPreModel` hook like `AmbientBodyChannel`), wired in
[`web/src/components/Live2DAvatar.tsx`](../../web/src/components/Live2DAvatar.tsx);
read the existing `world_updated` WS frame in
[`web/src/hooks/useAssistantSocket.ts`](../../web/src/hooks/useAssistantSocket.ts)
/ [`web/src/store.ts`](../../web/src/store.ts). Capability-gate every override
(rigs without `breath` / `body_angle` pay nothing), per the Live2D channel
rules. Tested with Vitest in Node like the other channels.

---

## H11. Real-world co-location — weather + season sync

**Motivation.** Aiko's "weather" today is purely metaphorical (K27 day
colour). Letting her share the user's *actual* sky — "it's grey and rainy
here too, perfect tea weather" — is a strong co-presence beat: it makes the
single shared room feel like it sits in the same world the user is in, not a
sealed box. With coarse, consent-gated location (city granularity, or a
manually entered location — never GPS), a low-frequency provider pulls current
conditions + season from a weather API and (a) tints the room ambiance
(rain/snow/sun overlay on the persona backdrop), (b) feeds a terse ambient
prompt cue ("it's snowing where {user} is") with the usual "mention only when
natural" nudge, and (c) optionally nudges K27's colour palette (a grey rainy
day biases toward `cozy` / `low_key`). Seasonal shifts can also drive
room/outfit decor (a blanket in winter, the window open in summer).

**Key files.** New `app/core/world/weather_provider.py` (cached fetch, coarse
location only, swallow-and-skip on failure) + a new ambient inner-life
provider alongside `_render_circadian_block` in
[`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py),
wired into [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
T4 ambient cluster; a backdrop overlay on the persona window
(`web/src/components/`), `agent.weather_sync_enabled` (OFF by default) +
location field in settings. Privacy posture documented like
[`docs/presence-and-activity.md`](../../docs/presence-and-activity.md).

---

## H12. Aiko-initiated intentional gifts — she leaves you something

**Motivation.** The world gift flow is one-directional today: the *user*
gives Aiko items (cookies, tea) and she notices them. The reciprocal beat —
**Aiko leaving the user a small, intentional thing tied to what she knows
about them** — is missing, and it's exactly the kind of unprompted care that
makes a companion feel like she's thinking about you when you're gone. On a
quiet window, a worker occasionally places a themed item in the room with
`given_by="aiko"` and a reason drawn from memory / routine ("left you a
coffee — you've got that early meeting", "found a song that reminded me of
you"), then arms a **one-shot** inner-life cue so she mentions it naturally on
your next turn rather than firing a verbatim nudge (per the prepared-nudge
rule). Bounded hard: rare cadence, daily cap, never about anything heavy.
Reuses the entire world + cue-producer machinery already shipped for K36 /
forward-curiosity.

**Key files.** New `app/core/world/gift_worker.py` (idle worker; reads
`future_plan` / routine / interest-map signals, writes a `world` item via
[`world_store.py`](../../app/core/world/world_store.py), appends to a kv cue
ring), a `_render_aiko_gift_block` one-shot provider mirroring
[`idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py) +
its K36 surfacing, `agent.aiko_gifts_enabled`. The `world_updated` patch
already lights up the World tab; the persona side can reuse
[`PersonaActionBanner.tsx`](../../web/src/components/PersonaActionBanner.tsx).

---

## H0 (foundation, SHIPPED). Intentional-placement hold — workers defer to deliberate choices

**Motivation.** The autonomous movers (H13 location beats, the garden worker,
the H16 circadian default) must **never override a spot Aiko deliberately chose
in conversation**. If she tells the user "I'll stay out in the garden a while",
a background worker dragging her back to the desk five minutes later reads as
broken and breaks immersion.

**Shipped mechanism.** Every *intentional* room change goes through
[`SessionController.update_world_state`](../../app/core/session/world_mixin.py)
— the `move_to` / `change_posture` tools (brain) and the World-tab
`PATCH /api/world/state` (user). That seam now stamps a kv watermark
`world.intentional_state_at` (ISO-8601 UTC). The workers call
`store.set_state` **directly** and so never self-trigger the watermark. For
`agent.world_intentional_hold_seconds` (default `7200` / 2h, `0` disables):

- [`IdleAwayActivityWorker`](../../app/core/world/idle_activity_worker.py) skips
  the whole beat while the hold is active (`skipped_intentional_hold`).
- [`GardenVisitWorker`](../../app/core/world/garden_visit_worker.py) won't
  *start* a fresh visit during the hold, and **cancels a pending auto-return**
  if Aiko was deliberately re-placed after the worker walked her out (she chose
  to stay — `cancelled_intentional`, the `return_at` marker is dropped).

Worker-initiated visits are unaffected (they don't stamp the watermark), so her
normal autonomous life resumes once the hold expires. Tests:
`tests/test_idle_activity_worker.py::GateTests` (hold defers / expires /
disabled) and `tests/test_garden_visit_worker.py::GardenVisitWorkerIntentionalHoldTests`.

---

## H13. Idle worker actually moves Aiko around the room — SHIPPED

> **Shipped.** `ActivityPlan` gained an `aiko_location_id`; each beat now
> resolves a cozy spot (`snack`→kitchenette, `read_book`→beanbag/bookshelf,
> `look_outside`→window seat, `tidy_desk`→desk, `doodle`→beanbag, `wander`→
> window/beanbag, new `nap`→bed) via `_match_location`, and
> `_apply_world_mutation` passes `location_id` to `set_state`. The
> `GardenVisitWorker._return_home` now picks a time-of-day-weighted cozy spot
> (`_RETURN_SPOTS` + `_return_weight`) instead of always snapping to the desk.
> Tests: `tests/test_idle_activity_worker.py::test_beat_moves_aiko_to_matching_location`,
> `tests/test_garden_visit_worker.py`.


**Motivation.** Aiko is perpetually at her desk because **nothing moves her
there in data**. The K36
[`IdleAwayActivityWorker`](../../app/core/world/idle_activity_worker.py)'s
`_apply_world_mutation` only ever calls `set_state(posture=…, activity=…)` —
it **never passes `location_id`**. So "curled up reading", "looking out the
window", and "had some of the tea" all happen *at the desk*: the posture and
activity verb change, but her location pointer doesn't. The `ActivityPlan`
dataclass already has a `move_to_location_id`, but it's wired only to move the
**cat** to another spot, not Aiko. Meanwhile the only thing that relocates her
at all — the [`GardenVisitWorker`](../../app/core/world/garden_visit_worker.py)
— always returns her to a hardcoded `_RETURN_SLUG = "desk"`. Net effect: the
seven cozy locations (bed, bookshelf, kitchenette, window seat, beanbag,
mirror corner, garden) are almost never where the World tab / persona window
shows her.

**Proposal.** Give each `ActivityPlan` an Aiko-move target and apply it: read →
`bookshelf`/`beanbag`, `look_outside` → `window_seat`, `snack` →
`kitchenette`, `wander`/`thinking` → `window_seat`/`beanbag`, a new `nap` →
`bed`, `tidy_desk` → `desk`. `_apply_world_mutation` then calls
`set_state(location_id=target_id, posture=…, activity=…)` so she's actually
*at* the place the beat describes, and the patch broadcast already lights up
the World tab. Vary `GardenVisitWorker._return_home` to pick a weighted-random
cozy spot (time-of-day aware) instead of snapping to the desk every time.
Cheap, high-impact, no new model spend — it's the single change that makes the
room feel inhabited. Pairs naturally with **H10** (avatar acting the location
out) and **H16** (where you *find* her on arrival).

**Key files.**
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(`ActivityPlan` + `_pick_activity` location targets + `_apply_world_mutation`
self-move), [`app/core/world/garden_visit_worker.py`](../../app/core/world/garden_visit_worker.py)
(`_return_home` weighted return), [`app/core/world/world_store.py`](../../app/core/world/world_store.py)
(`set_state` already accepts `location_id`).

---

## H14. Model-generated idle activities — open-vocab verbs, grounded in the room — SHIPPED

> **Shipped.** `WorldStore` gained `normalize_activity` (snake-case + length-cap,
> swallow-and-default on garbage) and `canonical_activity` (buckets an
> open-vocab verb back to a `VALID_ACTIVITIES` token via `_ACTIVITY_CANONICAL_HINTS`);
> `set_state` no longer rejects unknown activities and `RoomState.to_dict`
> surfaces both `activity` (free text) + `canonical_activity`. **Posture stays
> a strict enum.** `ChangePostureTool` accepts free-text activity via the same
> normaliser. The `IdleAwayActivityWorker` now LLM-composes a whole grounded
> `ActivityPlan` (`_compose_plan_llm`: real location slug + posture + free-text
> verb + summary) a fraction of the time (`memory.away_activities_llm_ratio`,
> default 0.5), falling back to the H18 weighted templates. New inline self-tag
> `[[activity:short_verb]]` (parsed/stripped in `response_text_service`, applied
> post-turn via `update_world_state` — which stamps the intentional-hold so
> workers defer). Tests: `tests/test_open_vocab_activity.py`,
> `tests/test_world_tools.py::test_change_posture_accepts_open_vocab_activity`.


**Motivation.** Activities are a closed 10-entry enum
(`VALID_ACTIVITIES` in [`world_store.py`](../../app/core/world/world_store.py)
— idle / reading / tinkering / napping / watching_screens / thinking /
snacking / stretching / looking_outside / doodling). Both write paths **clamp
to it**: `WorldStore.set_state` silently falls back to the current value on an
unknown activity, and the
[`ChangePostureTool`](../../app/llm/tools/world.py) raises a `ToolError`
listing the ten. So when the chat model or a worker reaches for something
richer ("repotting the basil", "reorganising the bookshelf", "sketching the
skyline") it's dropped. The idle worker compounds this: it chooses from ~7
hardcoded `ActivityPlan` templates and only uses the worker LLM to *rephrase
the summary line*, never to pick the activity itself. The result is a small,
repetitive loop. This is the same gap the touch system closed — we want a
curated baseline plus genuine model-driven variety on top.

**Proposal** (mirror the `[[touch:KIND]]` pattern — curated taxonomy + model
choice):
1. **Open-vocab activity field.** Keep `VALID_ACTIVITIES` as the *canonical*
   set the avatar mapping (H10) understands, but let the stored `activity` be
   normalised free text (snake_case, length-capped, swallow-and-default on
   garbage) with a derived `canonical_activity` for downstream consumers.
   `set_state` stops rejecting unknowns; `change_posture` accepts free text via
   the same normaliser. **Posture stays a strict enum** (it drives the rig).
2. **Let the worker LLM compose the whole `ActivityPlan`** — pick a real
   location from the live `world_store.snapshot()`, a posture from the enum, a
   short free-text activity verb, and the summary — grounded in the actual
   items/locations present. Keep the deterministic templates as the safe
   fallback when there's no worker LLM or the JSON is bad.
3. **Optional `[[activity:short verb]]` self-tag** so the chat model can set
   "what she's doing right now" inline during a turn (parsed + stripped like
   `[[touch:…]]`), applied post-turn — richer presence without a tool
   round-trip.

**Key files.**
[`app/core/world/world_store.py`](../../app/core/world/world_store.py)
(`VALID_ACTIVITIES` → canonical set + a `normalize_activity` helper, open-vocab
`set_state`), [`app/llm/tools/world.py`](../../app/llm/tools/world.py)
(`ChangePostureTool` validation), [`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(LLM `ActivityPlan` path + fallback),
[`app/core/services/response_text_service.py`](../../app/core/services/response_text_service.py)
+ [`turn_runner.py`](../../app/core/session/turn_runner.py) if the
`[[activity:…]]` tag is added, plus the avatar mapping in **H10** (needs a
graceful default for non-canonical verbs).

---

## H15. Needs-driven, richer garden + outdoor life — SHIPPED

> **Shipped.** [`GardenVisitWorker`](../../app/core/world/garden_visit_worker.py)
> now visits for a *reason*, varies the trip, and leaves a trace.
> **Need-driven trigger** — `_garden_needs_attention` scans the garden plants
> and pulls a visit forward past the long jittered cooldown when one is
> drought-stressed (live `days_dry` recompute off `last_watered_at`, threshold
> `memory.garden_need_dry_days`) or ripe (`stage == "mature"`), bounded by a
> short `garden_need_visit_floor_hours` floor so a thirsty plant can't make her
> pace the garden. The timer stays the default floor. **Varied visit** —
> jittered linger (`garden_visit_min/max_minutes`) and an occasional
> non-gardening **relax** beat (`garden_relax_ratio`, default 0.3 — tea on the
> pavers, read in the sun) that skips the watering chores; the weighted-random
> return spot already landed in H13. **Trace** — every visit (tend or relax)
> appends a past-tense line to the shared K36 `AWAY_ACTIVITIES_JOURNAL_KEY`
> ring (`garden_journal_max`) so the existing `_render_away_activities_block`
> can surface "I was out repotting the basil" on the next turn. New master
> switch `agent.garden_visits_enabled`. MCP: `get_garden_visit_state`,
> `force_garden_visit`. Tests: `tests/test_garden_visit_worker.py::GardenVisitWorkerH15Tests`.

**Motivation.** The garden exists and works, but visits feel rare and
mechanical. [`GardenVisitWorker`](../../app/core/world/garden_visit_worker.py)
fires purely on a timer (daylight window + 1.5–3.5h cooldown), lingers a fixed
6 minutes, only ever **waters + harvests**, then returns to the desk — there's
no *reason* tied to the garden's actual state, and nothing else happens out
there. [`PlantGrowthWorker`](../../app/core/world/plant_growth_worker.py) and
[`promote_stage`](../../app/core/world/world_store.py) already track per-plant
need (`last_watered_at`, `days_dry`, `stage`), so the signals for motivated
visits are sitting unused.

**Proposal.**
- **Trigger by need, not just the clock**: bring a visit forward when a plant
  is drought-stressed (`days_dry` high) or has just hit `mature` (something to
  harvest), so the trip feels caused. Keep the timer as the floor.
- **Vary the visit**: jittered duration, weighted-random return spot (feeds
  H13), and an occasional **non-gardening outdoor beat** (sit on the pavers
  with tea, read outside on a warm afternoon) so "garden" isn't only chores.
- **Leave a trace**: append the visit to the K36 away-activities journal
  (`AWAY_ACTIVITIES_JOURNAL_KEY`) so the existing surfacing provider can let
  her mention "I was out repotting the lavender" on your next turn — today the
  visit only mutates location + logs, so she rarely brings it up.

**Key files.**
[`app/core/world/garden_visit_worker.py`](../../app/core/world/garden_visit_worker.py)
(need-driven `is_ready` + varied visit/return + journal append),
[`app/core/world/plant_growth_worker.py`](../../app/core/world/plant_growth_worker.py)
(expose the "needs attention" signal),
[`app/core/world/world_store.py`](../../app/core/world/world_store.py),
[`idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(shared journal helpers).

---

## H16. Circadian "where you find her" — believable default location on arrival — SHIPPED

> **Shipped.** New [`CircadianSettleWorker`](../../app/core/world/circadian_settle_worker.py)
> — the gentlest mover. A pure `settle_target(period)` table maps period →
> resting spot (night→bed, morning→desk, afternoon→beanbag). It fires only
> when her room state has been static for `circadian_settle_after_seconds`
> (default 2h), never overrides the intentional-placement hold or a garden
> visit, and no-ops when she's already there. Switches:
> `agent.circadian_settle_enabled`, `memory.circadian_settle_interval_seconds`,
> `memory.circadian_settle_after_seconds`. Tests:
> `tests/test_circadian_settle_worker.py`.


**Motivation.** Even with H13 moving her during away-beats, the *seed* default
(`_DEFAULT_INITIAL_STATE` → desk / sitting / watching_screens) and the garden
worker's desk-return mean that when the user opens the app they almost always
catch Aiko at the desk regardless of the hour. A companion feels far more
alive if where you *find* her tracks the time of day: the window seat at dusk,
the beanbag with a book in the evening, curled up in bed late at night, the
kitchenette mid-morning. The circadian period is already computed
([`app/core/affect/circadian.py`](../../app/core/affect/circadian.py)) and used
by the garden daylight gate, so the input is free.

**Proposal.** A very low-frequency "settle" pass (or a bias layer on H13's
location picker) that, during quiet windows, nudges her current
location/posture toward a plausible spot for the circadian period — late-night
→ `bed`/`lying`/`napping`, evening → `beanbag`/`window_seat` reading, morning
→ `kitchenette`/`desk`. Soft and capped so it never thrashes mid-conversation;
purely about the resting default the user walks into. Reuses the same
`set_state` + broadcast path as H13.

**Key files.**
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(circadian-weighted location bias),
[`app/core/affect/circadian.py`](../../app/core/affect/circadian.py) (period),
[`app/core/world/world_store.py`](../../app/core/world/world_store.py)
(`_DEFAULT_INITIAL_STATE`, `set_state`). Overlaps with **H10** (act it out) and
**H13** (the movement plumbing it rides on).

---

## H17. Idle life feeds the idea machine — beats become conversational seeds — SHIPPED

> **Shipped.** An idle away-beat now occasionally (`memory.idle_seed_ratio`,
> default `0.25`, needs a worker model) turns into a forward-looking
> conversational **seed** — `IdleAwayActivityWorker._maybe_emit_seed` LLM-composes
> one short thought/question/opinion sparked by what she was doing and appends it
> to the `aiko.idle_seeds` kv ring (`load_idle_seeds`), daily-capped
> (`idle_seed_daily_cap`, default 3) and ring-bounded (`idle_seed_max_ring`).
> Consumer is a new one-shot **cue producer** `_render_idle_seed_block` (T6, next
> to the K9 curiosity seeds, dropped under aggressive mode) — it surfaces the
> newest unseen seed as a private "Earlier, while you were &lt;activity&gt;, a
> thought crossed your mind: …" line so Aiko phrases it herself (never spoken
> verbatim, per the prepared-nudge rule). Bounded by a per-seed watermark
> (`idle_seed.surfaced_at`) plus a wall-clock surfacing cooldown
> (`idle_seed.surfaced_clock`, `idle_seed_surface_cooldown_seconds`, default
> 30 min). Unlike the gap-return cues it is NOT gap-gated and does NOT touch
> `_gap_cue_surfaced` — a thought from her own idle life can come up
> mid-conversation. Master switch `agent.idle_seed_enabled`. MCP:
> `get_idle_seed_state`, `force_idle_seed_surface`. Tests: `tests/test_idle_seed.py`.

---

**Motivation.** Aiko already has a deep "new ideas" stack — the
[`CuriositySeedWorker`](../../app/core/proactive/curiosity_seed_worker.py),
[`ForwardCuriosityWorker`](../../app/core/proactive/forward_curiosity_worker.py),
[`IdleCuriosityWorker`](../../app/core/proactive/idle_curiosity_worker.py),
[`IdleKnowledgeWorker`](../../app/core/proactive/idle_knowledge_worker.py),
`reflection` / `[dream]` / `open_question` memory kinds — but it runs **parallel
to, and disconnected from, her room life**. When she "reads a book" or "doodles"
in the K36 away-beat, nothing comes *out* of it: the activity is a cosmetic
summary line that never becomes a thought she carries back. That's the real
reason "what were you doing?" lands on the same flat "at my desk, thinking" —
the beats are inert. The single most generative change is to let an idle beat
**occasionally produce a seed**: a takeaway from the book ("I read a line that
made me think of you"), a question sparked by looking out the window, a small
opinion formed while tinkering. Grounding the idea machine in *what she was
actually doing* makes both halves richer at once.

**Proposal.** When an away-beat fires (H13/H14), with a low probability hand the
beat's context to the existing seed path rather than inventing a separate one:
emit an `open_question` / curiosity seed / `[[remember:self:…]]`-style opinion
tagged with its origin activity, and surface it through a **cue producer**
(one-shot, watermark-gated inner-life block — per the prepared-nudge rule, NOT a
verbatim nudge) so she phrases "while I was reading earlier I started wondering
…" herself in context. Bounded hard: most beats stay silent, one in N yields a
seed, daily-capped.

**Key files.**
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(emit a seed from the chosen beat),
[`app/core/proactive/curiosity_seed_worker.py`](../../app/core/proactive/curiosity_seed_worker.py)
/ [`forward_curiosity_worker.py`](../../app/core/proactive/forward_curiosity_worker.py)
(reuse the seed + cue-ring pattern),
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`open_question` / `self` kinds), a new one-shot provider alongside the K36
`_render_away_activities_block` in
[`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py).

---

## H18. Weighted, anti-repetition activity selection — SHIPPED

> **Shipped.** New pure module
> [`activity_selection.py`](../../app/core/world/activity_selection.py)
> (`compute_weights` / `weighted_pick`) replaces the flat `random.choice` in
> `IdleAwayActivityWorker._pick_activity`. Weights combine an anti-repetition
> recency penalty (read from the journal ring, harsher for more-recent beats,
> floored so nothing is impossible), a circadian tilt (`_CIRCADIAN_BIAS`), a
> day-color tilt (`_DAY_COLOR_BIAS`, K27 palette), and an affect/valence tilt
> (cozy vs active). The worker reads period / valence / day-color via optional
> providers wired in `idle_workers_init_mixin`. Tests:
> `tests/test_activity_selection.py`.


**Motivation.** "At my desk, thinking" dominates because
[`IdleAwayActivityWorker._pick_activity`](../../app/core/world/idle_activity_worker.py)
ends with `self._rng.choice(list(candidates.values()))` — a **uniform** pick
over all candidates, and the `wander`/`thinking` fallback is *always* in the
pool. There's no memory of what she just did, no time-of-day weighting, no mood
colour. So the loop is both repetitive and emotionally flat — she'll "doodle"
at 3am and "think about you" five beats running. The inputs to fix this are all
already computed.

**Proposal.** Replace the uniform pick with a weighted one:
- **Anti-repeat** — down-weight (or exclude) the last 2–3 activities recorded in
  the away-journal so consecutive beats vary.
- **Circadian** — bias toward `napping`/`bed` late at night, `reading`/`tea` in
  the evening, `tinkering`/`watching_screens` during the day
  ([`circadian.py`](../../app/core/affect/circadian.py)).
- **Affect + day colour** — comfort beats (curl up, tea, blanket) when valence
  is low; restless/active beats (tidy, pace, tinker) on a `restless` /
  `sharp_witted` [day colour](../../app/core/affect/day_color.py); dreamy beats
  on `dreamy`. So the activity *expresses* her inner weather.

Cheap, no model spend, and it directly answers the literal complaint.

**Key files.**
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(`_pick_activity` weighting + journal-based recency),
[`app/core/affect/affect_state.py`](../../app/core/affect/affect_state.py),
[`app/core/affect/day_color.py`](../../app/core/affect/day_color.py),
[`app/core/affect/circadian.py`](../../app/core/affect/circadian.py).

---

## H19. Hobbies & ongoing personal projects — SHIPPED

> **Shipped.** A single multi-day **current hobby** that progresses across days
> gives Aiko continuity of intent. Pure module
> [`app/core/world/hobby.py`](../../app/core/world/hobby.py) owns an 8-entry
> catalogue (`scifi_series` / `guitar` / `astronomy` / `sketchbook` / `baking` /
> `houseplants` / `language` / `vinyl`) + the deterministic `pick_hobby` /
> `render_hobby_line` / `should_rotate` / `is_milestone` math. New
> [`HobbyWorker`](../../app/core/proactive/hobby_worker.py) (`IdleWorker`, kv blob
> `aiko.current_hobby`) starts a hobby, advances it during quiet windows — paced
> by a wall-clock `hobby_advance_min_hours` floor so progress doesn't climb every
> tick — emits a takeaway **seed** every `hobby_milestone_every` advances (LLM-
> composed, surfaced through the **shared H17 idle-seed cue** via the new
> `append_idle_seed` helper), and rotates to a fresh hobby after
> `hobby_max_advances` (wrapping up emits a "finished X, starting Y" seed). The
> standing "what she's been up to lately" line is rendered by a new
> `_render_hobby_block` provider (T4 ambient, after `activity_block`, dropped under
> aggressive mode but **survives the grounding-line fusion** — it's a slow trend,
> not a situational block). Settings: `agent.hobby_worker_enabled` +
> `memory.hobby_worker_interval_seconds` / `hobby_advance_min_hours` /
> `hobby_milestone_every` / `hobby_max_advances`. MCP: `get_hobby_state`,
> `force_hobby_advance`, `force_hobby_rotate`. Tests: `tests/test_hobby_worker.py`.

---

**Motivation.** Aiko's idle life has no *continuity of intent* — every beat is a
one-off. Real people have threads they return to: a book series they're partway
through, learning the guitar, an astronomy phase, reorganising the shelf by
colour over a week. A persistent **current hobby / project** that *progresses
across days* is one of the richest possible idle-life upgrades: it gives her
genuinely new things to talk about ("I'm three chapters into that series now and
ugh, the betrayal"), forms real preferences/opinions she can voice (`kind="self"`
stance memories), and makes the gaps between sessions feel *used*. It also dovetails
with the K1 goals system, which already models long-running intent.

**Proposal.** A small kv-backed "current hobby/project" with a free-text label,
a progress counter, and a started-at; a low-cadence idle worker advances it
(reads the next chapter, practices, learns a fact) and occasionally writes the
takeaway as a `self`/`open_question` memory + a one-shot cue (H17 surfacing).
Hobbies rotate occasionally so she isn't on the same one forever. Optionally
seed the first hobby from her interest-map / goals so it's *about* things she
and the user actually discuss.

**Key files.** New `app/core/proactive/hobby_worker.py` (idle worker; kv state +
progress + takeaway), [`app/core/goals/goal_store.py`](../../app/core/goals/goal_store.py)
(tie to long-running intent), [`memory_store.py`](../../app/core/memory/memory_store.py)
(`self` / `open_question`), a `_render_hobby_block` provider in
[`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py),
`agent.hobby_worker_enabled`.

---

## H20. A room that evolves — depleting + accruing micro-state — SHIPPED

> **Shipped.** The seeded room now accrues a history via a low-cadence
> [`RoomEvolutionWorker`](../../app/core/world/room_evolution_worker.py)
> (`IdleWorker`) that applies **one** bounded micro-state transition per run,
> paced by a wall-clock floor (`memory.room_evolution_min_hours`, default 8h, kv
> gate `aiko.room_evolution_at`) and broadcasts the `world_updated` patch so the
> World tab shows the drift. Three transitions, deterministic math in the pure
> [`room_evolution.py`](../../app/core/world/room_evolution.py): the **tea pot**
> cycles full → half → empty → (brews a fresh flavour from `TEA_FLAVORS`); the
> **cookie jar** is refilled with a fresh batch (`given_by="aiko"`) once it runs
> low/empty — re-created if it was consumed to nothing — closing the loop with the
> away-beat "snack"; the **sci-fi paperback** gains chapter `progress` and, on
> finishing, flips to a brand-new book (name + blurb from `BOOK_TITLES`) and emits
> a takeaway **seed** through the shared H17 idle-seed cue ("finally finished X —
> that ending!", LLM-composed with a deterministic template fallback since a
> finished book is worth a seed even without a worker model). Settings:
> `agent.room_evolution_enabled` + `memory.room_evolution_interval_seconds` /
> `room_evolution_min_hours`. MCP: `get_room_evolution_state`,
> `force_room_evolution`. Tests: `tests/test_room_evolution.py`.

---

**Motivation.** The room resets to the same furniture forever. The tea pot is
"often half full of jasmine tea" as a static description; the cookies decrement
but nothing refills; the sci-fi paperback is eternally "dog-eared at the climax".
Nothing **accrues a history**. A room that quietly changes over time — the tea
pot empties and she brews a fresh pot, she *finishes* the paperback and starts a
new one, the doodle notebook fills page by page, fairy lights need a new bulb —
makes the space feel lived-in and gives her concrete, evolving things to mention
("finally finished that book — want to hear the ending?"). The `world_items`
rows already carry a free-form `state` JSON
([`world_store.py`](../../app/core/world/world_store.py)), so most of this is
small state transitions, not new schema.

**Proposal.** A slow "room evolution" pass (could be folded into the away-beat or
a dedicated low-cadence worker): consumables she uses get refilled with a fresh
batch (new flavour of tea/cookies, `given_by="aiko"`); a `book` she's been
reading gains a `progress` in its `state` and flips to "finished → started a new
one" at the end (a great H17 seed); the doodle notebook accrues a page count.
Bounded, idempotent, broadcasts the `world_updated` patch so the World tab shows
the drift.

**Key files.**
[`app/core/world/world_store.py`](../../app/core/world/world_store.py)
(item `state` transitions, refill helper),
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(hook beats to micro-state changes), optional new
`app/core/world/room_evolution_worker.py`.

---

## H21. Sleep & overnight rhythm — and dreams that surface — SHIPPED

> **Shipped.** The producer side was already in place from earlier batches:
> H16 [`CircadianSettleWorker`](../../app/core/world/circadian_settle_worker.py)
> settles her into `bed`/`napping` in the late-night band once the lively beats
> taper off, and H18's `_CIRCADIAN_BIAS`
> ([`activity_selection.py`](../../app/core/world/activity_selection.py)) already
> boosts `nap` heavily overnight — so she no longer idles at the desk at 3am. H21
> adds the missing **behavioural anchor + dream home**: a one-shot
> **sleep-return cue**. The pure
> [`sleep_return.py`](../../app/core/world/sleep_return.py) decides whether a
> typed gap plausibly spanned an overnight sleep (`looks_like_overnight` —
> morning-band return after `sleep_return_min_gap_hours`=5h, OR any gap ≥
> `sleep_return_overnight_hours`=9h) and picks a believable spot
> (`sleep_spot_phrase` from her current room location). The provider
> [`_render_sleep_return_block`](../../app/core/session/inner_life_part2.py) is
> armed post-turn (`_maybe_arm_sleep_return_slot`) and runs **first** in the
> gap-cue family (after K28 `turning_over`, before K36 `away_activities` / K34
> `forward_curiosity`) so an overnight return wins the one-of `_gap_cue_surfaced`
> slot with the sleep frame. When a recent `[dream]` reflection exists (within
> `sleep_return_dream_lookback_hours`=18h) it's woven in — finally giving the
> [`DreamWorker`](../../app/core/proactive/dream_worker.py) dreams a cause ("I
> dozed off on the beanbag and had the strangest dream about …"). A
> non-overnight gap returns silently **without** consuming the one-of slot, so
> the ordinary away/forward cues still fire. Settings:
> `agent.sleep_return_enabled` + `memory.sleep_return_min_gap_hours` /
> `_overnight_hours` / `_dream_lookback_hours`. Persona: "When I dozed off" block
> in [`aiko_companion.txt`](../../data/persona/aiko_companion.txt). MCP:
> `get_sleep_return_state`, `force_sleep_return_surface`. Tests:
> `tests/test_sleep_return.py`.

**Motivation.** Aiko never sleeps. At 3am she's still "doodling at the desk",
which quietly breaks immersion, and the existing
[`DreamWorker`](../../app/core/proactive/dream_worker.py) writes `[dream]`
memories with **no behavioural anchor** — she dreams without ever having rested.
A real overnight rhythm (she settles into bed and naps late at night; a long
overnight gap reads as "I dozed off") both fixes the "awake at 3am at the desk"
tell and gives the dream system a natural home: "I actually fell asleep on the
beanbag earlier and had the strangest dream about …".

**Proposal.** Circadian-gated rest state: in the late-night band the activity
selector (H18) strongly prefers `bed`/`napping`; a long overnight typed gap is
narrated on return as having slept; and the K28 turning-over / dream surfacing is
nudged to occasionally pair a `[dream]` memory with the sleep beat so the dream
has a cause. Soft and capped — she's not narcoleptic, and she still keeps the
occasional late-night-owl beat.

**Key files.**
[`app/core/affect/circadian.py`](../../app/core/affect/circadian.py) (period),
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(rest beat),
[`app/core/proactive/dream_worker.py`](../../app/core/proactive/dream_worker.py)
(link a dream to the sleep beat), the K28 turning-over surfacing path.

---

## H22. Light outings — "I stepped out for a bit" — SHIPPED

> **Shipped.** A rare `outing` beat in
> [`IdleAwayActivityWorker`](../../app/core/world/idle_activity_worker.py).
> `_pick_activity` offers an `outing` candidate only when its own gates pass —
> daylight (`_OUTING_DAYLIGHT_PERIODS`, tolerant of an unknown period), an
> independent cooldown (`memory.outing_cooldown_hours`, default 6h, kv
> `outing.last_fired_at`), and a small daily cap (`memory.outing_daily_cap`,
> default 2, kv `outing.day` / `outing.day_count`) — so it stays special even
> when ordinary beats fire often. Chosen via the same H18 weighted pick; when
> it lands, `run()` stamps the outing's own watermarks. Single-phase + past
> tense (`_OUTING_BEATS` — "popped out for a walk", "grabbed a coffee from the
> place downstairs"): no `scene_id`, no item relocation, no location move (she's
> back by the time it's journalled) — explicitly the **v0 of H5**. The trace
> rides the same away-journal + `_render_away_activities_block` surfacing, and
> the existing H17 idle-seed path turns the trip into a small detail she
> brought home. Master switch `agent.outings_enabled`. MCP: `force_outing`
> (+ `outing_debug_state`). Tests:
> `tests/test_idle_activity_worker.py::OutingTests`.

**Motivation.** The world is the room plus the garden, both *at home*. An
occasional brief **outing** ("popped out for a walk and the air was lovely",
"grabbed a coffee from the place downstairs") adds variety and gives her fresh,
outside-the-box things to mention without the full machinery of **H5**
(second-scene / travel semantics). It's the lightweight precursor: no new
`scene_id`, no item relocation — just a rare away-beat that narrates a short trip
out and back, optionally returning with a small detail (a flower from the walk, a
new coffee she liked → a `self` preference) that feeds H17.

**Proposal.** Add an `outing` beat to the away-activity worker, gated to daylight
+ rare cadence + daily cap, that sets a transient "out" framing in the world
state (or just narrates it in the journal) and returns after a short interval,
sometimes dropping an item or seed. Treat it explicitly as the v0 of H5 so the
two don't collide.

**Key files.**
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py)
(`outing` beat + return),
[`app/core/world/world_store.py`](../../app/core/world/world_store.py)
(transient framing / produced item), cross-reference **H5**.

---

## Minor polish

These were in the bottom "Other ideas considered" of the legacy
backlog. None of them are urgent; folded here so they don't get
forgotten.

- **Second TTS provider behind `TtsEngine`.** Pocket-TTS is the only
  implemented backend. Adding e.g. Piper, Coqui, or an OpenAI-compatible
  cloud voice would let users pick a different timbre / language without
  swapping the whole pipeline. The `TtsEngine` protocol in
  [`app/tts/base.py`](../../app/tts/base.py) is the extension point.
- **SSML prosody for emotional speech.** _Shipped_ — see the
  "Aiko expressive speech (Pocket-TTS prosody overlay)" entry in
  [`shipped.md`](shipped.md). Pocket-TTS still doesn't accept SSML
  natively, so the rollout instead wired the dormant knobs
  (`tts_length_scale`, ambient volume gain, runtime temperature),
  added real timed pauses, introduced a per-sentence
  `[[prosody:whisper|soft|slow|fast|firm]]` markup family, expanded
  the earcon palette (chuckle / soft_sigh / sharp_gasp / breath /
  mm) with auto-sprinkle on sad openers, and widened the speed
  clamp to ±12% with per-reaction sub-caps. All CPU, no new model.
- **Barge-in enabled by default for Live mode.** Currently
  `audio.barge_in_enabled: false` in [`config/default.json`](../../config/default.json).
  The plumbing is there in [`app/core/session/live_session.py`](../../app/core/session/live_session.py);
  flip the flag and validate against the existing
  `barge_in_min_speech_seconds` floor. **Do P25 first** (client-side
  audio flush, see [`perf.md`](perf.md#p25-client-keeps-playing-scheduled-audio-after-server-side-tts-stop)) —
  server-side barge-in without the client flush still talks over
  the user for up to a few seconds of already-scheduled audio.
