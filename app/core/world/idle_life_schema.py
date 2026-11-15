"""Durable, time-retained diagnostics. No autobiographical memory or LLM reasoning."""

IDLE_LIFE_SCHEMA = """
CREATE TABLE IF NOT EXISTS idle_life_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT UNIQUE,
    run_id TEXT NOT NULL DEFAULT '',
    worker TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observations INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idle_life_time ON idle_life_events(recorded_at, id);
CREATE INDEX IF NOT EXISTS idx_idle_life_run ON idle_life_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_idle_life_worker ON idle_life_events(worker, recorded_at);
"""
