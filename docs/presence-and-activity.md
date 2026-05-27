# Presence + activity awareness

This doc captures the privacy posture of the typed-mode proactive
nudge gate AND the optional desktop-only activity-awareness feature.
Both signals piggyback on the same WebSocket but answer different
questions:

- **Presence** (always on, both browser + desktop) — *"Is Jacob
  actually looking at the app right now?"* Used to pause the typed-
  mode proactive timer so a backgrounded UI never gets nudged.
- **Activity awareness** (desktop-only, opt-in, off by default) —
  *"Which application is in the foreground?"* Surfaced as an inner-
  life cue ("Jacob is currently working in Cursor") so Aiko can
  reference it naturally, NOT to surveil.

## What gets sent over the wire

### Presence

```jsonc
// Client → server, on every visibility / focus change (debounced 500 ms)
{ "type": "presence", "visible": true | false }
```

- `visible` is the AND-fold of:
  - `document.visibilityState === "visible"` (the browser tab is
    not hidden / minimised).
  - `document.hasFocus()` (the page itself has focus).
  - **Desktop only**: `tauri://focus` / `tauri://blur` events on
    the webview window (covers "user alt-tabbed to VS Code").
- The client AND-folds the signals so the backend gets one boolean.
  No metadata about *which* signal flipped is sent.

### Activity awareness

```jsonc
// Client → server, every 5 s when enabled (only on diff)
{ "type": "user_activity", "app": "Code" }
{ "type": "user_activity", "app": null }   // unknown / self-app
```

- The Rust command [`get_active_app`](../web/src-tauri/src/lib.rs)
  reads ONLY `active_win_pos_rs::get_active_window().app_name`. We
  *never* read `w.title`, the window-title field. That is the
  load-bearing privacy decision.
- The frontend strips a trailing `.exe` (so Windows / macOS /
  Linux reports collapse to one bucket) and coerces "Aiko" /
  "aiko-desktop" to `null` so Aiko isn't told "Jacob is in Aiko".

## What is NOT sent

- Window titles
- URLs (browser address bar)
- File names of open documents
- Process IDs / executable paths
- Per-window geometry
- Keystrokes, mouse moves, clipboard contents
- Anything from non-foreground windows

## Defence in depth

Multiple layers must all fail for a privacy regression to leak data:

1. **Rust command boundary.** `get_active_app` returns `Option<String>`;
   the only string it reads off the underlying struct is `app_name`.
2. **Client-side filter.** `normaliseActiveAppName` strips `.exe`,
   trims whitespace, and coerces self-app matches to `null`. It also
   bails out of the polling loop entirely on the browser
   (`isTauri() === false`) so a regular Chrome tab can't even be
   tricked into sending activity events.
3. **Settings gate.** When `activity.awareness_enabled` is `false`,
   the polling loop is a no-op AND the React hook explicitly fires
   one `user_activity: null` frame on disable so the backend's
   cached value drops immediately.
4. **Server-side gate.**
   `SessionController.set_user_active_app(app)` short-circuits to
   `_user_active_app = None` when the toggle is off. So even if a
   buggy or rogue client kept emitting events, no value would land.
5. **PATCH-time clear.** Flipping the toggle off via
   `PATCH /api/settings` calls `set_user_active_app(None)` server-
   side so a stale "Jacob is in Discord" line can't survive the
   transition into the next prompt.
6. **Render-time gate.** `_render_activity_block()` returns `""`
   whenever `activity_awareness_enabled` is false, regardless of
   whatever's in `_user_active_app`. Belt + braces with #4 in case
   the toggle was flipped between the setter call and this render.

## Verifying what's being shared

Open Settings → "Activity awareness (desktop)". When the toggle is on
and you're inside the Tauri shell, a live "Currently sees: \<App\>"
readout shows the latest app name the polling loop captured. This is
the literal string that lands in the prompt. Browsers see the toggle
but the readout reads "Browser shell — desktop app required to share
foreground state" so it's clear the toggle is a no-op there.

## Disabling

Toggle off in Settings → "Activity awareness (desktop)". The next
prompt build will not include the activity block. The cached
`_user_active_app` is cleared server-side as part of the same PATCH.

## Platform support

- **Windows / macOS** — fully supported by `active-win-pos-rs`.
- **Linux X11** — supported.
- **Linux Wayland** — best-effort. `active-win-pos-rs` returns `Err`
  on most Wayland sessions; the Rust command maps that to `Ok(None)`
  so the activity block silently degrades to "no signal" rather
  than producing "Jacob is in (unknown)".

## Why presence is *not* opt-in

Presence is a behavioural gate, not a data-sharing feature. The only
value it produces is "should the typed-mode proactive timer fire
right now?". Nothing about the user's environment leaves the client
besides a single boolean. Voice mode is intentionally exempt — when
Live mode is on the user may be away from the screen but very much
present in conversation, so the voice-mode `_maybe_proactive` loop
ignores `_user_present` entirely.

## Where the polling happens

- **Presence**: only on transitions (event-driven via
  `document.visibilitychange`, `window.focus`/`blur`, and Tauri's
  `tauri://focus`/`tauri://blur`). Debounced ~500 ms client-side so
  a rapid alt-tab doesn't spam the WS.
- **Activity awareness**: a 5 s `setInterval` on the desktop only,
  pinging the Tauri command and emitting a WS event ONLY when the
  app name differs from the last sent value. Loop is torn down the
  moment the settings toggle flips off.
