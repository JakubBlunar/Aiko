# Presence + activity awareness

This doc captures the privacy posture of the typed-mode proactive
nudge gate AND the optional desktop-only activity-awareness feature.
Both signals piggyback on the same WebSocket but answer different
questions:

- **Presence** (always on, both browser + desktop) — *"Is Jacob
  actually looking at the app right now?"* Used to pause the typed-
  mode proactive timer so a backgrounded UI never gets nudged.
- **Activity awareness** (desktop-only, opt-in, off by default) —
  *"What is happening on the machine?"* Cheap collectors (foreground
  app, OS idle, session lock) push versioned envelopes to the server,
  which redacts then stores them. The prompt still gets **app name
  only**. Titles are stored only for apps on a positive allowlist.
  This is C6 phases 1–2 (collection). Interpretation, cues, memories,
  UIA, and a live tool-pass pull are later consumers — see
  [`docs/personality-backlog/proactive.md`](personality-backlog/proactive.md#c6-companion-mode--the-desktop-as-a-sensory-channel).

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

A background `CollectorRuntime` in the Tauri process polls cheap
sources on its own thread. JS **never awaits** an OS poll. Envelopes
ride `activity://sample` → `{ type: "user_activity", envelope }`.

```jsonc
{
  "type": "user_activity",
  "envelope": {
    "v": 1,
    "at": "2026-08-30T19:01:02Z",
    "source": "foreground",   // or "idle" | "lock"
    "tier": "cheap",
    "subject": { "app": "Code", "title": "rag_store.py — assistant", "surface_id": "…" },
    "signal": { "kind": "focus" },  // focus | idle | lock | unlock
    "payload": {}
  }
}
```

- `subject.surface_id` is a hash of the platform window id, **not**
  the raw HWND.
- `subject.title` is omitted unless the app is on
  `activity.title_allowlist`. Rust may *read* the title (it is
  already in the `active-win-pos-rs` struct) but only *emits* it for
  allowlisted apps. Python re-applies the same list and drops
  URL-shaped titles before persist.
- Self-app names (`Aiko`, `aiko-desktop`) coerce to a null app so
  Aiko is not told "Jacob is in Aiko".
- The legacy `{ type: "user_activity", app: "Code" }` frame is still
  accepted (live cache only, not stored).
- Unknown envelope `v` and unknown `source` are **dropped** on the
  server. That is the safety property that lets a later `uia` source
  land as one Rust plugin + one Python handler without a window
  where raw trees hit disk.

## What is NOT sent (and not stored)

- Titles of apps that are not on the allowlist
- URL-shaped titles (even when the app is allowlisted)
- Process IDs / executable paths
- Per-window geometry
- Keystrokes, mouse moves, clipboard contents
- Anything from non-foreground windows
- UIA trees (not implemented; the plug exists, the walker does not)

The prompt `activity_block` remains **app name only**. Stored titles
are for later interpretation, not for this turn's system prompt.

## Defence in depth

Multiple layers must all fail for a privacy regression to leak data:

1. **Collector isolation.** OS reads run on a background thread.
   One source panicking is catch-and-skip. Disabled → no OS reads.
2. **Allowlist at emit.** Titles leave Rust only for named apps.
3. **Client-side filter.** `normaliseActiveAppName` strips `.exe`
   and coerces self-app matches to `null`. Browser shells never
   subscribe (`isTauri() === false`).
4. **Settings gate.** Toggle off idles the collector, sends one
   empty frame, and the server drops envelopes + clears the live
   cache.
5. **Server-side redact-before-persist.** Unknown `source` / `v`
   never hit SQLite. Python re-applies the allowlist and URL strip.
   `set_user_active_app` still takes **app name only**; ingest is a
   sibling so a buggy client cannot write titles into the prompt
   cache.
6. **PATCH-time clear.** Flipping the toggle off via
   `PATCH /api/settings` calls `set_user_active_app(None)`.
7. **Render-time gate.** `_render_activity_block()` returns `""`
   whenever the toggle is off, and never interpolates a title.
8. **Retention.** `activity_events` / `activity_sessions` prune
   after `memory.activity_keep_days` (default 30). An append-only
   event table without retention is H33 shape 14.

**Idle-gate invariant.** `_touch_user_activity` is called from a
chat turn only. `user_activity` frames must not reset it:
coding-not-chatting has to look idle to the scheduler. Held by
`tests/test_activity_touch_invariant.py`.

## Verifying what's being shared

Open Settings → "Activity awareness (desktop)". When the toggle is
on and you're inside the Tauri shell, a live "Currently sees:
\<App\> — \<title\>" readout shows the **literal stored** app +
title (title empty unless the app is allowlisted). Browsers see
the toggle but the readout reads "Browser shell — desktop app
required".

MCP `get_activity_timeline` dumps recent sessions, the last
envelope, the registered sources, the allowlist, and the prune
watermark.

## Disabling

Toggle off in Settings → "Activity awareness (desktop)". The
collector skips OS reads, the next prompt has no activity block,
and the cached `_user_active_app` is cleared as part of the same
PATCH. Existing SQLite rows remain until the prune worker ages
them out (or you clear the db).

## Platform support

- **Windows** — foreground via `active-win-pos-rs`; idle via
  `GetLastInputInfo`; lock via `OpenInputDesktop`.
- **macOS / Linux X11** — foreground only; idle and lock degrade
  to absent signals.
- **Linux Wayland** — foreground is best-effort (`Err` → no
  sample), same as before.

## Why presence is *not* opt-in

Presence is a behavioural gate, not a data-sharing feature. The only
value it produces is "should the typed-mode proactive timer fire
right now?". Nothing about the user's environment leaves the client
besides a single boolean. Voice mode is intentionally exempt — when
Live mode is on the user may be away from the screen but very much
present in conversation, so the voice-mode `_maybe_proactive` loop
ignores `_user_present` entirely.

## Where collection happens

- **Presence**: only on transitions (event-driven via
  `document.visibilitychange`, `window.focus`/`blur`, and Tauri's
  `tauri://focus`/`tauri://blur`). Debounced ~500 ms client-side so
  a rapid alt-tab doesn't spam the WS.
- **Activity awareness**: a background collector thread (~1 s
  cheap poll, emit on change only). JS listens and forwards.
  Torn down (no OS reads) the moment the settings toggle flips off.
