"""Bounded request counts without storing URLs, query strings or headers."""
from collections import Counter
from datetime import datetime, timezone
from threading import Lock

from ..db import connect

_counts = Counter()
_lock = Lock()


def record(provider, operation, status):
    day = datetime.now(timezone.utc).date().isoformat()
    with _lock:
        _counts[day, provider, operation, status] += 1


def flush():
    with _lock:
        entries = list(_counts.items())
        _counts.clear()
    if not entries:
        return
    try:
        with connect() as con:
            con.executemany(
                """INSERT INTO provider_requests VALUES (?,?,?,?,?)
                   ON CONFLICT(day,provider,operation,status)
                   DO UPDATE SET requests=requests+excluded.requests""",
                [(*key, count) for key, count in entries],
            )
    except Exception:
        with _lock:
            _counts.update(dict(entries))
        raise


def recent():
    with connect() as con:
        return [dict(row) for row in con.execute(
            """SELECT provider,operation,status,SUM(requests) AS requests FROM provider_requests
               WHERE day>=date('now','-6 days') GROUP BY provider,operation,status""",
        )]
