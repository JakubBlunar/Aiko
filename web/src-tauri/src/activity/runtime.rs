//! Isolated collector: cheap sources on one thread, dedicated isolation
//! as an API even when unused (UIA later).
//!
//! Nothing here may stall the UI thread or the WebSocket send path.
//! One source failing is catch-and-skip.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Emitter};

use super::envelope::{Envelope, Isolation, SAMPLE_EVENT};
use super::source::{ActivitySource, EscalationBus, FocusChanged, TickContext};
use super::sources::cheap_sources;

const POLL_INTERVAL: Duration = Duration::from_millis(1000);

#[derive(Debug, Clone)]
pub struct CollectorConfig {
    pub enabled: bool,
    pub title_allowlist: Vec<String>,
}

impl Default for CollectorConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            title_allowlist: Vec::new(),
        }
    }
}

#[derive(Clone)]
pub struct CollectorHandle {
    config: Arc<Mutex<CollectorConfig>>,
    stop: Arc<AtomicBool>,
}

impl CollectorHandle {
    pub fn set_config(&self, enabled: bool, title_allowlist: Vec<String>) {
        let mut cfg = self.config.lock().unwrap_or_else(|e| e.into_inner());
        cfg.enabled = enabled;
        cfg.title_allowlist = title_allowlist;
    }
}

impl Drop for CollectorHandle {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

pub fn start(app: AppHandle) -> CollectorHandle {
    let config = Arc::new(Mutex::new(CollectorConfig::default()));
    let stop = Arc::new(AtomicBool::new(false));
    let handle = CollectorHandle {
        config: config.clone(),
        stop: stop.clone(),
    };
    let bus = EscalationBus::new();
    let (shared, dedicated) = partition_sources(cheap_sources());
    for source in dedicated {
        spawn_dedicated(source, config.clone(), stop.clone(), app.clone(), bus.clone());
    }
    let _ = thread::Builder::new()
        .name("aiko-activity".into())
        .spawn(move || shared_loop(app, config, stop, shared, bus));
    handle
}

pub fn partition_sources(
    sources: Vec<Box<dyn ActivitySource>>,
) -> (Vec<Box<dyn ActivitySource>>, Vec<Box<dyn ActivitySource>>) {
    let mut shared = Vec::new();
    let mut dedicated = Vec::new();
    for source in sources {
        if source.isolation() == Isolation::Dedicated {
            dedicated.push(source);
        } else {
            shared.push(source);
        }
    }
    (shared, dedicated)
}

/// Own thread for an escalated source. Unused in v1. A hung COM call
/// cannot be cancelled; leaking *this* thread is the contract.
pub fn spawn_dedicated(
    source: Box<dyn ActivitySource>,
    config: Arc<Mutex<CollectorConfig>>,
    stop: Arc<AtomicBool>,
    app: AppHandle,
    bus: EscalationBus,
) {
    let wrapped = Arc::new(Mutex::new(source));
    if wrapped
        .lock()
        .map(|s| s.tier() == super::envelope::Tier::Escalated)
        .unwrap_or(false)
    {
        bus.subscribe(wrapped.clone());
    }
    let _ = thread::Builder::new()
        .name("aiko-activity-dedicated".into())
        .spawn(move || dedicated_loop(app, config, stop, wrapped, bus));
}

fn shared_loop(
    app: AppHandle,
    config: Arc<Mutex<CollectorConfig>>,
    stop: Arc<AtomicBool>,
    mut sources: Vec<Box<dyn ActivitySource>>,
    bus: EscalationBus,
) {
    let mut was_enabled = false;
    while !stop.load(Ordering::Relaxed) {
        let snapshot = config.lock().unwrap_or_else(|e| e.into_inner()).clone();
        if snapshot.enabled {
            was_enabled = true;
            tick_shared(&mut sources, &snapshot.title_allowlist, &bus, |env| {
                emit_sample(&app, &env);
            });
        } else if was_enabled {
            was_enabled = false;
            for source in &mut sources {
                source.reset();
            }
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn dedicated_loop(
    app: AppHandle,
    config: Arc<Mutex<CollectorConfig>>,
    stop: Arc<AtomicBool>,
    source: Arc<Mutex<Box<dyn ActivitySource>>>,
    _bus: EscalationBus,
) {
    let mut was_enabled = false;
    while !stop.load(Ordering::Relaxed) {
        let snapshot = config.lock().unwrap_or_else(|e| e.into_inner()).clone();
        if snapshot.enabled {
            was_enabled = true;
            let ctx = TickContext {
                allowlist: &snapshot.title_allowlist,
            };
            let envelope = match source.lock() {
                Ok(mut inner) => catch_tick(&mut **inner, &ctx),
                Err(_) => None,
            };
            if let Some(env) = envelope {
                emit_sample(&app, &env);
            }
        } else if was_enabled {
            was_enabled = false;
            if let Ok(mut inner) = source.lock() {
                inner.reset();
            }
        }
        thread::sleep(POLL_INTERVAL);
    }
}

pub fn tick_shared(
    sources: &mut [Box<dyn ActivitySource>],
    allowlist: &[String],
    bus: &EscalationBus,
    mut emit: impl FnMut(Envelope),
) {
    let ctx = TickContext { allowlist };
    for source in sources.iter_mut() {
        let Some(env) = catch_tick(&mut **source, &ctx) else {
            continue;
        };
        if env.source == "foreground" && env.signal.kind == "focus" {
            if let Some(app) = env.subject.app.clone() {
                bus.publish(FocusChanged {
                    surface_id: env.subject.surface_id.clone().unwrap_or_default(),
                    app,
                    title: env.subject.title.clone(),
                    at: env.at.clone(),
                });
            }
        }
        emit(env);
    }
}

fn catch_tick(source: &mut dyn ActivitySource, ctx: &TickContext<'_>) -> Option<Envelope> {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| source.tick(ctx))) {
        Ok(env) => env,
        Err(_) => None,
    }
}

fn emit_sample(app: &AppHandle, env: &Envelope) {
    let _ = app.emit(SAMPLE_EVENT, env);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::activity::envelope::{Subject, Tier};
    use crate::activity::source::ActivitySource;

    struct CountingSource {
        isolation: Isolation,
        ticks: usize,
    }

    impl ActivitySource for CountingSource {
        fn name(&self) -> &'static str {
            "count"
        }
        fn tier(&self) -> Tier {
            Tier::Cheap
        }
        fn isolation(&self) -> Isolation {
            self.isolation
        }
        fn tick(&mut self, _ctx: &TickContext<'_>) -> Option<Envelope> {
            self.ticks += 1;
            Some(Envelope::new(
                "foreground",
                Tier::Cheap,
                "focus",
                Subject {
                    app: Some("Code".into()),
                    title: None,
                    surface_id: Some("s".into()),
                },
                serde_json::json!({}),
            ))
        }
    }

    struct PanickingSource;

    impl ActivitySource for PanickingSource {
        fn name(&self) -> &'static str {
            "boom"
        }
        fn tier(&self) -> Tier {
            Tier::Cheap
        }
        fn tick(&mut self, _ctx: &TickContext<'_>) -> Option<Envelope> {
            panic!("source must not stall the loop");
        }
    }

    #[test]
    fn partition_puts_dedicated_aside() {
        let sources: Vec<Box<dyn ActivitySource>> = vec![
            Box::new(CountingSource {
                isolation: Isolation::Shared,
                ticks: 0,
            }),
            Box::new(CountingSource {
                isolation: Isolation::Dedicated,
                ticks: 0,
            }),
        ];
        let (shared, dedicated) = partition_sources(sources);
        assert_eq!(shared.len(), 1);
        assert_eq!(dedicated.len(), 1);
        assert_eq!(dedicated[0].isolation(), Isolation::Dedicated);
    }

    #[test]
    fn tick_skips_a_panicking_source_and_still_emits_the_rest() {
        let mut sources: Vec<Box<dyn ActivitySource>> = vec![
            Box::new(PanickingSource),
            Box::new(CountingSource {
                isolation: Isolation::Shared,
                ticks: 0,
            }),
        ];
        let bus = EscalationBus::new();
        let mut emitted = Vec::new();
        tick_shared(&mut sources, &[], &bus, |env| emitted.push(env.source));
        assert_eq!(emitted, vec!["foreground"]);
    }

    #[test]
    fn foreground_tick_publishes_to_escalation_bus() {
        let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        struct Rec {
            seen: std::sync::Arc<std::sync::Mutex<Vec<String>>>,
        }
        impl ActivitySource for Rec {
            fn name(&self) -> &'static str {
                "fake-uia"
            }
            fn tier(&self) -> Tier {
                Tier::Escalated
            }
            fn isolation(&self) -> Isolation {
                Isolation::Dedicated
            }
            fn tick(&mut self, _ctx: &TickContext<'_>) -> Option<Envelope> {
                None
            }
            fn on_focus_changed(&mut self, event: &FocusChanged) {
                self.seen.lock().unwrap().push(event.app.clone());
            }
        }
        let bus = EscalationBus::new();
        bus.subscribe(std::sync::Arc::new(std::sync::Mutex::new(Box::new(Rec {
            seen: seen.clone(),
        }))));
        let mut sources: Vec<Box<dyn ActivitySource>> = vec![Box::new(CountingSource {
            isolation: Isolation::Shared,
            ticks: 0,
        })];
        tick_shared(&mut sources, &[], &bus, |_| {});
        assert_eq!(*seen.lock().unwrap(), vec!["Code"]);
    }
}
