"""Encrypted persistent credentials for the single-user provider connections."""

import json

from cryptography.fernet import Fernet

from .config import settings
from .db import connect


def has_token(provider: str) -> bool:
    with connect() as con:
        return con.execute(
            'SELECT 1 FROM encrypted_tokens WHERE provider=?', (provider,)
        ).fetchone() is not None


def load_token(provider: str) -> dict | None:
    with connect() as con:
        row = con.execute(
            'SELECT payload_ciphertext FROM encrypted_tokens WHERE provider=?', (provider,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(Fernet(settings.encryption_key.encode()).decrypt(row[0]))


def replace_token(provider: str, previous: dict, renewed: dict) -> bool:
    """Prevent an in-flight refresh overwriting a newly authorized account."""
    cipher = Fernet(settings.encryption_key.encode())
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT payload_ciphertext FROM encrypted_tokens WHERE provider=?', (provider,)).fetchone()
        if row is None or json.loads(cipher.decrypt(row[0])) != previous:
            return False
        con.execute("UPDATE encrypted_tokens SET payload_ciphertext=?,updated_at=datetime('now') WHERE provider=?",
                    (cipher.encrypt(json.dumps(renewed).encode()), provider))
        return True


def save_token(provider: str, payload: dict, *, reconnect: bool = False) -> None:
    ciphertext = Fernet(settings.encryption_key.encode()).encrypt(json.dumps(payload).encode())
    with connect() as con:
        con.execute(
            """INSERT INTO encrypted_tokens(provider, payload_ciphertext) VALUES (?, ?)
               ON CONFLICT(provider) DO UPDATE SET payload_ciphertext=excluded.payload_ciphertext,
               updated_at=datetime('now')""",
            (provider, ciphertext),
        )
        if reconnect:
            invalidate_account(con, provider)


def invalidate_account(con, provider: str) -> None:
    """Require a fresh baseline before resuming writes after authorization."""
    con.execute(
        """UPDATE accounts SET status='connected', last_success_at=NULL, last_error=NULL,
           updated_at=datetime('now') WHERE provider=?""", (provider,)
    )
    con.execute(
        """INSERT INTO settings(key,value) VALUES (?, '1') ON CONFLICT(key)
           DO UPDATE SET value=CAST(settings.value AS INTEGER)+1""", (provider + '_auth_version',),
    )
    con.execute('DELETE FROM direct_catalog_cache WHERE target=?', (provider,))
    con.execute("DELETE FROM settings WHERE key LIKE 'direct_observation:%'")
    con.execute('DELETE FROM library_versions WHERE provider=?', (provider,))
    con.execute('DELETE FROM sync_schedule WHERE name IN (?,?)', (provider, 'direct'))
    con.execute("UPDATE settings SET value='false',updated_at=datetime('now') WHERE key='baseline_approved'")
    con.execute('UPDATE source_states SET present=0,missing_streak=0 WHERE provider=? AND present=1', (provider,))
    con.execute(
        """UPDATE sync_operations SET status='needs_review', completed_at=datetime('now'),
           last_error='An account was reauthorized. A fresh baseline is required.'
           WHERE status IN ('planned','running','retry')"""
    )
