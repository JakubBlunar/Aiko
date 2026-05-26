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

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, LogicalSize, Manager, WindowEvent,
};

const MAIN_LABEL: &str = "main";
const PERSONA_LABEL: &str = "persona";

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
