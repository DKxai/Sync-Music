"""Persist deadlines so idle libraries stay quiet across restarts."""
import time

from ..db import connect

MINUTE = 60
MAX_IDLE_MINUTES = 240


def next_check(name):
    with connect() as con:
        row = con.execute('SELECT next_at FROM sync_schedule WHERE name=?', (name,)).fetchone()
    return row[0] if row else None


def reset(name):
    with connect() as con:
        con.execute('DELETE FROM sync_schedule WHERE name=?', (name,))


def record(name, minutes, *, unchanged=False, retry_at=None):
    with connect() as con:
        row = con.execute('SELECT idle_checks FROM sync_schedule WHERE name=?', (name,)).fetchone()
        idle = min(8, (row[0] if row else 0) + 1) if unchanged else 0
        delay = min(max(minutes, MAX_IDLE_MINUTES), minutes * 2**idle) * MINUTE
        next_at = max(int(time.time()) + delay, retry_at or 0)
        con.execute(
            """INSERT INTO sync_schedule(name,next_at,idle_checks) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET next_at=excluded.next_at,idle_checks=excluded.idle_checks""",
            (name, next_at, idle),
        )
