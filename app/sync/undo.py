"""Journal verified additions and undo the latest transfer for each destination."""
import hashlib
import json
from contextlib import contextmanager

import httpx

from ..db import connect


def rejected_write(exc):
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403, 404, 429)


def fingerprint(ids):
    return hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()


def history():
    with connect() as con:
        return [dict(row) for row in con.execute(
            """SELECT job_id,source,target,MIN(created_at) AS created_at,
               SUM(status='confirmed') AS confirmed, SUM(status='pending') AS uncertain
               FROM direct_sync_undo WHERE status!='reverted'
               GROUP BY job_id,source,target ORDER BY MAX(id) DESC LIMIT 20"""
        ).fetchall()]


def resolve_absent_spotify_likes(account, current_likes):
    with connect() as con:
        rows = con.execute(
            """SELECT id,ids_json FROM direct_sync_undo
               WHERE target='spotify' AND account=? AND kind='likes' AND status='pending'
               ORDER BY id DESC LIMIT 100""", (account,),
        ).fetchall()
        absent = [(row['id'],) for row in rows if current_likes.isdisjoint(json.loads(row['ids_json']))]
        con.executemany("UPDATE direct_sync_undo SET status='reverted' WHERE id=?", absent)


class Journal:
    def __init__(self, job_id, source, target, account, stats):
        self.job_id = job_id
        self.source = source
        self.target = target
        self.account = account
        self.stats = stats

    def record(self, kind, ids, playlist_id=None, guard=None):
        with connect() as con:
            return con.execute(
                """INSERT INTO direct_sync_undo
                   (job_id,source,target,account,kind,playlist_id,ids_json,guard_json,status)
                   VALUES (?,?,?,?,?,?,?,?,'pending')""",
                (self.job_id, self.source, self.target, self.account, kind, playlist_id,
                 json.dumps(ids), json.dumps(guard or {})),
            ).lastrowid

    @contextmanager
    def attempt(self, entry):
        try:
            yield
        except httpx.HTTPStatusError as exc:
            if rejected_write(exc):
                with connect() as con:
                    con.execute("DELETE FROM direct_sync_undo WHERE id=? AND status='pending'", (entry,))
            raise

    def confirm(self, entry, guard=None):
        with connect() as con:
            row = con.execute('SELECT kind,ids_json FROM direct_sync_undo WHERE id=?', (entry,)).fetchone()
            con.execute('UPDATE direct_sync_undo SET status=?,guard_json=? WHERE id=?',
                        ('confirmed', json.dumps(guard or {}), entry))
        key = 'liked_added' if row['kind'] == 'likes' else 'playlist_tracks_added'
        self.stats[key] += len(json.loads(row['ids_json']))


async def reverse(run_id, adapter, result, report):
    with connect() as con:
        rows = con.execute(
            """SELECT * FROM direct_sync_undo WHERE job_id=? AND status!='reverted' ORDER BY id DESC""",
            (run_id,),
        ).fetchall()
    account = await adapter.account()
    if not account or any(row['account'] != account for row in rows):
        raise ValueError('Reconnect the account originally used by this transfer.')
    likes = []
    for position, row in enumerate(rows):
        ids = json.loads(row['ids_json'])
        if row['status'] == 'pending':
            result['uncertain'] += len(ids)
        elif row['kind'] == 'likes':
            likes.append((row['id'], ids))
            continue
        else:
            guard = json.loads(row['guard_json'])
            try:
                restored = await adapter.undo_playlist(row['playlist_id'], ids, guard)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                restored = False
            if restored:
                _completed(row['id'], row, guard, restored if isinstance(restored, str) else None)
                result['playlist_tracks_removed'] += len(ids)
            else:
                result['conflicts'] += len(ids)
        report(position + 1 - len(likes), len(rows))
    if likes:
        ids = [track_id for _, values in likes for track_id in values]
        await adapter.undo_likes(ids)
        with connect() as con:
            con.executemany("UPDATE direct_sync_undo SET status='reverted' WHERE id=?",
                            [(entry,) for entry, _ in likes])
        result['likes_removed'] += len(ids)
        report(len(rows), len(rows))


def _completed(entry, row=None, guard=None, snapshot=None):
    with connect() as con:
        con.execute("UPDATE direct_sync_undo SET status='reverted' WHERE id=?", (entry,))
        if not snapshot:
            return
        previous = con.execute(
            """SELECT id,guard_json FROM direct_sync_undo WHERE id<? AND target=? AND account=?
               AND playlist_id=? AND status='confirmed' ORDER BY id DESC LIMIT 1""",
            (entry, row['target'], row['account'], row['playlist_id']),
        ).fetchone()
        if previous:
            previous_guard = json.loads(previous['guard_json'])
            if previous_guard.get('after') == guard['before']:
                previous_guard['snapshot'] = snapshot
                con.execute('UPDATE direct_sync_undo SET guard_json=? WHERE id=?',
                            (json.dumps(previous_guard), previous['id']))
