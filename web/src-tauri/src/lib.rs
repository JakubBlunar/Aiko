//! Aiko desktop shell — Tauri 2 wiring.
//!
//! Two windows are declared in [`tauri.conf.json`]: `main` (full SPA at `/`)
//! and `persona` (minimal HUD at `index.html#/persona`, transparent +
//! frameless + always-on-top, hidden on launch). The persona window opens
//! either from the main window's top-bar button or from the system-tray
//! icon menu.
//!
//! All commands defined here are pure window-management shims; the actual
//! application state lives in the external Python backend that the
//! webviews connect to over WebSocket. See `docs/tauri-shell.md` for the
//! full architecture rationale.

#[cfg(target_os = "macos")]
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, LogicalSize, Manager, WindowEvent,
};

const MAIN_LABEL: &str = "main";
const PERSONA_LABEL: &str = "persona";

/// Where the FastAPI backend listens. Mirrors ``config/default.json`` and
/// the dev-mode Vite proxy targets.
const BACKEND_HEALTH_URL: &str = "http://127.0.0.1:6275/api/health";

/// Total time the sidecar waits for the backend to answer before
/// surfacing a "couldn't start" error. Generous because cold-starting the
/// venv + importing the ML stack on a busy laptop takes ~10-15 s.
const BACKEND_BOOT_TIMEOUT: Duration = Duration::from_secs(25);

/// Event name fired whenever the persona window's visibility changes.
/// Payload is a single ``bool`` (``true`` = now visible). Listened to
/// in the main window's React tree so it can hide the redundant avatar
/// rail when Aiko has been popped out into the floating window.
const PERSONA_VISIBILITY_EVENT: &str = "persona-visibility";

// ── Internal helpers ────────────────────────────────────────────────────

/// Show + focus the persona window AND emit the visibility event so the
/// main window (or any other listener) can react. Centralised so the
/// tray menu, the top-bar invoke, and the explicit close-button hide
/// all funnel through the same notification path.
fn show_persona_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(PERSONA_LABEL) {
        let _ = window.show();
        let _ = window.set_focus();
        let _ = app.emit(PERSONA_VISIBILITY_EVENT, true);
    }
}

/// Hide (NOT destroy) the persona window so reopening it from the tray or
/// the main window's top-bar button is instant. The webview keeps its WS
/// connection alive in the background — that's intentional, the persona
/// rejoins state-in-progress instead of replaying `hello`.
fn hide_persona_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(PERSONA_LABEL) {
        let _ = window.hide();
        let _ = app.emit(PERSONA_VISIBILITY_EVENT, false);
    }
}

// ── Window-management commands ──────────────────────────────────────────

#[tauri::command]
fn open_persona(app: AppHandle) {
    show_persona_window(&app);
}

#[tauri::command]
fn close_persona(app: AppHandle) {
    hide_persona_window(&app);
}

/// Synchronous probe used by the main window on mount to seed its
/// initial "is the persona window visible right now?" state, before the
/// first ``persona-visibility`` event lands.
#[tauri::command]
fn is_persona_visible(app: AppHandle) -> bool {
    app.get_webview_window(PERSONA_LABEL)
        .and_then(|w| w.is_visible().ok())
        .unwrap_or(false)
}

/// Resize the persona window. Called from the persona webview itself
/// after a `desktop_settings_changed` WS event lands; the source of truth
/// stays in the Python backend's `config/user.json`.
#[tauri::command]
fn set_persona_geometry(app: AppHandle, width: u32, height: u32) -> Result<(), String> {
    let window = app
        .get_webview_window(PERSONA_LABEL)
        .ok_or_else(|| "persona window not found".to_string())?;
    window
        .set_size(LogicalSize::new(width as f64, height as f64))
        .map_err(|err| err.to_string())
}

/// Toggle whether the persona window floats above other apps. Mirrors the
/// settings-drawer checkbox.
#[tauri::command]
fn set_persona_always_on_top(app: AppHandle, on_top: bool) -> Result<(), String> {
    let window = app
        .get_webview_window(PERSONA_LABEL)
        .ok_or_else(|| "persona window not found".to_string())?;
    window
        .set_always_on_top(on_top)
        .map_err(|err| err.to_string())
}

// ── Activity awareness ──────────────────────────────────────────────────

/// Return the foreground application's *name only* (never the window
/// title or URL). Polled from the React side every few seconds when the
/// user has opted in to activity awareness.
///
/// Privacy posture is enforced at every layer; this command sits at the
/// outermost boundary:
///   - We deliberately read ``w.app_name`` and never ``w.title``. Adding
///     titles would leak bank URLs / file names / chat partner names; the
///     trade-off is documented in ``docs/presence-and-activity.md``.
///   - On Wayland sessions and unsupported platforms ``active-win-pos-rs``
///     returns ``Err``; we map that to ``Ok(None)`` so the React side can
///     simply send ``null`` and the backend silently skips the inner-life
///     block instead of producing "Jacob is in (unknown)".
///   - Self-app filtering (so Aiko isn't told "Jacob is in Aiko") happens
///     on the React side because the bundle name is what the frontend
///     already knows.
#[tauri::command]
fn get_active_app() -> Result<Option<String>, String> {
    match active_win_pos_rs::get_active_window() {
        Ok(window) => {
            let name = window.app_name.trim().to_string();
            if name.is_empty() {
                Ok(None)
            } else {
                Ok(Some(name))
            }
        }
        Err(_) => Ok(None),
    }
}

// ── Backend sidecar ─────────────────────────────────────────────────────

/// Poll the backend's health endpoint with a short HTTP HEAD-style GET.
/// Returns ``true`` as soon as we get any 2xx response; treats every
/// other outcome (connection refused, timeout, non-2xx) as "not ready".
fn backend_is_up() -> bool {
    // We avoid pulling in reqwest just for this — a one-shot
    // ``curl --fail --max-time`` is plenty and is always present on
    // macOS. ``--silent`` keeps the log clean.
    Command::new("curl")
        .args([
            "--silent",
            "--fail",
            "--max-time",
            "1",
            BACKEND_HEALTH_URL,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Resolve the macOS-style ``$HOME/Library/Application Support/Aiko`` path.
///
/// Only the macOS sidecar spawn path consumes this helper; gating it
/// behind ``#[cfg(target_os = "macos")]`` keeps Windows / Linux builds
/// from generating a dead-code warning.
#[cfg(target_os = "macos")]
fn aiko_support_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    let mut p = PathBuf::from(home);
    p.push("Library");
    p.push("Application Support");
    p.push("Aiko");
    Some(p)
}

/// Spawn the Python backend as a detached child. Errors bubble up to
/// ``ensure_backend_running`` which forwards them to the frontend so the
/// React side can show a real "couldn't launch the backend" message
/// instead of a silent WS-connect failure.
#[cfg(target_os = "macos")]
fn spawn_backend_sidecar() -> Result<(), String> {
    let support = aiko_support_dir()
        .ok_or_else(|| "could not resolve Application Support directory".to_string())?;

    let script = support.join("scripts").join("macos-start-backend.sh");
    let fallback_script = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|p| p.to_path_buf()))
        .map(|p| p.join("scripts/macos-start-backend.sh"));

    let chosen = if script.exists() {
        script
    } else if let Some(fb) = fallback_script.filter(|p| p.exists()) {
        fb
    } else {
        return Err(format!(
            "backend sidecar script not found at {:?}",
            script
        ));
    };

    Command::new("/bin/bash")
        .arg(chosen)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| format!("failed to spawn backend sidecar: {err}"))?;
    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn spawn_backend_sidecar() -> Result<(), String> {
    // On other platforms the assumption is that the developer runs
    // ``python -m app.web`` themselves (the historical workflow). We
    // still expose the command so the JS bootstrap gate works
    // identically, it just doesn't try to spawn anything.
    Ok(())
}

/// Frontend-facing bootstrap gate. Returns ``Ok(())`` once the backend
/// answers ``/api/health`` (whether it was already running or this call
/// just started it), or ``Err`` with a clear message after the boot
/// timeout. The React side calls this before connecting the WS.
#[tauri::command]
async fn ensure_backend_running() -> Result<(), String> {
    if backend_is_up() {
        return Ok(());
    }
    spawn_backend_sidecar()?;
    let deadline = Instant::now() + BACKEND_BOOT_TIMEOUT;
    loop {
        if backend_is_up() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(format!(
                "Aiko backend did not answer {BACKEND_HEALTH_URL} within {}s. \
                 Check ~/Library/Application Support/Aiko/logs/backend.log.",
                BACKEND_BOOT_TIMEOUT.as_secs()
            ));
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

// ── Setup ───────────────────────────────────────────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            open_persona,
            close_persona,
            is_persona_visible,
            set_persona_geometry,
            set_persona_always_on_top,
            get_active_app,
            ensure_backend_running,
        ])
        .setup(|app| {
            install_tray(app.handle())?;
            wire_persona_close_to_hide(app.handle());
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running aiko desktop shell");
}

/// Build the system-tray icon + menu. The menu mirrors what the user can
/// already do from inside the main window (open / close persona) plus a
/// proper Quit entry that exits the entire app.
fn install_tray(app: &AppHandle) -> tauri::Result<()> {
    let show_main = MenuItem::with_id(app, "show_main", "Show main window", true, None::<&str>)?;
    let show_persona = MenuItem::with_id(
        app,
        "show_persona",
        "Show persona window",
        true,
        None::<&str>,
    )?;
    let hide_persona = MenuItem::with_id(
        app,
        "hide_persona",
        "Hide persona window",
        true,
        None::<&str>,
    )?;
    let separator = MenuItem::with_id(app, "separator", "—", false, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Aiko", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&show_main, &show_persona, &hide_persona, &separator, &quit],
    )?;

    let _tray = TrayIconBuilder::with_id("aiko-tray")
        .tooltip("Aiko")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show_main" => {
                if let Some(window) = app.get_webview_window(MAIN_LABEL) {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            "show_persona" => {
                show_persona_window(app);
            }
            "hide_persona" => {
                hide_persona_window(app);
            }
            "quit" => {
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            // Left-click on the tray toggles the main window's visibility —
            // a familiar shortcut on every desktop OS.
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if let Some(window) = tray.app_handle().get_webview_window(MAIN_LABEL) {
                    if window.is_visible().unwrap_or(false) {
                        let _ = window.hide();
                    } else {
                        let _ = window.show();
                        let _ = window.set_focus();
                    }
                }
            }
        })
        .build(app)?;
    Ok(())
}

/// Intercept the persona window's close button so the X icon hides the
/// window instead of destroying it. The main window's close exits the app
/// (default Tauri behaviour); we leave that alone. Hiding via the X
/// button funnels through the same emit path the tray + top-bar
/// commands use, so the main window's UI flips back to "show avatar
/// inline" without any other plumbing.
fn wire_persona_close_to_hide(app: &AppHandle) {
    if let Some(persona) = app.get_webview_window(PERSONA_LABEL) {
        let app_clone = app.clone();
        persona.on_window_event(move |event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                hide_persona_window(&app_clone);
            }
        });
    }
}
