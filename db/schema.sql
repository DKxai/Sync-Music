PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS accounts (
  provider TEXT PRIMARY KEY CHECK(provider IN ('spotify','ytmusic','apple')),
  status TEXT NOT NULL DEFAULT 'disconnected',
  remote_user_id TEXT,
  last_success_at TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS encrypted_tokens (
  provider TEXT PRIMARY KEY REFERENCES accounts(provider),
  payload_ciphertext BLOB NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  verifier_ciphertext BLOB NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS music_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('track','artist','playlist')),
  title TEXT NOT NULL,
  artist TEXT,
  album TEXT,
  duration_ms INTEGER,
  isrc TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS music_isrc_idx ON music_items(isrc) WHERE isrc IS NOT NULL;
CREATE TABLE IF NOT EXISTS provider_mappings (
  provider TEXT NOT NULL,
  entity_kind TEXT NOT NULL,
  remote_id TEXT NOT NULL,
  music_item_id TEXT NOT NULL REFERENCES music_items(id),
  confidence REAL NOT NULL DEFAULT 1.0,
  match_method TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  PRIMARY KEY(provider, entity_kind, remote_id)
);
CREATE TABLE IF NOT EXISTS playlist_entries (
  provider TEXT NOT NULL,
  playlist_remote_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  occurrence_id TEXT NOT NULL,
  track_remote_id TEXT,
  music_item_id TEXT REFERENCES music_items(id),
  PRIMARY KEY(provider,playlist_remote_id,occurrence_id)
);
CREATE TABLE IF NOT EXISTS snapshot_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  scope_counts_json TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS source_states (
  provider TEXT NOT NULL,
  scope TEXT NOT NULL,
  remote_id TEXT NOT NULL,
  music_item_id TEXT REFERENCES music_items(id),
  payload_json TEXT NOT NULL,
  present INTEGER NOT NULL DEFAULT 1,
  missing_streak INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(provider,scope,remote_id)
);
CREATE INDEX IF NOT EXISTS states_scope_idx ON source_states(provider,scope,present);
CREATE TABLE IF NOT EXISTS sync_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  provider TEXT NOT NULL,
  scope TEXT NOT NULL,
  remote_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('added','removed','modified')),
  status TEXT NOT NULL DEFAULT 'detected',
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sync_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER REFERENCES sync_events(id),
  target_provider TEXT NOT NULL,
  operation TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'planned',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  item_id TEXT,
  reason TEXT NOT NULL,
  details_json TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO accounts(provider) VALUES ('spotify'),('ytmusic'),('apple');
INSERT OR IGNORE INTO settings(key,value) VALUES('baseline_approved','false');

CREATE TABLE IF NOT EXISTS reviewed_links (
  source_provider TEXT NOT NULL REFERENCES accounts(provider),
  target_provider TEXT NOT NULL REFERENCES accounts(provider),
  entity_kind TEXT NOT NULL CHECK(entity_kind IN ('track','artist')),
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  PRIMARY KEY(source_provider,target_provider,entity_kind,source_id),
  UNIQUE(source_provider,target_provider,entity_kind,target_id),
  FOREIGN KEY(source_provider,entity_kind,source_id)
    REFERENCES provider_mappings(provider,entity_kind,remote_id),
  FOREIGN KEY(target_provider,entity_kind,target_id)
    REFERENCES provider_mappings(provider,entity_kind,remote_id),
  CHECK(source_provider<>target_provider)
);
CREATE INDEX IF NOT EXISTS operation_status_idx ON sync_operations(status, id);

CREATE TABLE IF NOT EXISTS playlist_transfers (
  transfer_key TEXT PRIMARY KEY,
  target TEXT NOT NULL CHECK(target IN ('spotify','ytmusic')),
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  playlist_id TEXT,
  added INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS music_title_idx ON music_items(title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS library_export_snapshots (
  provider TEXT PRIMARY KEY REFERENCES accounts(provider),
  track_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS library_export_order (
  provider TEXT NOT NULL REFERENCES accounts(provider),
  position INTEGER NOT NULL,
  remote_id TEXT NOT NULL,
  PRIMARY KEY(provider,position)
);

CREATE TABLE IF NOT EXISTS dashboard_sessions (
  token_hash TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS direct_sync_rule (
  id INTEGER PRIMARY KEY CHECK(id=1),
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  scheduled INTEGER NOT NULL CHECK(scheduled IN (0,1))
);

CREATE TABLE IF NOT EXISTS direct_sync_job (
  id INTEGER PRIMARY KEY CHECK(id=1),
  job_id TEXT NOT NULL,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  state TEXT NOT NULL,
  stage TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS direct_playlist_links (
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target TEXT NOT NULL,
  target_id TEXT,
  PRIMARY KEY(source,source_id,target),
  UNIQUE(target,target_id)
);

CREATE TABLE IF NOT EXISTS direct_catalog_cache (
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target TEXT NOT NULL,
  target_id TEXT,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY(source,source_id,target)
);
