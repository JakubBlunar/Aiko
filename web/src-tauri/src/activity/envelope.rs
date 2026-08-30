//! Versioned activity envelope shared with the Python ingest path.
//!
//! Keep this shape stable: UIA later fills `payload` and a new `source`
//! without a new WebSocket type. Unknown keys on `v: 1` are a Python
//! concern (it keeps them); Rust only serialises the known fields.

use serde::{Deserialize, Serialize};

pub const ENVELOPE_VERSION: u32 = 1;
pub const SAMPLE_EVENT: &str = "activity://sample";

const SELF_APP_NAMES: &[&str] = &["aiko", "aiko-desktop"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Tier {
    Cheap,
    Escalated,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Isolation {
    Shared,
    Dedicated,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Subject {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub app: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub surface_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Signal {
    pub kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Envelope {
    pub v: u32,
    pub at: String,
    pub source: String,
    pub tier: Tier,
    pub subject: Subject,
    pub signal: Signal,
    pub payload: serde_json::Value,
}

impl Envelope {
    pub fn new(
        source: &str,
        tier: Tier,
        kind: &str,
        subject: Subject,
        payload: serde_json::Value,
    ) -> Self {
        Self {
            v: ENVELOPE_VERSION,
            at: now_rfc3339(),
            source: source.to_string(),
            tier,
            subject,
            signal: Signal {
                kind: kind.to_string(),
            },
            payload,
        }
    }
}

pub fn now_rfc3339() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// Strip a trailing `.exe` (any case) and collapse whitespace.
pub fn normalise_app_name(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let stripped = if trimmed.len() >= 4 && trimmed[trimmed.len() - 4..].eq_ignore_ascii_case(".exe")
    {
        trimmed[..trimmed.len() - 4].trim()
    } else {
        trimmed
    };
    if stripped.is_empty() {
        return None;
    }
    let lower = stripped.to_ascii_lowercase();
    if SELF_APP_NAMES.contains(&lower.as_str()) {
        return None;
    }
    Some(stripped.to_string())
}

pub fn app_on_allowlist(app: &str, allowlist: &[String]) -> bool {
    if allowlist.is_empty() {
        return false;
    }
    let needle = app.trim().to_ascii_lowercase();
    if needle.is_empty() {
        return false;
    }
    allowlist.iter().any(|entry| {
        let candidate = match normalise_app_name(entry) {
            Some(name) => name.to_ascii_lowercase(),
            None => entry.trim().to_ascii_lowercase(),
        };
        candidate == needle
    })
}

/// Stable FNV-1a of a platform window id. Not a HWND — SQLite never
/// sees the raw handle.
pub fn hash_surface_id(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in trimmed.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0100_0000_01b3);
    }
    Some(format!("{hash:016x}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalise_strips_exe_and_self_app() {
        assert_eq!(normalise_app_name("Code.exe").as_deref(), Some("Code"));
        assert_eq!(normalise_app_name("Aiko"), None);
        assert_eq!(normalise_app_name("aiko-desktop.exe"), None);
        assert_eq!(normalise_app_name("  Firefox  ").as_deref(), Some("Firefox"));
    }

    #[test]
    fn allowlist_is_case_insensitive_and_strips_exe() {
        let list = vec!["code.exe".into(), "Cursor".into()];
        assert!(app_on_allowlist("Code", &list));
        assert!(app_on_allowlist("CURSOR", &list));
        assert!(!app_on_allowlist("Chrome", &list));
        assert!(!app_on_allowlist("Code", &[]));
    }

    #[test]
    fn surface_hash_is_stable_and_hides_raw() {
        let hashed = hash_surface_id("12345").unwrap();
        assert_eq!(hashed, hash_surface_id("12345").unwrap());
        assert_ne!(hashed, "12345");
        assert!(hash_surface_id("  ").is_none());
    }
}
