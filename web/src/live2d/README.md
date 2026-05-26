# `web/src/live2d/` — Live2D engine + channel architecture

The Live2D component used to live entirely inside one ~1 300-line
React component (`Live2DAvatar.tsx`) with seven `useEffect` hooks
sharing refs, RAF loops, and overlapping pointer / store
subscriptions. That made it nearly impossible to reason about
*which* effect was writing *which* parameter on a given frame, and
unit-testing anything required spinning up jsdom + Pixi.

This module replaces that with an **event-driven channel
architecture**. The component shrinks to mount + Pixi setup +
dispose; everything that writes parameters lives in a
single-purpose channel registered on an `AvatarEngine`.

```
                 ┌───────────────────────────────────────────────┐
                 │              Live2DAvatar.tsx                 │
                 │   (Pixi setup + model load + engine boot)     │
                 └───────────────────────────────────────────────┘
                                       │
                  PixiLive2DAdapter ───┴─── WindowMouseSource
                                       │
                                ┌──────▼───────┐
                                │ AvatarEngine │
                                └──┬───┬───┬───┘
                                   │   │   │
            ┌──────────────────────┘   │   └──────────────────────┐
            │       fan-out events     │      RAF loops           │
            │                          │                          │
       Channels                  EngineState              StoreBridge
       (this dir)              (shared mutable          (Zustand → engine
                                 cross-channel)           dispatch events)
```

## Components

| File | Purpose |
|------|---------|
| `AvatarEngine.ts` | Owns the two RAF loops (tier-3 + gaze), the `beforeModelUpdate` hook, and fans store events out to every registered channel. Also manages `EngineState.exprSlotLockUntil` expiry. |
| `Live2DModelAdapter` (interface in `types.ts`) | Narrow surface every channel uses to drive the rig. `setParam`, `expression`, `motion`, `focus`, `onBeforeModelUpdate`. |
| `PixiLive2DAdapter.ts` | Production wrapper around `pixi-live2d-display`'s `Live2DModel`. Centralises the unsafe-cast plumbing in one place. |
| `WindowMouseSource.ts` | Production `MouseSource` — wires real `pointermove` / `focus` / `blur` events. |
| `StoreBridge.ts` | Subscribes to the Zustand store and turns each slice change into a discrete engine event (`dispatchReaction`, `dispatchOverlay`, …). Channels never touch the store directly. |
| `state.ts` | `EngineState` — shared mutable cross-channel coordination flags (e.g. `exprSlotLockUntil`, `tailWagBoostUntil`). |
| `math.ts` | `approach()` (critically-damped easing), `clamp()`. |
| `channels/` | Eight `AvatarChannel` implementations (one per concern). |
| `__fixtures__/` | `FakeAdapter`, `FakeClock`, `FakeMouseSource`, `ManualRaf`, `buildManifest` — the building blocks every channel test uses. |

## Channels

Each channel implements the `AvatarChannel` interface (see
`types.ts`). The engine fans events out only to channels that
implement the matching optional method, so adding a channel never
forces the engine to grow new event types.

| Channel | Hooks | What it does |
|---------|-------|--------------|
| `MotionChannel` | `onMotion`, `onTtsState` | LLM `[[motion:X]]` playback + auto-fire talk motions on TTS rising edge. |
| `OutfitChannel` | `tickTier3` | 3-way crossfade (`pajamas` / `pajamas_hooded` / `day`) with **additive** param-sum so shared params don't stomp each other during the transition. |
| `OverlayChannel` | `onOverlay`, `tickTier3` | `[[overlay:X]]` param pulses + `expr:`-bound pulses. Writes `EngineState.exprSlotLockUntil` for cross-channel coordination. |
| `LipsyncChannel` | `tickPreModel` | Writes `ParamMouthOpenY` on **`beforeModelUpdate`** so the motion-manager can't clobber it (see §5 of `docs/alexia-model-notes.md`). |
| `ExpressionChannel` | `onReaction`, `onVoiceMode`, `onBackchannel`, `onExpressionSlotReleased` | Persistent reaction expression + voice-mode override + transient backchannel overlay. |
| `GestureChannel` | `onOverlay`, `tickTier3` | `wink_left`, `wink_right`, `ear_wiggle` (per-frame drives) + `tail_wag` (sets a deadline read by `AmbientBodyChannel`). |
| `GazeChannel` | `tickGaze` | Mouse follow + conversation lock + thinking drift + idle break + micro-saccades. |
| `AmbientBodyChannel` | `onReaction`, `tickTier3` | Auto-blush, auto-sweat, cat-tail sine, body-language sums (lean-in, slump, excited bounce, breath, sass tilt). |

## Tick rates and write-discipline

The engine runs three independent loops; channels opt in to the
ones they care about:

- **`tickTier3`** — 60 Hz RAF for non-mouth parameter writes
  (envelopes, gestures, body language, outfit crossfade).
- **`tickGaze`** — 60 Hz RAF for `adapter.focus(x, y)`. Runs in
  its own loop so the gaze can sample mouse state at native
  refresh even if tier-3 work spikes.
- **`tickPreModel`** — fires inside the rig's
  `beforeModelUpdate` event. **Only** lipsync writes here — see
  `docs/alexia-model-notes.md` §5 for the motion-manager
  write-order trap that makes this hook mandatory for `ParamMouthOpenY`.

Discrete store changes (`onReaction`, `onMotion`, `onOverlay`, …)
fire immediately when the bridge sees a slice change — channels
don't need to poll snapshots for events. The
`getStoreSnapshot()` getter on `ChannelDeps` is for *per-tick*
state the channels do want to poll (mood, voiceMode, audioAmplitude).

## Cross-channel coordination

A small set of flags live on `EngineState` so channels can
coordinate without coupling:

- `exprSlotLockUntil` — `OverlayChannel` writes this when an
  `expr:`-bound pulse fires; `ExpressionChannel` reads it to defer
  reaction writes; the engine fans `onExpressionSlotReleased` when
  the deadline passes.
- `tailWagBoostUntil` — `GestureChannel` writes this on
  `[[overlay:tail_wag]]`; `AmbientBodyChannel` reads it to scale
  the cat-tail sine's freq + amp for the boost window.
- `lastReaction` — engine-internal dedup so
  `dispatchReaction("neutral")` followed by another
  `"neutral"` doesn't re-fan.

## Testing

Vitest runs in **Node** with no jsdom — channels are pure TS, the
adapter is an interface, and the test fixtures (`FakeAdapter`,
`FakeClock`, `FakeMouseSource`, `ManualRaf`) replace every
otherwise-fragile dependency.

```bash
cd web && npm test
```

Each channel has its own `*.test.ts` next to it. The whole
channel suite runs in well under a second on a laptop. The React
component itself is *not* under test — its surface area shrunk to
mount + Pixi boot + dispose, which is covered by manual smoke
testing.

## How to add a channel

1. Create `channels/MyChannel.ts` implementing `AvatarChannel`.
2. Pick the lifecycle hooks you need (`attach`, `detach`,
   `onReaction`, `tickTier3`, …). Don't implement hooks you don't
   use — the engine skips them.
3. If you need cross-channel coordination, add a field to
   `EngineState` (and document **who writes** vs. **who reads**).
4. If you need a new store slice, add it to
   `ChannelStoreSnapshot` and dispatch from `StoreBridge`.
5. Write `MyChannel.test.ts` next to it. Use `FakeAdapter` + the
   relevant fixtures; don't reach for jsdom.
6. Register the channel in `Live2DAvatar.tsx`'s `engine.register(...)`.
