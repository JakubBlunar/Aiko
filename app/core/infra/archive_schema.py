"""D8 derived archive tables and transactional source invalidation.

Ownership follows SessionController.session_key: ``user_id:session_id``.
Unnamespaced legacy rows have no established owner and are excluded.
"""

ARCHIVE_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_messages_archive_owner_time
ON messages(substr(session_id, 1, instr(session_id, ':') - 1), created_at, id);
CREATE INDEX IF NOT EXISTS idx_messages_archive_owner_id
ON messages(substr(session_id, 1, instr(session_id, ':') - 1), id);

CREATE TABLE IF NOT EXISTS archive_revisions (
    user_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversation_daily_overviews (
    user_id TEXT NOT NULL,
    timezone TEXT NOT NULL,
    local_day TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    overview_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, timezone, local_day)
);
CREATE INDEX IF NOT EXISTS idx_daily_overviews_range
ON conversation_daily_overviews(user_id, start_at, end_at);

CREATE TRIGGER IF NOT EXISTS archive_message_insert AFTER INSERT ON messages BEGIN
    DELETE FROM conversation_daily_overviews
    WHERE user_id=substr(NEW.session_id, 1, instr(NEW.session_id, ':') - 1)
      AND start_at<=NEW.created_at AND end_at>NEW.created_at;
END;
CREATE TRIGGER IF NOT EXISTS archive_message_delete AFTER DELETE ON messages BEGIN
    INSERT INTO archive_revisions(user_id, revision)
    VALUES(substr(OLD.session_id, 1, instr(OLD.session_id, ':') - 1), 1)
    ON CONFLICT(user_id) DO UPDATE SET revision=revision+1;
    DELETE FROM conversation_daily_overviews
    WHERE user_id=substr(OLD.session_id, 1, instr(OLD.session_id, ':') - 1)
      AND start_at<=OLD.created_at AND end_at>OLD.created_at;
END;
CREATE TRIGGER IF NOT EXISTS archive_message_update
AFTER UPDATE OF content, session_id, created_at, role ON messages
WHEN OLD.content IS NOT NEW.content OR OLD.session_id IS NOT NEW.session_id
  OR OLD.created_at IS NOT NEW.created_at OR OLD.role IS NOT NEW.role
BEGIN
    INSERT INTO archive_revisions(user_id, revision)
    VALUES(substr(OLD.session_id, 1, instr(OLD.session_id, ':') - 1), 1)
    ON CONFLICT(user_id) DO UPDATE SET revision=revision+1;
    INSERT INTO archive_revisions(user_id, revision)
    VALUES(substr(NEW.session_id, 1, instr(NEW.session_id, ':') - 1), 1)
    ON CONFLICT(user_id) DO UPDATE SET revision=revision+1;
    DELETE FROM conversation_daily_overviews
    WHERE (user_id=substr(OLD.session_id, 1, instr(OLD.session_id, ':') - 1)
           AND start_at<=OLD.created_at AND end_at>OLD.created_at)
       OR (user_id=substr(NEW.session_id, 1, instr(NEW.session_id, ':') - 1)
           AND start_at<=NEW.created_at AND end_at>NEW.created_at);
END;
"""
