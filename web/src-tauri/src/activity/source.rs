//! Pluggable activity sources and the cheap→escalated bus.
//!
//! v1 registers three cheap `Shared` sources. UIA later is a fourth
//! `register()`: `Escalated` + `Dedicated`, subscribed to
//! [`EscalationBus`]. A hung dedicated tick cannot be cancelled — the
//! contract is leak that thread and keep collecting.

use std::sync::{Arc, Mutex};

use super::envelope::{Envelope, Isolation, Tier};

#[derive(Debug, Clone)]
#[allow(dead_code)] // UIA later reads title / surface_id / at on the bus event.
pub struct FocusChanged {
    pub surface_id: String,
    pub app: String,
    pub title: Option<String>,
    pub at: String,
}

pub struct TickContext<'a> {
    pub allowlist: &'a [String],
}

pub trait ActivitySource: Send {
    fn name(&self) -> &'static str;
    fn tier(&self) -> Tier;
    fn isolation(&self) -> Isolation {
        Isolation::Shared
    }
    /// Change-detected push. `None` means nothing new this tick.
    fn tick(&mut self, ctx: &TickContext<'_>) -> Option<Envelope>;
    fn on_focus_changed(&mut self, _event: &FocusChanged) {}
    fn reset(&mut self) {}
}

/// Fan-out for cheap-tier identity changes. v1 has zero subscribers;
/// the unit test with a fake escalated source is the lock UIA needs.
#[derive(Clone, Default)]
pub struct EscalationBus {
    subscribers: Arc<Mutex<Vec<Arc<Mutex<Box<dyn ActivitySource>>>>>>,
}

impl EscalationBus {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn subscribe(&self, source: Arc<Mutex<Box<dyn ActivitySource>>>) {
        self.subscribers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(source);
    }

    pub fn subscriber_count(&self) -> usize {
        self.subscribers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .len()
    }

    pub fn publish(&self, event: FocusChanged) {
        let guards = self.subscribers.lock().unwrap_or_else(|e| e.into_inner());
        for source in guards.iter() {
            if let Ok(mut inner) = source.lock() {
                inner.on_focus_changed(&event);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct RecordingEscalated {
        seen: Arc<Mutex<Vec<String>>>,
    }

    impl ActivitySource for RecordingEscalated {
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

    #[test]
    fn fake_escalated_source_receives_focus_changed() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let fake: Arc<Mutex<Box<dyn ActivitySource>>> =
            Arc::new(Mutex::new(Box::new(RecordingEscalated {
                seen: seen.clone(),
            })));
        let bus = EscalationBus::new();
        assert_eq!(bus.subscriber_count(), 0);
        bus.subscribe(fake);
        bus.publish(FocusChanged {
            surface_id: "s".into(),
            app: "Code".into(),
            title: Some("rag_store.py".into()),
            at: "2026-08-30T19:00:00Z".into(),
        });
        assert_eq!(*seen.lock().unwrap(), vec!["Code"]);
    }
}
