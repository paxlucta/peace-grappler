-- peacegrappler.db schema — derived from src/ig-sync.js INSERT statements.
-- Apply with: sqlite3 peacegrappler.db < schema.sql

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sync_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type     TEXT NOT NULL,
  account_id      TEXT,
  status          TEXT NOT NULL,            -- 'running' | 'success' | 'error'
  started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  completed_at    TEXT,
  records_fetched INTEGER,
  error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_log_entity ON sync_log(entity_type, status, completed_at);

CREATE TABLE IF NOT EXISTS ig_accounts (
  id                              TEXT PRIMARY KEY,
  username                        TEXT,
  name                            TEXT,
  biography                       TEXT,
  profile_picture_url             TEXT,
  website                         TEXT,
  followers_count                 INTEGER,
  follows_count                   INTEGER,
  media_count                     INTEGER,
  is_published                    INTEGER,
  shopping_product_tag_eligibility INTEGER,
  fetched_at                      TEXT
);

CREATE TABLE IF NOT EXISTS ig_account_snapshots (
  account_id      TEXT NOT NULL,
  followers_count INTEGER,
  follows_count   INTEGER,
  media_count     INTEGER,
  snapshot_date   TEXT NOT NULL,
  PRIMARY KEY (account_id, snapshot_date),
  FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
);

CREATE TABLE IF NOT EXISTS ig_media (
  id                   TEXT PRIMARY KEY,
  account_id           TEXT NOT NULL,
  caption              TEXT,
  media_type           TEXT,
  media_product_type   TEXT,
  media_url            TEXT,
  thumbnail_url        TEXT,
  permalink            TEXT,
  shortcode            TEXT,
  alt_text             TEXT,
  like_count           INTEGER,
  comments_count       INTEGER,
  is_comment_enabled   INTEGER,
  is_shared_to_feed    INTEGER,
  timestamp            TEXT,
  fetched_at           TEXT,
  FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_ig_media_account_ts ON ig_media(account_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ig_media_product_type ON ig_media(media_product_type);

CREATE TABLE IF NOT EXISTS ig_media_children (
  id            TEXT PRIMARY KEY,
  parent_id     TEXT NOT NULL,
  media_type    TEXT,
  media_url     TEXT,
  thumbnail_url TEXT,
  alt_text      TEXT,
  position      INTEGER,
  FOREIGN KEY (parent_id) REFERENCES ig_media(id)
);

CREATE TABLE IF NOT EXISTS ig_comments (
  id                TEXT PRIMARY KEY,
  media_id          TEXT NOT NULL,
  parent_comment_id TEXT,
  username          TEXT,
  from_id           TEXT,
  text              TEXT,
  like_count        INTEGER,
  hidden            INTEGER,
  timestamp         TEXT,
  fetched_at        TEXT,
  FOREIGN KEY (media_id) REFERENCES ig_media(id),
  FOREIGN KEY (parent_comment_id) REFERENCES ig_comments(id)
);
CREATE INDEX IF NOT EXISTS idx_ig_comments_media ON ig_comments(media_id, timestamp);

-- Append-only: ig-sync.js uses plain INSERT (no OR REPLACE) so multiple
-- snapshots per metric over time are preserved for trend analysis.
CREATE TABLE IF NOT EXISTS ig_media_insights (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id   TEXT NOT NULL,
  metric     TEXT NOT NULL,
  value      INTEGER,
  fetched_at TEXT,
  FOREIGN KEY (media_id) REFERENCES ig_media(id)
);
CREATE INDEX IF NOT EXISTS idx_ig_media_insights_media ON ig_media_insights(media_id, metric, fetched_at);

CREATE TABLE IF NOT EXISTS ig_account_insights (
  account_id           TEXT NOT NULL,
  metric               TEXT NOT NULL,
  period               TEXT NOT NULL,     -- 'day' | 'days_28'
  breakdown_dimension  TEXT,
  breakdown_value      TEXT,
  value                REAL,
  end_time             TEXT NOT NULL,
  PRIMARY KEY (account_id, metric, period, breakdown_dimension, breakdown_value, end_time),
  FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_ig_account_insights_metric ON ig_account_insights(account_id, metric, end_time);

CREATE TABLE IF NOT EXISTS ig_audience_demographics (
  account_id      TEXT NOT NULL,
  metric          TEXT NOT NULL,         -- follower_demographics | engaged_audience_demographics
  dimension       TEXT NOT NULL,         -- age | city | country | gender
  dimension_value TEXT NOT NULL,
  value           INTEGER,
  timeframe       TEXT NOT NULL,         -- last_30_days | this_month | this_week
  fetched_at      TEXT,
  PRIMARY KEY (account_id, metric, dimension, dimension_value, timeframe),
  FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
);

CREATE TABLE IF NOT EXISTS ig_conversations (
  id           TEXT PRIMARY KEY,
  account_id   TEXT NOT NULL,
  updated_time TEXT,
  fetched_at   TEXT,
  FOREIGN KEY (account_id) REFERENCES ig_accounts(id)
);

CREATE TABLE IF NOT EXISTS ig_messages (
  id                  TEXT PRIMARY KEY,
  conversation_id     TEXT NOT NULL,
  from_id             TEXT,
  from_username       TEXT,
  to_id               TEXT,
  to_username         TEXT,
  message             TEXT,
  created_time        TEXT,
  is_unsupported      INTEGER,
  reply_to_message_id TEXT,
  fetched_at          TEXT,
  FOREIGN KEY (conversation_id) REFERENCES ig_conversations(id)
);
CREATE INDEX IF NOT EXISTS idx_ig_messages_convo ON ig_messages(conversation_id, created_time);

CREATE TABLE IF NOT EXISTS ig_message_attachments (
  id         TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  type       TEXT,
  file_url   TEXT,
  name       TEXT,
  FOREIGN KEY (message_id) REFERENCES ig_messages(id)
);

-- Manually curated allowlist consumed by ig-auto-engage / retro-engage.
CREATE TABLE IF NOT EXISTS ig_ignored_accounts (
  username   TEXT PRIMARY KEY,
  reason     TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
