"""Small SQLite connection helpers; low memory and bounded WAL growth."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from .config import settings

SCHEMA = Path(__file__).resolve().parent.parent / 'db' / 'schema.sql'
DB_PATH = settings.data_dir / 'music.db'

@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=20000')
    con.execute('PRAGMA cache_size=-2000')
    con.execute('PRAGMA wal_autocheckpoint=200')
    con.execute('PRAGMA journal_size_limit=4194304')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def init_db():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA.read_text(encoding='utf-8'))
        con.execute('PRAGMA synchronous=NORMAL')
        con.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in con.execute('PRAGMA table_info(direct_sync_items)')}
        if 'collection_key' not in columns:
            con.execute('ALTER TABLE direct_sync_items ADD COLUMN collection_key TEXT')

def get_setting(name: str):
    with connect() as con:
        row = con.execute('SELECT value FROM settings WHERE key=?', (name,)).fetchone()
        return row['value'] if row else None

def set_setting(name: str, value: str):
    with connect() as con:
        con.execute('INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime("now")) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (name, value))

def prune_history():
    """Never delete pending actions, unresolved conflicts, or live library snapshots."""
    with connect() as con:
        con.execute("DELETE FROM oauth_states WHERE expires_at < datetime('now', '-1 day')")
        con.execute("DELETE FROM snapshot_runs WHERE status IN ('complete','failed') AND started_at < datetime('now', '-30 days')")
        con.execute("DELETE FROM sync_events WHERE status IN ('applied','ignored','dismissed') AND created_at < datetime('now', '-90 days') AND id NOT IN (SELECT event_id FROM sync_operations WHERE event_id IS NOT NULL)")
        con.execute("DELETE FROM conflicts WHERE status='resolved' AND created_at < datetime('now', '-90 days')")
    # Run a passive WAL checkpoint outside the transaction on a fresh connection.
    con = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    try:
        con.execute('PRAGMA wal_checkpoint(PASSIVE)')
    finally:
        con.close()
