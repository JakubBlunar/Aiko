//! v1 cheap sources: foreground app, OS idle, session lock.
//!
//! Windows implements idle (`GetLastInputInfo`) and lock
//! (`OpenInputDesktop`). macOS / Linux keep foreground via
//! `active-win-pos-rs` and degrade idle/lock to absent, the same way
//! Wayland already degrades the app name to `None`.

use super::envelope::{
    app_on_allowlist, hash_surface_id, normalise_app_name, Envelope, Isolation, Subject, Tier,
};
use super::source::{ActivitySource, TickContext};

const IDLE_THRESHOLD_MS: u64 = 60_000;

pub fn cheap_sources() -> Vec<Box<dyn ActivitySource>> {
    vec![
        Box::new(ForegroundSource::default()),
        Box::new(IdleSource::default()),
        Box::new(LockSource::default()),
    ]
}

#[derive(Default)]
struct ForegroundSource {
    last_key: Option<String>,
}

impl ActivitySource for ForegroundSource {
    fn name(&self) -> &'static str {
        "foreground"
    }
    fn tier(&self) -> Tier {
        Tier::Cheap
    }
    fn isolation(&self) -> Isolation {
        Isolation::Shared
    }

    fn tick(&mut self, ctx: &TickContext<'_>) -> Option<Envelope> {
        let mut subject = match read_foreground() {
            Some(s) => Subject {
                app: s.app,
                title: s.title,
                surface_id: s.surface_id,
            },
            None => Subject {
                app: None,
                title: None,
                surface_id: None,
            },
        };
        if let Some(app) = subject.app.as_deref() {
            if !app_on_allowlist(app, ctx.allowlist) {
                subject.title = None;
            }
        } else {
            subject.title = None;
        }
        let key = format!(
            "{}|{}|{}",
            subject.app.as_deref().unwrap_or(""),
            subject.surface_id.as_deref().unwrap_or(""),
            subject.title.as_deref().unwrap_or(""),
        );
        if self.last_key.as_deref() == Some(key.as_str()) {
            return None;
        }
        self.last_key = Some(key);
        Some(Envelope::new(
            "foreground",
            Tier::Cheap,
            "focus",
            subject,
            serde_json::json!({}),
        ))
    }

    fn reset(&mut self) {
        self.last_key = None;
    }
}

struct ForegroundRead {
    app: Option<String>,
    title: Option<String>,
    surface_id: Option<String>,
}

fn read_foreground() -> Option<ForegroundRead> {
    match active_win_pos_rs::get_active_window() {
        Ok(window) => {
            let app = normalise_app_name(&window.app_name);
            let title_raw = window.title.trim();
            let title = if title_raw.is_empty() {
                None
            } else {
                Some(title_raw.to_string())
            };
            Some(ForegroundRead {
                app,
                title,
                surface_id: hash_surface_id(&window.window_id),
            })
        }
        Err(_) => None,
    }
}

#[derive(Default)]
struct IdleSource {
    last_idle: Option<bool>,
}

impl ActivitySource for IdleSource {
    fn name(&self) -> &'static str {
        "idle"
    }
    fn tier(&self) -> Tier {
        Tier::Cheap
    }

    fn tick(&mut self, _ctx: &TickContext<'_>) -> Option<Envelope> {
        let idle = os_idle_ms().map(|ms| ms >= IDLE_THRESHOLD_MS);
        let Some(is_idle) = idle else {
            return None;
        };
        if self.last_idle == Some(is_idle) {
            return None;
        }
        self.last_idle = Some(is_idle);
        if !is_idle {
            // Return-from-idle is a foreground focus event. Only the
            // crossing *into* idle is this source's signal.
            return None;
        }
        Some(Envelope::new(
            "idle",
            Tier::Cheap,
            "idle",
            Subject {
                app: None,
                title: None,
                surface_id: None,
            },
            serde_json::json!({ "idle": true }),
        ))
    }

    fn reset(&mut self) {
        self.last_idle = None;
    }
}

#[derive(Default)]
struct LockSource {
    last_locked: Option<bool>,
}

impl ActivitySource for LockSource {
    fn name(&self) -> &'static str {
        "lock"
    }
    fn tier(&self) -> Tier {
        Tier::Cheap
    }

    fn tick(&mut self, _ctx: &TickContext<'_>) -> Option<Envelope> {
        let Some(locked) = os_session_locked() else {
            return None;
        };
        if self.last_locked == Some(locked) {
            return None;
        }
        self.last_locked = Some(locked);
        let kind = if locked { "lock" } else { "unlock" };
        Some(Envelope::new(
            "lock",
            Tier::Cheap,
            kind,
            Subject {
                app: None,
                title: None,
                surface_id: None,
            },
            serde_json::json!({ "locked": locked }),
        ))
    }

    fn reset(&mut self) {
        self.last_locked = None;
    }
}

#[cfg(windows)]
fn os_idle_ms() -> Option<u64> {
    use windows::Win32::System::SystemInformation::GetTickCount;
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};

    let mut info = LASTINPUTINFO {
        cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32,
        dwTime: 0,
    };
    let ok = unsafe { GetLastInputInfo(&mut info) };
    if !ok.as_bool() {
        return None;
    }
    let now = unsafe { GetTickCount() };
    Some(u64::from(now.wrapping_sub(info.dwTime)))
}

#[cfg(not(windows))]
fn os_idle_ms() -> Option<u64> {
    None
}

#[cfg(windows)]
fn os_session_locked() -> Option<bool> {
    use windows::Win32::System::StationsAndDesktops::{
        CloseDesktop, OpenInputDesktop, DESKTOP_CONTROL_FLAGS, DESKTOP_SWITCHDESKTOP,
    };

    match unsafe { OpenInputDesktop(DESKTOP_CONTROL_FLAGS(0), false, DESKTOP_SWITCHDESKTOP) } {
        Ok(desk) => {
            let _ = unsafe { CloseDesktop(desk) };
            Some(false)
        }
        Err(_) => Some(true),
    }
}

#[cfg(not(windows))]
fn os_session_locked() -> Option<bool> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cheap_sources_are_shared_foreground_idle_lock() {
        let sources = cheap_sources();
        let names: Vec<&str> = sources.iter().map(|s| s.name()).collect();
        assert_eq!(names, vec!["foreground", "idle", "lock"]);
        assert!(sources.iter().all(|s| s.isolation() == Isolation::Shared));
        assert!(sources.iter().all(|s| s.tier() == Tier::Cheap));
    }
}
