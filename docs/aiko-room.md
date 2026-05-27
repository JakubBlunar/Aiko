# Aiko's room

A small, persistent "virtual apartment" that gives Aiko a sense of
place. The room is a *background* feature — she knows where she is and
what's nearby, but the prompt explicitly tells her not to force-mention
it every turn. The user can also drop items into the room (cookies,
tea, plushies) and Aiko notices them naturally on her next reply.

This document is a one-stop reference for the data model, the prompt
seam, the agent tools, the give-cookie flow, the default room seed,
and how to extend it.

---

## Data model

Three SQLite tables, created at schema v6 by
[`app/core/chat_database.py`](../app/core/chat_database.py):

```
world_locations  (id, slug, name, description, position)
world_items      (id, slug, name, description, kind, consumable,
                  quantity, location_id NULLABLE, state_json,
                  given_by, created_at, updated_at)
world_state      (id=1 singleton, location_id, posture, activity,
                  mood_note, updated_at)
```

- `location_id IS NULL` on a `world_items` row means **Aiko is
  carrying that item** (e.g. she pocketed a cookie before walking to
  the beanbag).
- `world_state` is intentionally a singleton: there's one Aiko per
  assistant.
- All vocabulary is whitelisted. Invalid `kind` / `posture` /
  `activity` values are clamped to defaults rather than raising — see
  [`app/core/world_store.py`](../app/core/world_store.py)
  `VALID_KINDS` / `VALID_POSTURES` / `VALID_ACTIVITIES`.

Pinning, RAG mirroring, and decay are intentionally **not** duplicated
from `MemoryStore`: the room is curated by the user + Aiko, not mined
by background workers, so it doesn't need the same machinery.

---

## Default seed (rich room)

`WorldStore.seed_default()` runs once on first boot if the world is
empty. It populates seven locations — `bed`, `desk`, `bookshelf`,
`kitchenette`, `window_seat`, `beanbag`, `mirror_corner` — and ten
items, anchored at the desk:

| Item | Where | Kind | Notes |
|---|---|---|---|
| dual monitors | desk | gadget | non-consumable |
| retro keyboard | desk | gadget | |
| warm lamp | desk | decor | |
| sci-fi paperback | bookshelf | book | |
| photo of Jacob | bookshelf | keepsake | the relationship anchor |
| plush blanket | bed | decor | |
| cat pillow | bed | toy | |
| **cookies** | kitchenette | food | **consumable, qty=3** |
| tea pot | kitchenette | gadget | |
| fairy lights | beanbag | decor | |

The initial `world_state` puts Aiko at the desk, sitting, watching
screens. The persona file at
[`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt)
already mentions her "cozy virtual apartment full of books, gadgets,
and glowing screens" — the seed is built to match that line.

To reset the room (e.g. mid-development): use the World tab's
"Reset to default room" button or `POST /api/world/seed?force=true`.
Memories are not affected — only the world tables.

---

## Prompt seam

Inner-life provider name: `world`. Wired in
`SessionController._render_world_block()` and registered via
`PromptAssembler.set_inner_life_providers(world=...)`.

The block is **per-turn dynamic** (read fresh on every assemble, like
`narrative_block`) so cookie consumption / state changes from agent
tools surface in the next prompt. It's dropped in `aggressive=True`
mode to free tokens for history.

Example block:

```
You are in your room. Right now: at the desk, sitting, watching screens.
Nearby at the desk: dual monitors, a retro keyboard, a warm lamp.
Jacob gave you 3 cookies in the kitchenette.
Acknowledge your surroundings only when it feels natural — never force
a room mention or list your inventory.
```

The last line is the **tonal nudge** — keep it. It's what stops Aiko
from turning every reply into a travelogue.

The block lands between `agenda_block` and `catchphrase_block` in the
system prompt (see `assemble_with_budget` in
[`app/core/prompt_assembler.py`](../app/core/prompt_assembler.py)).

---

## Agent tools

Five tools in [`app/llm/tools/world.py`](../app/llm/tools/world.py),
gated by `tools.world` in config (default `true`):

| Tool | What it does |
|---|---|
| `look_around` | Returns Aiko's current location, posture, activity, items here, items carried, and other locations. Tool description tells the model: *"Call only when the user asks about your room/surroundings, when you genuinely want to ground a metaphor, or when something in your space is plot-relevant. Do NOT call it on every turn."* |
| `move_to` | Move Aiko to a different location. Fuzzy slug/name match. |
| `change_posture` | Change posture and/or activity (sitting → lying, watching_screens → reading). Both vocabularies are validated. |
| `inspect_item` | Detailed read of one item (description, current state, quantity remaining). |
| `consume_item` | Decrement a consumable's quantity. Refuses politely on non-consumables ("the lamp isn't something you can consume"). The row is deleted at quantity zero. |

Each tool description includes the same "only when natural" tonal
nudge so Aiko doesn't spray tool calls.

---

## Give-cookie flow (silent)

The user-facing surface is the **World** tab in `SettingsDrawer.tsx`,
under "Give Aiko something". Quick-give buttons cover the four common
cases (🍪 Cookie, 🍵 Tea, 🧸 Plushy, 🌷 Flower); a "Custom..." form
covers the rest.

Lifecycle of a give:

1. UI calls `api.giveItem({ name, kind, quantity, ... })`. The
   convenience wrapper sets `given_by="user"` and defaults the
   location to the kitchenette if none is provided.
2. Backend handler `POST /api/world/items` (with the user-attribution
   payload) calls `SessionController.add_world_item(...)`.
3. The `WorldStore` either inserts a new row or stacks into an
   existing same-slug consumable (cookies always merge; the lamp does
   not).
4. The session controller fires `_notify_world({"item": ...})`, which
   broadcasts a single `world_updated` WS event with the new row.
5. The Zustand reducer `applyWorldPatch` merges the row into the
   store; the World tab re-renders with the gift visible.
6. **No proactive message is sent**. On Aiko's next turn, the
   `world` prompt block surfaces the new gift via its
   "Jacob gave you …" line. She notices when it feels natural.

Why silent? See the conversation that designed this: the user
explicitly asked for "silent inventory add" so the immersion isn't
broken by canned "thanks for the cookie!" notifications. Aiko picks
up the gift in her own voice when the moment is right.

---

## REST surface

All routes live in [`app/web/server.py`](../app/web/server.py):

| Route | Purpose |
|---|---|
| `GET /api/world` | Full snapshot: state + locations + items + enabled flag. |
| `PATCH /api/world/state` | Patch posture / activity / location_id / mood_note. |
| `POST /api/world/locations` | Create a location. |
| `PATCH /api/world/locations/{id}` | Update name / description / position. |
| `DELETE /api/world/locations/{id}` | Delete (cascades item.location_id to NULL; clears state pointer). |
| `POST /api/world/items` | Create an item. The "give" wrapper passes `given_by: "user"`. |
| `PATCH /api/world/items/{id}` | Update name / description / kind / location / quantity / state. |
| `DELETE /api/world/items/{id}` | Delete an item. |
| `POST /api/world/items/{id}/consume` | Decrement quantity (and delete on zero for consumables). |
| `POST /api/world/seed?force=true` | Wipe and re-seed the rich default room. Debug-only. |

WS event: `world_updated` with a typed `patch` payload. The reducer
in `web/src/store.ts` (`applyWorldPatch`) handles each shape:

- `{ state }` — replace `world.state`.
- `{ location }` — upsert by id, sort by position.
- `{ item }` — upsert by id.
- `{ deleted_location_id }` — remove location, NULL out items at it,
  clear state pointer if it was there.
- `{ deleted_item_id }` — remove item.
- `{ snapshot }` — replace everything (used after a reseed).

---

## Frontend

Settings drawer tab id: `"world"`, icon 🏠. The `WorldTab` component
is in [`web/src/components/SettingsDrawer.tsx`](../web/src/components/SettingsDrawer.tsx)
and renders four sections:

1. **Right now** — Aiko's current location / posture / activity, with
   inline `<select>`s that fire `PATCH /api/world/state`.
2. **Give Aiko something** — the four quick-give presets plus a
   custom form. Each give is silent.
3. **Items** — grouped by location. Items given by the user have a
   green "gift" pill. Each row supports edit-in-place, delete, and
   (for consumables) a "consume" button that decrements quantity and
   deletes the row at zero.
4. **Locations** — full editor for the room layout, plus a "+ Add"
   form for new locations.
5. **Reset** — wipe + re-seed the default room (with a confirm
   prompt). Memories are untouched.

Types live in [`web/src/types.ts`](../web/src/types.ts):
`WorldLocation`, `WorldItem`, `WorldState`, `WorldSnapshot`,
`WorldPatch`, plus the `WORLD_KINDS` / `WORLD_POSTURES` /
`WORLD_ACTIVITIES` const arrays.

API helpers live in [`web/src/api.ts`](../web/src/api.ts):
`getWorld`, `patchWorldState`, `createWorldLocation`,
`updateWorldLocation`, `deleteWorldLocation`, `createWorldItem`,
`updateWorldItem`, `deleteWorldItem`, `consumeWorldItem`, `giveItem`
(thin shortcut over `createWorldItem`), `reseedWorld`.

---

## Extension guide

### Adding a new posture / activity

Edit `VALID_POSTURES` / `VALID_ACTIVITIES` in
[`app/core/world_store.py`](../app/core/world_store.py) and add the
matching entry to `WORLD_POSTURES` / `WORLD_ACTIVITIES` in
[`web/src/types.ts`](../web/src/types.ts) so the UI dropdown picks
it up. No schema migration needed — the column is just `TEXT`.

### Adding a new item kind

1. Add the slug to `VALID_KINDS` in `world_store.py`.
2. Mirror in `WORLD_KINDS` in `web/src/types.ts`.
3. Optionally extend `_DEFAULT_ITEMS` if you want the seed to ship
   one of the new kind.

### Adding a new agent tool

Define the class in
[`app/llm/tools/world.py`](../app/llm/tools/world.py) and add it to
the list returned by `build_world_tools(session)`. The
`SessionController.rebuild_tool_registry()` path picks it up
automatically. **Always** include the "only when natural" nudge in
the tool description — that's the rule that keeps Aiko from spraying
calls.

### Adding a new REST endpoint

Add the route in `app/web/server.py` next to the existing
`/api/world/*` block. If the endpoint mutates the world, call
`session._notify_world(patch)` so the matching `world_updated` WS
event fires and the UI stays live without a refetch.

### Multi-room support (future)

Currently the world has the apartment plus a single outdoor garden
plot (see "Garden" below). To grow into more scenes (a balcony, a
coffee shop, a library) we'd need:

- A `scene_id` column on `world_state` and on each location.
- A `change_scene` agent tool.
- A bigger render block describing the current scene + maybe one
  hint about which other scenes are reachable.

The current `_OUTDOOR_SLUGS` switch in `render_block` is a tiny
foreshadowing of that — extending it would let outdoor scenes share
phrasing instead of being hardcoded.

Marked as a follow-up in
[`docs/personality-backlog.md`](personality-backlog.md).

---

## Garden (living plants outside the apartment)

Aiko has a small outdoor garden plot — a sibling location to the
apartment's seven indoor spots, with `slug="garden"`. Plants grow over
wall-clock time, can be watered and harvested, and a background
worker wanders her out there during quiet daylight windows so the
world feels alive even without user prompting.

### Data model

- New item kinds in [`VALID_KINDS`](../app/core/world_store.py):
  `"plant"` and `"seed"`. (`food` already existed and is reused for
  harvest output — fresh basil, tomatoes, lavender sprigs land in
  the kitchenette as ordinary consumable food items.)
- `kind == "plant"` items carry `state = {species, stage, planted_at,
  last_watered_at, last_promotion_at, days_dry, lifecycle,
  produce_species}`.
- `kind == "seed"` items carry `state = {species, gift_at}`. Seeds
  with `location_id IS NULL` live in Aiko's inventory.
- Stage enum (in promotion order): `sprout → sapling → growing →
  flowering → mature`. `mature` is the terminal "ready to harvest"
  stage; the growth worker no-ops there.
- `_SPECIES_CATALOG` maps species → `(display_name, lifecycle,
  produce_species, produce_name, produce_quantity_range)`. Unknown
  user-gifted seeds fall back to a generic perennial that yields
  "trimmings" so the loop still closes.

### Auto-seed

`WorldStore.ensure_garden_seed()` is idempotent and called from
`SessionController.__init__` after the regular `seed_default`. Older
worlds picked up the garden automatically next boot:

- The `garden` location row.
- A `watering_can` gadget.
- Three plants: `lavender_pot` (perennial, growing),
  `basil_seedling` (perennial, sprout), `tomato_seedling`
  (annual, sprout).
- A `seed_packet_sunflower` (annual) in Aiko's inventory.

### Render block

When Aiko is in an `_OUTDOOR_SLUGS` location the world block flips
from `"You are in your room..."` to `"You are at home, currently
outside in the garden..."`. Plant items get a stage suffix in the
nearby line — `"(sprout)"`, `"(flowering)"`, or the loud
`"(mature, ready to harvest)"` cue that tells the LLM to reach for
`harvest_plant`. Seeds in inventory get `"(seed)"`.

### Background workers

Both workers piggyback on the existing
[`IdleWorkerScheduler`](../app/core/idle_worker_scheduler.py) so they
share its quiet-window gate (no Live mode, no recent user activity).

| Worker | Interval | Behaviour |
|---|---|---|
| [`PlantGrowthWorker`](../app/core/plant_growth_worker.py) | hourly | Walks every `kind == "plant"` item, calls `promote_stage(item)` which advances one step when the stage's `min_age_hours` elapsed and the plant was watered within `_DRY_TOLERANCE_HOURS` (96h). Promotes are broadcast as `world_updated` patches so the UI updates live. |
| [`GardenVisitWorker`](../app/core/garden_visit_worker.py) | 30 min check, 1.5-3.5h randomised cooldown between visits | Two-phase, single worker. **Outbound**: during daylight (`morning / midday / afternoon / early_morning`), moves her to the garden, waters every plant, auto-harvests any that are mature, stamps a `return_at` timestamp 6 min ahead in `kv_meta`. **Inbound**: when the timestamp elapses, moves her back to `desk`. Silent — no chat message, no proactive nudge. |

### Tools

Three new agent tools alongside the original five in
[`app/llm/tools/world.py`](../app/llm/tools/world.py):

| Tool | What it does |
|---|---|
| `water_plant` | Refreshes `state.last_watered_at` + clears `days_dry`. Requires Aiko to be in the same location as the target plant (so she can't water her basil from the bookshelf). Refuses non-plant items politely. |
| `plant_seed` | Consumes a `kind == "seed"` from inventory and creates a fresh `kind == "plant"` row at the chosen location (default `garden`), `stage="sprout"`, with `lifecycle` / `produce_species` pulled from `_SPECIES_CATALOG`. |
| `harvest_plant` | Refuses unless `stage == "mature"`. Delegates to `WorldStore.harvest_plant(item.id)`: spawns a `kind == "food"` item with the species' produce name + a quantity from the species' range, location = `kitchenette` (or any other location / inventory as fallback). **Annual** plants are deleted and a fresh seed of the same species drops into inventory. **Perennial** plants reset to `stage="growing"` so the same plant bears another crop. |

`GardenVisitWorker` auto-harvests mature plants by calling the same
`WorldStore.harvest_plant(item.id)` helper, so the loop keeps moving
even without the LLM ever touching the tool.

### Editing scope (UI)

The existing item editor (`name`, `description`, `kind`,
`location_id`) plus add/delete via `WORLD_KINDS` covers the user-
facing CRUD for plants and seeds — no dedicated plant-state editor.
The World tab adds a small stage badge next to plant items so the
user can see "ready to harvest" at a glance, plus a species badge
next to seeds. Stage / species / `last_watered_at` advance via the
growth worker, the agent tools, or by deleting and re-adding the
plant.

### Acceptance shape

- Fresh boot: garden appears in the World tab with the default plants
  + watering can; LLM prompt block references it correctly when Aiko
  is there.
- After the user gifts a seed (kind `seed` via "Give Aiko something"),
  `plant_seed("sunflower seed packet")` from a chat turn creates a
  new sprout in the garden visible in the UI within one WS tick.
- After enough time, a plant promotes to `mature`, `look_around`
  shows `(mature, ready to harvest)`, `harvest_plant` succeeds and a
  new `food` item appears in `kitchenette`. Annuals produce a seed
  back in inventory; perennials flip to `growing` and start climbing
  the stage ladder again.
- Without any user interaction, after sitting idle through a daylight
  window with at least one mature plant, the World tab eventually
  shows her in the garden, the mature plant is consumed/reset, fresh
  produce shows up in the kitchen, and she returns to her room
  later. Nothing speaks; she just moves.

---

## Testing

| Suite | What it covers |
|---|---|
| [`tests/test_world_store.py`](../tests/test_world_store.py) | Schema migration, seed idempotency, CRUD, stacking on consumables, consume-to-zero, location-cascade, render-block shape, vocabulary clamping, garden seed + plant promotion + watering, harvest perennial/annual branches, outdoor render phrasing. |
| [`tests/test_session_controller_world.py`](../tests/test_session_controller_world.py) | Listener fan-out, `give_item` defaults, render fallback when the store is missing, reseed snapshot. |
| [`tests/test_web_server_world.py`](../tests/test_web_server_world.py) | REST surface — status codes, payload shapes, error branches. |
| [`tests/test_world_tools.py`](../tests/test_world_tools.py) | Each agent tool's happy + sad paths, including `water_plant` / `plant_seed` / `harvest_plant`. |
| [`tests/test_plant_growth_worker.py`](../tests/test_plant_growth_worker.py) | Hourly promotion: due sprouts advance, immature plants stay put, interval gate respected. |
| [`tests/test_garden_visit_worker.py`](../tests/test_garden_visit_worker.py) | Outbound phase moves + waters + auto-harvests; daylight gate blocks night; cooldown blocks repeat visits; inbound phase fires after `return_at`. |
| [`web/src/store.world.test.ts`](../web/src/store.world.test.ts) | `applyWorldPatch` reducer per discriminator + plant/seed kind round-trip. |

Run all together: `python -m pytest tests/test_world_*.py
tests/test_session_controller_world.py tests/test_web_server_world.py
tests/test_plant_growth_worker.py tests/test_garden_visit_worker.py`
plus `cd web && npx vitest run src/store.world.test.ts`.
