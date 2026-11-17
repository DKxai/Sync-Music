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
        columns = {row['name'] for row in con.execute('PRAGMA table_info(music_items)')}
        if 'search_title' not in columns:
            from .sync.matcher import search_title
            con.execute('ALTER TABLE music_items ADD COLUMN search_title TEXT')
            rows = con.execute("SELECT id,title FROM music_items WHERE kind='track'")
            while batch := rows.fetchmany(200):
                con.executemany('UPDATE music_items SET search_title=? WHERE id=?',
                                [(search_title(row['title']), row['id']) for row in batch])
        con.execute('CREATE INDEX IF NOT EXISTS music_search_title_idx ON music_items(search_title)')
        columns = {row['name'] for row in con.execute('PRAGMA table_info(direct_sync_items)')}
        for name in ('source_id', 'target_id'):
            if name not in columns:
                con.execute(f'ALTER TABLE direct_sync_items ADD COLUMN {name} TEXT')
        if 'collection_key' not in columns:
            con.execute('ALTER TABLE direct_sync_items ADD COLUMN collection_key TEXT')
        result_key = {row['name'] for row in con.execute('PRAGMA table_info(direct_sync_items)') if row['pk']}
        if 'target' not in result_key:
            con.execute('DROP INDEX IF EXISTS direct_sync_items_status_idx')
            con.execute('ALTER TABLE direct_sync_items RENAME TO direct_sync_items_previous')
            con.execute("""CREATE TABLE direct_sync_items (
                job_id TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, position INTEGER NOT NULL,
                title TEXT NOT NULL, artist TEXT, collection TEXT, collection_key TEXT, source_id TEXT,
                target_id TEXT, status TEXT NOT NULL, reason TEXT, PRIMARY KEY(job_id,source,target,position))""")
            columns_sql = 'job_id,source,target,position,title,artist,collection,collection_key,source_id,target_id,status,reason'
            con.execute(f'INSERT INTO direct_sync_items ({columns_sql}) SELECT {columns_sql} FROM direct_sync_items_previous')
            con.execute('DROP TABLE direct_sync_items_previous')
            con.execute('CREATE INDEX direct_sync_items_status_idx ON direct_sync_items(job_id,status,source,position)')
        columns = {row['name'] for row in con.execute('PRAGMA table_info(direct_catalog_cache)')}
        for name in ('match_key', 'candidates_json'):
            if name not in columns:
                con.execute(f'ALTER TABLE direct_catalog_cache ADD COLUMN {name} TEXT')
        if 'reason' not in columns:
            con.execute('ALTER TABLE direct_catalog_cache ADD COLUMN reason TEXT')
            con.execute('DELETE FROM direct_catalog_cache WHERE target_id IS NULL')
        columns = {row['name'] for row in con.execute('PRAGMA table_info(direct_sync_job)')}
        if 'retry_provider' not in columns:
            con.execute('ALTER TABLE direct_sync_job ADD COLUMN retry_provider TEXT')
        if 'retry_at' not in columns:
            con.execute('ALTER TABLE direct_sync_job ADD COLUMN retry_at INTEGER')

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
        con.execute("""DELETE FROM import_jobs WHERE state IN ('completed','failed')
                       AND job_id NOT IN (SELECT job_id FROM import_jobs ORDER BY rowid DESC LIMIT 30)""")
        con.execute("DELETE FROM provider_requests WHERE day < date('now', '-7 days')")
        con.execute("DELETE FROM spotify_playlist_cache WHERE fetched_at<unixepoch()-172800")
        expired_runs = """SELECT job_id FROM direct_sync_runs WHERE finished_at<unixepoch()-2592000
                          OR job_id NOT IN (SELECT job_id FROM direct_sync_runs
                                             ORDER BY finished_at DESC,rowid DESC LIMIT 30)"""
        con.execute(f'DELETE FROM direct_sync_items WHERE job_id IN ({expired_runs})')
        con.execute(f'DELETE FROM direct_sync_runs WHERE job_id IN ({expired_runs})')
    # Run a passive WAL checkpoint outside the transaction on a fresh connection.
    con = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    try:
        con.execute('PRAGMA wal_checkpoint(PASSIVE)')
    finally:
        con.close()
