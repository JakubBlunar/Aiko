//! Desktop activity collector. Isolated from the UI thread: JS never
//! awaits an OS poll. See `docs/presence-and-activity.md`.

mod envelope;
mod runtime;
mod source;
mod sources;

pub use runtime::{start, CollectorHandle};
