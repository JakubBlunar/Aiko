# Avatar + expressiveness

Open items in the avatar / expressiveness depth pass. Shipped entries (B1, B2, B4, B5, B6) live in [`shipped.md`](shipped.md).

---

## B3. Blink-rate modulation by arousal (deferred follow-up to B1)

**Why deferred.** B1's plan considered tying blink interval to the
arousal axis (faster blinks under high arousal, slower under low),
but pixi-live2d-display does not expose a public
`setBlinkingInterval` setter — only the `beforeModelUpdate` event
hook is documented. Overriding the auto-blink driver from
`tickPreModel` every frame would conflict with the existing wink
gesture and is brittle when the library upgrades. Held until we either
swap blink drivers or upstream a setter.

**Sketched approach (when we revisit).**
- Replace the auto blink driver with a custom one that exposes a
  setter; or fork `EyeBlink.update` and own the parameters via
  `tickPreModel`.
- Map arousal -> blink-interval multiplier (e.g. 0.7-1.4 around the
  rig's authored mean) plus a small jitter so the cadence doesn't
  read as metronomic.
- Reuse the `avatar.expressiveness` slider so the user can dampen the
  blink modulation along with the rest of the body-language overlays.

**Open questions.**
- Is the cleanest path forking the `EyeBlink` controller or upstreaming
  a setter PR? The fork is faster, but means we own that surface
  forever.

---

## B7. Open-vocabulary touch gestures (model-invented, no config)

**Motivation.** The K31 touch family (`[[touch:KIND]]`) is a **fixed**
8-entry taxonomy — `wave / poke / boop / nudge / high_five / hug /
head_pat / cuddle` — hardcoded as a dict in
[`touch_gestures.py`](../../app/core/touch/touch_gestures.py) and a
hardcoded grammar string in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(`_TOUCH_GRAMMAR_ADDENDUM`). Aiko can only reach for the user in those
eight ways; a fist-bump, a hair-ruffle, a pinky-promise, tossing a
cookie over — all impossible. We want her to **invent** a fitting
physical beat in the moment rather than picking from a menu. Decision
(June 2026): keep this **on the model**, no user config and no
per-gesture authoring — and a custom gesture **does not need an emoji**;
the model can supply one if it wants, otherwise the badge renders
without.

**The enabling fact.** The frontend is already data-driven. The
`ReachChannel` lean-in reads `duration_ms` + `lean_amount` straight off
the `avatar_touch` WS payload, and the chat-bubble badge / persona
banner render `label` + `emoji` from the same frame. So a novel gesture
needs **zero frontend work** as long as the backend emits those fields.
The only hard limit is visual: the Alexia rig has no arbitrary-motion
param, so every custom gesture *animates* as the same generic lean —
the novelty lives in the **badge text**, not new art.

**Sketched approach.**
- **Stop dropping unknown kinds.** Today `get_gesture()` returns `None`
  for anything off-taxonomy and `TouchService.try_dispatch` rejects
  with `REASON_UNKNOWN_KIND`. Add a *synthesized* fallback: an unknown
  kind becomes a `TouchGesture` built on the fly — default
  `lean_amount` (~0.3), no overlays, `duration_ms` ~1500, no axes gate
  (or a mild closeness floor), and a **default cooldown + daily cap**
  shared across all custom kinds so a novel beat can't spam.
- **Label + optional emoji from the model.** Extend the tag parser
  (`extract_touch_commands` in
  [`response_text_service.py`](../../app/core/services/response_text_service.py))
  to accept an optional trailing segment, e.g. `[[touch:fist_bump]]`,
  `[[touch:fist_bump:🤜]]`, or `[[touch:fist_bump:bumped fists::🤜]]`.
  When no label is given, derive a readable one by humanizing the slug
  (`fist_bump` → "fist-bumped you" / "fist bump"); when no emoji is
  given, render the badge glyph-free. Built-in kinds keep their curated
  label/emoji and ignore the freeform segment.
- **Teach the grammar.** Add one or two lines to
  `_TOUCH_GRAMMAR_ADDENDUM` telling Aiko she may coin a new
  `[[touch:...]]` kind for a physical beat the eight built-ins don't
  cover, with an example, and that an emoji is optional. Persona block
  gets a sentence so coined gestures stay rare + earned, same posture
  as the built-ins.
- **Sanitize.** Open vocabulary means the model controls badge text:
  clamp kind/label length, restrict the slug charset, strip newlines,
  and cap custom dispatches per turn (the existing "≤ once a turn" rule
  already helps). Consider a small denylist so a "gesture" can't smuggle
  arbitrary prose into the transcript.

**Key files.**
[`app/core/touch/touch_gestures.py`](../../app/core/touch/touch_gestures.py)
(synthesized fallback + per-custom cooldown/cap),
[`app/core/services/response_text_service.py`](../../app/core/services/response_text_service.py)
(`extract_touch_commands` parser extension),
[`app/core/session/avatar_mixin.py`](../../app/core/session/avatar_mixin.py)
(`_emit_avatar_touch` payload already carries the fields — confirm it
forwards synthesized ones),
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(grammar addendum),
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
(one sentence). Frontend: none expected.

**Open questions.**
- Tag shape for the optional label + optional emoji — a single
  `[[touch:kind:label:emoji]]` with empty slots, or keep it minimal
  (`[[touch:kind]]` + auto-derived label) and let the model only ever
  add an emoji? Minimal is safer and matches "no config".
- Should custom gestures share **one** cooldown bucket (so the model
  can't cycle through ten novel kinds to dodge the per-kind cap), or
  get per-kind buckets like the built-ins? One shared bucket is the
  conservative default.
- Do custom gestures feed the K31 physical-budget cue
  (`render_touch_state_block`) the same way intimate built-ins do?

**Effort.** Medium.

---

## B8. Live "listening" face — visual backchannel while you type

**✅ Shipped.** Implemented as a `composing` flag on the `ui` store slice
(set by the chat composer on each keystroke, cleared on send / blur / a
2.5 s idle debounce), polled per gaze tick through `ChannelStoreSnapshot`.
Rather than a standalone channel (which would double-write params the
existing channels already own), the behaviour rides the two natural
owners: `GazeChannel` gains a typed-listening branch (eye-contact bias +
low-amplitude sway, outranking thinking drift), and `AmbientBodyChannel`'s
lean-in now also triggers on `composing`. Both decay out via their
existing `approach()` easing when the user stops typing. Amplitudes stay
small and `avatar.expressiveness`-scaled (lean-in). No backend cost, no
new rig params. Tests: `GazeChannel.test.ts` (typed-listening branch),
`AmbientBodyChannel.test.ts` (composing lean-in + relax), and
`ui.composing.test.ts` (store flag). The brow-flicker / anticipatory-nod
flourishes sketched below are a possible follow-up.

**Motivation.** H6/H7 cover *audible* backchannels in voice mode, but in typed
mode the avatar is socially dead while the user composes — she stares blankly
until the message lands. A real listener's face moves *while you talk*: a small
attentive head-tilt, a brow flicker, a tiny anticipatory nod. If the frontend
emits a lightweight "user is composing" signal (the same `composer_draft` / P7
typed-prefetch plumbing), the avatar can run a subtle **listening posture** —
gaze settles on the user, a low-amplitude attentive micro-motion — that decays
when the field goes idle and resolves into the real reaction when the message
arrives. Pure embodiment polish, no backend cost, and it makes typed mode feel
as *present* as voice. The whole risk is over-animating into the uncanny, so it's
low-amplitude and `avatar.expressiveness`-scaled like every other continuous
overlay.

**The enabling fact.** This is a channel, not a model change. A new
`ListeningChannel` (or a mode on `GazeChannel` / `AmbientBodyChannel`) reading a
`composing` boolean + idle timer needs no new rig parameters — it reuses head
angle, gaze focus, and breath amplitude already owned by existing channels.

**Key files.** A new channel under
[`web/src/live2d/channels/`](../../web/src/live2d/) hooking `tickPreModel`, a
`composing` flag plumbed from the composer in
[`ChatView.tsx`](../../web/src/components/ChatView.tsx) into the avatar engine
(via the store), capability-gated on head-angle / gaze params. Backend: none, or
at most reuse the typed-prefetch frame. Scale every amplitude by
`avatar.expressiveness`.

**Effort.** Small–medium (one channel + one frontend flag).

---

_Shipped avatar items (B1, B2, B4, B5, B6) live in [`shipped.md`](shipped.md) — see B4 for the Phase 5 close-out (new reactions, persona idiom fix, tail-wag breath boost, ear-wiggle override). B8 (live "listening" face) shipped — see the ✅ note in its section above. Open: B3 (blink-rate modulation by arousal) and B7 (open-vocabulary touch gestures)._
