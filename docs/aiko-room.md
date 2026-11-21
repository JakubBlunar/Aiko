# Aiko's room

## Grounded idle activity

The apartment and garden are simulated places with authoritative stored state;
they are not desktop sensors or permission to use external tools. Idle activity
only uses items in the current scene (or carried items). Failed item mutations
are reported as failures, not journalled as completed experiences.

Reading requires actual supplied text in a book's `state.chapters` (a non-empty
list of non-empty strings). A title, blurb or legacy progress counter alone does
not enable reading. Each successful step retains a bounded `last_read_excerpt`;
finishing does not invent replacement book contents. Supply those passages with
the existing `PATCH /api/world/items/{item_id}` item-state endpoint (the World
tab does not yet have a passage editor).

`inspect_item` returns one actual book passage (at most 2,000 characters), with
optional 1-based `passage` and character `offset` arguments. `next_offset`
continues a longer passage. Inspection is read-only and never awards progress;
`look_around` lists passage counts rather than dumping every book's contents.
Idle reading thoughts receive the saved passage itself, so a title is not used
to invent a plot reaction. Keep source URL, author and rights information in
item state when supplying a real-world text.

When several readable books are available, idle reading can select any of them;
each retains its own progress and `last_read_at`. The room's reading activity
names the most recently read book in the current scene, rather than always
the original paperback. Set `text_unit` to `"passage"` for short text chunks
so their progress is not described as whole chapters.
An empty tea pot no longer hides a second full pot.

The legacy doodle beat is now imagining a drawing, not claiming an unsaved
sketchbook artifact. Legacy outings are explicitly daydreams, not completed café
visits. Quiet rest, naps and sleep remain valid; activity need not be announced.
LLM activity proposals cannot grant themselves unsupported actions or items.
Personal projects use real research results and the existing diary, as described
in [Idle workers](idle-workers.md#research-grounding).

## Household drinks and shared actions

Chat and idle life share `available_home_actions` and `perform_home_action`.
Availability comes from current world objects, supplies, equipment, and scene.
The read tool returns exact action tokens, target items, required locations,
and deterministic descriptions. Execution is synchronous: each action is one
small simulated step, rather than a real-time brewing process. A posture change
alone does not make or consume anything.

On the next application start, an additive migration supplies a household drink
station, ceramic mug, and drinking glass in the apartment's kitchenette. These
are household provisions, not gifts attributed to the user. Existing belongings
are retained. The migration runs once; deleting a provision does not cause it
to respawn on every restart. An explicit World reset restores the kit too.

The station has a kettle, coffee press, tap, small fridge, and eight portions
each of tea, coffee, hot chocolate, and juice. Water uses the tap. Recipes share
the same vessel/content model; hot recipes require a heat-safe vessel. Preparing
a drink consumes one available portion. An explicit household restock action is
available when supplies are low, at most once per 24 elapsed hours; it restores
stocks to eight. It is simulated provisioning, not a real purchase.

Supported actions:

- Prepare tea, coffee, hot chocolate, water, or juice in a clean, empty vessel.
- Sip 75 ml, leaving the reusable vessel when the drink is finished.
- Pour away the remainder at the station, or wash an empty used vessel there.
- Warm remaining tea, coffee, or hot chocolate when it has cooled sufficiently;
  reheating preserves the remaining amount.
- Open or close an existing lid. Open it before sipping.
- Replenish low household drink supplies under the provisioning rule above.

`vessel` is a portable item kind, subject to the existing two-item carry limit.
Use `take_item`/`put_item` to bring a mug to the window or return it to the
kitchenette. Preparation, washing, emptying, and reheating require both the
vessel and Aiko at the station. Sipping uses the vessel's current spot, or her
current spot when carried. Other scenes cannot supply missing equipment.

New items explicitly given kind `vessel` receive a default `state.home_vessel`.
A name declaring a lid, thermos, or travel mug enables a lid; a thermos or
explicitly insulated vessel also slows heat transfer. Glasses default to cold
drinks. Advanced configuration can use the existing item-state PATCH endpoint:

```json
{"home_vessel": {"schema": 1, "capacity_ml": 250, "heat_safe": true,
                 "has_lid": true, "insulated": false, "lid_closed": false,
                 "clean": true, "drink": null}}
```

Temperature is calculated from a saved reference temperature and timestamp,
toward a simulated indoor ambient temperature of 22 C. Hot drinks cool; chilled
drinks warm. Lids and insulation slow either direction. This is a coarse virtual
model, independent of real outdoor weather, with no claim of physical sensation.
Changing the lid preserves the temperature reached so far. Inspection and time
passing do not consume a drink or create an experience. Restart-time elapsed
time affects temperature, not the amount drunk.

World snapshots and `inspect_item` include a derived `home_drink` view with
contents, amount, temperature band, cleanliness, and lid state. The World tab
updates its temperature display every 30 seconds using the server's narrative
clock anchor. It does not require a background LLM call or minute-by-minute
database writes. Existing gift and item controls also support the vessel kind.

Actions revalidate current state inside a SQLite transaction. Changed items,
activity, optional autonomous movement, and an execution receipt commit together.
Chat actions refresh the existing deliberate-action hold. Stale
tokens fail without moving Aiko or spending supplies. The latest 64 receipts
survive restart and make exact retries return `already_applied=true`; these are
recoveries of the original action, not new experiences. Tokens incorporate item
versions, including an action revision that advances even under a fixed debug
clock. Very old tokens outside that bounded retry window must be refreshed.

The idle planner receives the same capabilities and live tokens as chat. Its
fallback candidates use that catalogue too, and shared-conversation/sleep
admission is checked again before autonomous execution. Small household steps
are completed actions, not artificially extended open activity windows. In a
station-equipped scene the worker uses these actions instead of the legacy
pot-only tea beat. Legacy pots remain separate items maintained by the existing
room-evolution worker; they are not silently converted into mugs or treated as
proof of drink temperature or consumption.

## Editable sketchbooks

The next-start household migration provides a blank sketchbook and portable
pencil/eraser set. It does not reconstruct a previously narrated sketch. Extra
books or materials can come from the virtual shop or the user's Give form:
`sketchbook` initializes blank editable pages; `art_supply` recognizes explicitly
named pencils/erasers. Both are portable and use the existing two-slot carry cap.

`new_sketch` and `revise_sketch` are shared home actions. They require the book
and a pencil at the same spot, or carried; revision additionally requires an
eraser. A valid action includes actual bounded drawing data: a title, note and
strokes with points, width, one of four graphite shades, and optional filled
polygons. A title or narration alone creates nothing. The existing language
model produces those drawing instructions, and the app renders them locally;
no separate image-generation model or service is required.

The drawing surface is 512 by 320, with at most 24 strokes and 256 points per
revision. A book holds sixteen pages and four immutable revisions per page;
the shared daily drawing allowance is three saves across all books, including
stored books. Revisions retain timestamps and content hashes. Stale versions
are rejected; an exact retry recovers the original receipt, while different
drawing data submitted with an already-used token is rejected.

The book includes a stable authored fictional rooftop reference. It is not an
image of real buildings or a visual observation. The idle model sees that
reference, relevant saved drawing data and its optional ongoing interest, then
must supply a real draft to use the action. Its bounded output budget grows for
drawing-capable turns. Deterministic fallback does not invent drawings when no
model draft is available. No timer awards drawing skill or claims improvement.

The World tab renders the actual saved strokes, with page/revision selectors.
`inspect_item(page=..., revision=...)` returns one selected drawing, without
dumping every page into look-around context. It also returns an exact revision
preview URL under `GET /api/world/items/{id}/sketch.svg`. That endpoint renders
validated geometry using fixed SVG elements, escaped text, no external assets
or executable markup, and a restrictive content security policy. Saved work can
be revisited or opened separately. Visual judgment still needs actual vision;
structured inspection alone supports explaining intended marks.

Saved creative work is protected from autonomous disposal; it can be stored
and retrieved through the normal bounded household lifecycle. This is the
simple sketch backend. More polished image generation/editing, arbitrary crafts,
and automatic visual critique remain possible extensions, not current abilities.

## Our keepsake box

The apartment's bookshelf holds a shared keepsake box. Open it under
**World → Belongings → Items → Bookshelf** to read the notes or leave one. Aiko can use
`read_keepsake_notes` and `leave_keepsake_note` in chat. Each slip retains
its text, author and saved date. Either of you can leave a note when a
moment feels worth keeping. Older pages stay available as the box fills.

Existing apartments get the box the next time the backend starts. The
box stays on the bookshelf and can't be stored, discarded, or removed
through the World controls. A room reset restores the box with its notes.
Notes are saved only when one of you writes one.

## Virtual shopping, storage and the bin

The next-start migration adds a household organizer at the existing desk. Its
SQLite item state owns a small virtual shop, wishlist, bounded orders, storage
and bin. `browse_home_catalogue` inspects these; the shared home-action catalogue
provides execution tokens. Ordering requires the computer in the home scene.
It is entirely simulated and makes no external purchases or payment requests.

The catalogue offers lidded/insulated vessels, sketchbook/materials, and a few
modest decorations. There is no forced acquisition quota: she may wish for
something, order it, or use what she owns. One order is admitted per 24 elapsed
hours, with at most two outstanding. An order can be cancelled before its
five-minute dispatch point. It is delivered thirty minutes after ordering;
delivery stages derive from persisted timestamps. Only explicit unpacking
creates the actual item. Retries recover the same execution receipt and never
duplicate the parcel contents. Ordered drawing materials are just materials;
they are not evidence that a drawing exists.

Space is bounded by active belongings and per-surface capacities. Existing
possessions are grandfathered intact at migration, with a little spare space;
fixtures and food stacks do not each count like a displayed possession. Pending
orders reserve capacity. Unpacking, retrieval and restoration recheck capacity
against the current room, so gifts arriving during transit cannot bypass it.
An explicit user gift is retained even when it exceeds an acquisition budget.

Storage holds eight possessions, without disposing of them. A recoverable bin
holds at most sixteen. Both remove items from ordinary room listings, prompts,
item matching, drink/reading candidates and active-space counts. Their actual
rows retain state and the original home, allowing the same object to return.
Gifts, keepsakes, favorites, saved creative work and managed fixtures are
protected from autonomous disposal. Objects whose home belongs to another
scene are not managed as her possessions.

Discarding sets an absolute 48-hour deadline. A compute-only `home_maintenance`
worker clears expired, unprotected rows owned by this bin during an eligible
quiet window, including while Aiko sleeps. It neither journals a new activity
nor writes a memory. The last 32 small disposal records remain inspectable as
past ownership; chat history and unrelated files are untouched. Recovery remains
possible while a row awaits actual clearance. Marking it protected prevents
automatic clearance. No background sweep chooses things to throw away.

The World tab's Home shop and belongings panel shows orders, wishlist, space,
storage and bin deadlines. Bring back performs an explicit user world edit
through the validated restore action. `GET /api/world/home` supplies that view;
`POST /api/world/home/action` accepts only restore/retrieve tokens, including
safe retries. It is not a generic endpoint for arbitrary model actions.

## Bathroom routines

An additive, one-time migration creates one compact bathroom location in the
existing apartment and a household fixture kit: sink/mirror, toothbrush,
hairbrush, small bath, towel, and toothpaste/soap. It preserves existing
locations, including the mirror corner. Deleted fixtures do not respawn at
every restart; an explicit World reset restores the household kits.

The same home-action catalogue exposes brushing teeth, brushing hair, washing
the face, taking a short shower, starting a bath, finishing it, and draining it
early. Fixtures and
supplies must exist in the current scene, and execution requires the bathroom.
Toothpaste and soap are consumed by their respective routines; bounded household
restocking replenishes low stock at most once per 24 hours. Brushing teeth is
eligible at most once per eight hours, face/hair routines per four hours, and
a shower per eight hours, and a bath per twelve hours. These are pacing gates,
not required chores, health
meters, or penalties for being away.

Starting a bath persists `soaking`, the start time, and a readiness time twenty
minutes later. It records only the start, not a completed bath. While this
uninterrupted activity still owns her bathroom location, the away worker waits
and then prioritizes the supported finish action over its general cooldown.
An early drain is separately available to chat. Ending the bath drains the tub
and leaves a drying towel. Water cools and the towel dries as passive state;
inspection or elapsed time never finishes a bath by itself.

Sleep and shared-conversation admission remain higher authority. A deliberate
move or activity change ends the automatic continuation; the stored bath remains
inspectable and can be drained later without inventing continuous soaking or a
completed routine. Restart likewise preserves the actual phase. The World tab
shows phase, readiness, towel state, and the recorded toothbrushing time, with
no forced bathroom report in chat.

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

Four SQLite tables (schema v41 adds `home_location_id` on items). Created by
[`app/core/infra/chat_database.py`](../app/core/infra/chat_database.py):

```
world_scenes     (id, slug, name, description, origin, created_at, updated_at)
                 origin=builtin is her apartment (locked). Custom rows are
                 places you author — typically *your* room.
world_locations  (id, scene_id, slug, name, description, position, locked)
                 UNIQUE(scene_id, slug). Seeded apartment spots are locked.
world_items      (id, slug, name, description, kind, consumable,
                  quantity, location_id NULLABLE, home_location_id NULLABLE,
                  state_json, given_by, created_at, updated_at)
world_state      (id=1 singleton, scene_id, location_id, posture, activity,
                  mood_note, updated_at)
```

- `location_id IS NULL` on a `world_items` row means **Aiko is holding
  that item**. Only pocketable kinds can land there: `food`, `book`,
  `toy`, `keepsake`, `seed`, `vessel`, `art_supply`, `sketchbook`.
  Not gadgets, furniture, decor, plants, or
  `other`. Cap is **2**, excluding seeds (they live in inventory until
  planted). Constants: `PORTABLE_KINDS` / `CARRY_CAP` in
  [`app/core/world/carry.py`](../app/core/world/carry.py).
- `home_location_id` is the spot the item returns to after an idle beat
  or `put_item`. Seeded on add (current location, else the rich-room
  slug map, else the first spot in the scene). Deleting a location
  relocates items to their home (or another remaining spot) — **never**
  into her hands.
- `world_state` is intentionally a singleton: there's one Aiko per
  assistant.
- All vocabulary is whitelisted. Invalid `kind` / `posture` /
  `activity` values are clamped to defaults rather than raising — see
  [`app/core/world/world_store.py`](../app/core/world/world_store.py)
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
| paperback (titled from `BOOK_TITLES` at seed; slug stays `scifi_paperback`) | bookshelf | book | |
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
You're holding a sci-fi paperback. Put it down with put_item when
you're done — don't wander around with an armful.
Jacob gave you 3 cookies in the kitchenette.
Acknowledge your surroundings only when it feels natural — never force
a room mention or list your inventory.
```

The last line is the **tonal nudge** — keep it. It's what stops Aiko
from turning every reply into a travelogue.

The block lands between `agenda_block` and `catchphrase_block` in the
system prompt (see `assemble_with_budget` in
[`app/core/session/prompt_assembler.py`](../app/core/session/prompt_assembler.py)).

---

## Conversation-owned room lease

Conversation context can temporarily own the current scene. Every two user
turns by default, `ConversationSituationWorker` reads a bounded, age-tagged
transcript and records one open-ended per-session interpretation in
`conversation_situation`. For example, if the conversation establishes that
Aiko and the user are sitting together on the beanbag, that interpretation
becomes a room lease only while the authoritative world state also says Aiko
is at the beanbag.

`WorldMutationGuard` combines that lease with the existing
`world.intentional_state_at` hold. The away-activity, garden-visit, and
circadian-settle workers check it in both their demand probe and mutation
step. While a compatible shared situation is active they cannot change
location, posture, or activity, even after the old two-hour intentional hold
expires. Item-only maintenance remains eligible. A pending garden auto-return
is cancelled if the conversation establishes a newer shared situation in the
garden.

The lease is conservative:

- current world/runtime truth always wins over the worker interpretation;
- malformed or timed-out extraction preserves the previous state;
- one implicit miss makes the interpretation uncertain and two end it;
- a direct conversational ending or contradictory deliberate world action
  releases it immediately;
- compatible current turns and deliberate room edits refresh it;
- the persistence ceiling defaults to six hours, so a stale row cannot lock
  the room after a restart or long gap.

World tools and World-tab actions remain deliberate, higher-authority writes.
An incompatible deliberate move clears the inferred lease rather than being
blocked.

---

## Agent tools

Fourteen tools in [`app/llm/tools/world.py`](../app/llm/tools/world.py)
(take/put live in [`world_carry.py`](../app/llm/tools/world_carry.py)),
gated by `tools.world` in config (default `true`). The garden three are
documented under [Garden](#garden-living-plants-outside-the-apartment):

| Tool | What it does |
|---|---|
| `look_around` | Returns Aiko's current scene + spot, posture, activity, items here, items carried, other spots in this scene, and other scenes she can travel to. |
| `move_to` | Walk to a different spot in the *current* scene. Fuzzy slug/name match. |
| `go_to_scene` | Travel to another scene (her apartment, your room). Items stay put; carried items come along. |
| `change_posture` | Change posture and/or activity (sitting → lying, watching_screens → reading). Both vocabularies are validated. |
| `inspect_item` | Detailed read of one item (description, current state, quantity remaining). |
| `consume_item` | Decrement a consumable's quantity. Refuses politely on non-consumables ("the lamp isn't something you can consume"). The row is deleted at quantity zero. |
| `water_plant` / `plant_seed` / `harvest_plant` | The garden loop. Each takes a plant/seed name and is restricted to rows of that `kind`, so `lavender` can't resolve to the cooking sprigs in the kitchenette. |
| `take_item` | Pick up one pocketable thing (food / book / toy / keepsake / seed). Cap 2 excluding seeds; furniture and gadgets stay put. |
| `put_item` | Put a held item down at the current spot, a named spot, or home. |
| `available_home_actions` | Read supported household actions and live execution tokens, including drink, bathroom, shopping, storage/bin, and drawing capabilities. |
| `perform_home_action` | Execute one validated household action; drawing actions require actual structured strokes. |
| `browse_home_catalogue` | Inspect the virtual shop, wishlist, orders, storage, bin, and recent disposal records. |

Each tool description includes the same "only when natural" tonal
nudge so Aiko doesn't spray tool calls.

### How item names are resolved

Every item-taking tool goes through `WorldStore.find_item`, and it has to
be generous, because **Aiko asks for what she just narrated, not what the
row is called**. The ambient block shows her `9 cookies`, but
`inspect_item` hands back the description — *"warm, fish-shaped
chocolate-chip cookies in a glass jar"* — so she'll happily call
`consume_item("fish-shaped cookie")`. The original matcher only asked *is
the query a substring of the name?*, which fails on every query that is
more specific than the row: `a cookie`, `the potato chips`, `jasmine
tea`, and `fish-shaped cookie` all matched nothing.

Matching is therefore word-based, scored in tiers (best first):

| Tier | Fires when |
|---|---|
| exact slug / exact name | `cookie_jar`, `cookies` |
| squashed | punctuation-insensitive equality: `sci-fi paperback` → `scifi_paperback` |
| same words | the query and the name reduce to the same word set |
| query covers | the query contains every word of the name, plus extras — this is the `fish-shaped cookie` case |
| name covers | the name contains every word of the query, plus extras — `scifi` → `scifi_paperback` |
| substring | either direction, raw |
| overlap | **two or more** words in common. One is too weak: `chocolate-chip cookie` and `potato chips` share "chip" |
| description | last resort — the words only appear in the blurb |

Words are compared through a stem *set* rather than a single stem, since
nothing in the surface form of `cookies` says whether it came from
`cookie` or `cooky`; both are kept and two words match when their sets
intersect. Name and slug are scored as separate vocabularies, because
pooling them would demand the query say "jar" before it could match the
cookies.

Equally good candidates are then ordered by **where the thing is**:
edibility first (when the caller passes `prefer_consumable`, so "eat a
tomato" reaches the ripe ones and not the seed packet), then stock on
hand, then proximity, then recency. That last pair matters — the room
routinely holds several rows called "cookies", and eating from the
kitchenette jar while sitting on the beanbag with a bag the user just
handed over reads as a continuity bug.

When nothing matches, the error lists what she *does* have
(`WorldStore.summarize_available`) split into within-reach and elsewhere.
A bare "no item matching 'x'" is a dead end — the model's only move is to
call again with the same string, which is exactly what happened: three
consecutive turns of `consume_item("fish-shaped cookie")`, each refused,
while she narrated a biscuit that kept vanishing before it reached her.

---

## Idle life puts things back

Carrying is a chat-time pocket, not how the room rearranges itself while
you are away. [`IdleAwayActivityWorker`](../app/core/world/idle_activity_worker.py)
never persists `location_id=NULL` for a beat:

- A `read_book` beat moves the paperback **to the beanbag** with her,
  then restores it to `home_location_id` (the shelf) when the beat or
  episode finishes. A chain of tea → read → nap does not leave the book
  on the beanbag overnight.
- `tidy_desk` is a real mutation: portable strays sitting on the desk
  whose home is elsewhere go home. It never picks them up.
- `move_cat` only targets other spots in the **current scene**. The cat
  stays there; that beat is a wander, not a borrow.
- If you return mid-beat, the caught-mid-activity cue `put_item`s the
  used object at her current spot (matching the line she already says).
  When the window later settles, it goes home.

Garden harvest still drops produce in the kitchenette and can leave a
fresh seed in inventory. That pattern is seed-only.

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
| `DELETE /api/world/locations/{id}` | Delete. Items there go home (or another remaining spot), never carried; clears Aiko's state pointer if she was standing there. |
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
- `{ deleted_location_id }` — remove location, send items that lived
  there to `home_location_id` (or the first remaining spot), clear
  state pointer if it was there. A `{ snapshot }` usually follows.
- `{ deleted_item_id }` — remove item.
- `{ snapshot }` — replace everything (used after a reseed).

---

## Frontend

Settings drawer section id: `"world"`. The
[`WorldTab`](../web/src/features/settings/WorldTab.tsx) component groups its
controls into sticky subtabs:

- **Overview** — Right now and Scenes.
- **Belongings** — Give Aiko something and Items, including drink, bathroom,
  creation and sketchbook controls on the relevant objects.
- **Home shop** — catalogue, orders, wishlist, storage, bin and available space.
- **Rhythm** — sleep rhythm, proactive room notices and companion feel.
- **Weather** — shared sky, home city and units.
- **Places** — locations and the room reset.

Switching subtabs preserves drafts and returns the content scroll to the top.
The underlying editors retain these actions:

1. **Right now** — Aiko's current location / posture / activity, with
   inline `<select>`s that fire `PATCH /api/world/state`.
2. **Give Aiko something** — the four quick-give presets plus a
   custom form. Each give is silent.
3. **Items** — grouped by location, with **carrying** last and only for
   pocketable kinds (`WORLD_PORTABLE_KINDS`). The where-dropdown lists
   spots first, then "in her hands" for those kinds. Items given by the
   user have a green "gift" pill. Each row supports edit-in-place,
   delete, and (for consumables) a "consume" button that decrements
   quantity and deletes the row at zero.
4. **Locations** — full editor for the room layout, plus a "+ Add"
   form for new locations.
5. **Reset** — wipe + re-seed the default room (with a confirm
   prompt). Memories are untouched.

Types live in [`web/src/types.ts`](../web/src/types.ts):
`WorldLocation`, `WorldItem`, `WorldState`, `WorldSnapshot`,
`WorldPatch`, plus the `WORLD_KINDS` / `WORLD_PORTABLE_KINDS` /
`WORLD_POSTURES` / `WORLD_ACTIVITIES` const arrays.

API helpers live in [`web/src/api.ts`](../web/src/api.ts):
`getWorld`, `patchWorldState`, `createWorldLocation`,
`updateWorldLocation`, `deleteWorldLocation`, `createWorldItem`,
`updateWorldItem`, `deleteWorldItem`, `consumeWorldItem`, `giveItem`
(thin shortcut over `createWorldItem`), `reseedWorld`.

---

## Extension guide

### Adding a new posture / activity

Edit `VALID_POSTURES` / `VALID_ACTIVITIES` in
[`app/core/world/world_store.py`](../app/core/world/world_store.py) and add the
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

### Extra scenes (H5)

The apartment and garden are a locked builtin scene. You can still put
objects in them. Extra scenes — typically *your* room — are authored
in the World tab: create the scene, add spots, drop objects, then
"Bring Aiko here" (or she calls `go_to_scene`). Lookups stay in the
scene she is in, so idle workers will not walk her to *her* bed while
she is visiting. Force-reseed resets the apartment only.

See [H5 in `shipped/immersion.md`](../backlog/personality/shipped/immersion.md#h5-user-owned-scenes--she-can-be-in-your-room).

---

## Garden (living plants outside the apartment)

Aiko has a small outdoor garden plot — a sibling location to the
apartment's seven indoor spots, with `slug="garden"`. Plants grow over
wall-clock time, can be watered and harvested, and a background
worker wanders her out there during quiet daylight windows so the
world feels alive even without user prompting.

### Data model

- New item kinds in [`VALID_KINDS`](../app/core/world/world_store.py):
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
[`IdleWorkerScheduler`](../app/core/proactive/idle_worker_scheduler.py) so they
share its quiet-window gate (no Live mode, no recent user activity).

| Worker | Interval | Behaviour |
|---|---|---|
| [`PlantGrowthWorker`](../app/core/world/plant_growth_worker.py) | hourly | Walks every `kind == "plant"` item, calls `promote_stage(item)` which advances one step when the stage's `min_age_hours` elapsed and the plant was watered within `_DRY_TOLERANCE_HOURS` (96h). Promotes are broadcast as `world_updated` patches so the UI updates live. |
| [`GardenVisitWorker`](../app/core/world/garden_visit_worker.py) | 30 min check, 1.5-3.5h randomised cooldown between visits | Two-phase, single worker. **Outbound**: during daylight (`morning / midday / afternoon / early_morning`), moves her to the garden, waters every plant, auto-harvests any that are mature, stamps a `return_at` timestamp 6 min ahead in `kv_meta`. **Inbound**: when the timestamp elapses, moves her back to `desk`. Silent — no chat message, no proactive nudge. |

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
| [`tests/test_world_store.py`](../tests/test_world_store.py) | Schema migration, seed idempotency, CRUD, stacking on consumables, consume-to-zero, location-cascade (home, not NULL), carry refuse/cap/tidy-on-load, render-block shape + holding nudge, vocabulary clamping, garden seed + plant promotion + watering, harvest perennial/annual branches, outdoor render phrasing. |
| [`tests/test_idle_activity_worker.py`](../tests/test_idle_activity_worker.py) | Idle beats: book to beanbag then home, tidy_desk returns strays, move_cat stays in-scene, in-progress settle puts the used item home. |
| [`tests/test_session_controller_world.py`](../tests/test_session_controller_world.py) | Listener fan-out, `give_item` defaults, render fallback when the store is missing, reseed snapshot. |
| [`tests/test_web_server_world.py`](../tests/test_web_server_world.py) | REST surface — status codes, payload shapes, error branches. |
| [`tests/test_world_tools.py`](../tests/test_world_tools.py) | Each agent tool's happy + sad paths, including `water_plant` / `plant_seed` / `harvest_plant` / `take_item` / `put_item`. |
| [`tests/test_plant_growth_worker.py`](../tests/test_plant_growth_worker.py) | Hourly promotion: due sprouts advance, immature plants stay put, interval gate respected. |
| [`tests/test_garden_visit_worker.py`](../tests/test_garden_visit_worker.py) | Outbound phase moves + waters + auto-harvests; daylight gate blocks night; cooldown blocks repeat visits; inbound phase fires after `return_at`. |
| [`web/src/stores/useWorldStore.test.ts`](../web/src/stores/useWorldStore.test.ts) | `applyWorldPatch` reducer per discriminator + plant/seed kind round-trip. |

Run all together: `python -m pytest tests/test_world_*.py
tests/test_session_controller_world.py tests/test_web_server_world.py
tests/test_plant_growth_worker.py tests/test_garden_visit_worker.py`
plus `cd web && npx vitest run src/store.world.test.ts`.
