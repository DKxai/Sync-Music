"""Paginated track results for the latest library transfer."""

import json

from ..db import connect

# Old results only recorded playlist names. Keep them visible after upgrading.
COLLECTION_KEY = "COALESCE(collection_key, CASE WHEN collection IS NULL THEN 'liked' ELSE 'name:' || collection END)"


def record(job_id, source, target, collection, rows, *, collection_key=None):
    with connect() as con:
        con.executemany(
            """INSERT INTO direct_sync_items
               (job_id,source,target,collection,collection_key,position,title,artist,status,reason,source_id,target_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id,source,target,position) DO UPDATE
               SET title=excluded.title,artist=excluded.artist,source_id=excluded.source_id,target_id=excluded.target_id,
                   status=CASE WHEN direct_sync_items.status='synced' AND excluded.status='pending'
                               THEN 'synced' ELSE excluded.status END,
                   reason=CASE WHEN direct_sync_items.status='synced' AND excluded.status='pending'
                               THEN direct_sync_items.reason ELSE excluded.reason END""",
            [(job_id, source, target, collection, collection_key, *row,
              *((None, None) if len(row) == 5 else ())) for row in rows],
        )


def mark(job_id, source, tracks, status, reason=None, *, target=None):
    if not tracks:
        return
    with connect() as con:
        con.executemany(
            """UPDATE direct_sync_items SET status=?,reason=?
               WHERE job_id=? AND source=? AND position=? AND (? IS NULL OR target=?)""",
            [(status, reason, job_id, source, position, target, target) for _, position in tracks],
        )


def _latest_job(con):
    row = con.execute(
        'SELECT job_id FROM direct_sync_items WHERE job_id=(SELECT job_id FROM direct_sync_job WHERE id=1) LIMIT 1',
    ).fetchone()
    if row is None:
        row = con.execute('SELECT job_id FROM direct_sync_items ORDER BY rowid DESC LIMIT 1').fetchone()
    return row['job_id'] if row else None


def list_groups(status, offset, limit, *, job_id=None):
    with connect() as con:
        con.execute('BEGIN')
        job_id = job_id or _latest_job(con)
        if job_id is None:
            return {'groups': [], 'total': 0, 'job_id': None}
        grouping = f"""SELECT source,target,collection,{COLLECTION_KEY} AS key,
                              MIN(position) AS first_position, COUNT(*) AS total,
                              SUM(CASE WHEN ? IS NULL OR status=? THEN 1 ELSE 0 END) AS visible
                       FROM direct_sync_items WHERE job_id=?
                       GROUP BY source,target,collection,{COLLECTION_KEY}"""
        params = (status, status, job_id)
        total = con.execute(f'SELECT COUNT(*) FROM ({grouping}) WHERE visible>0', params).fetchone()[0]
        rows = con.execute(
            f"""SELECT source,target,collection,key,total,visible
                FROM ({grouping}) WHERE visible>0 ORDER BY source,first_position LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    return {'job_id': job_id, 'groups': [dict(row) for row in rows], 'total': total}


def list_items(status, offset, limit, *, source=None, target=None, collection_key=None, job_id=None):
    with connect() as con:
        con.execute('BEGIN')
        job_id = job_id or _latest_job(con)
        if job_id is None:
            return {'items': [], 'total': 0, 'job_id': None}
        conditions, params = ['job_id=?'], [job_id]
        if status:
            conditions.append('status=?')
            params.append(status)
        if source is not None:
            conditions.append('source=?')
            params.append(source)
        if target is not None:
            conditions.append('target=?')
            params.append(target)
        if collection_key is not None:
            conditions.append(f'{COLLECTION_KEY}=?')
            params.append(collection_key)
        where = ' AND '.join(conditions)
        total = con.execute(f'SELECT COUNT(*) FROM direct_sync_items WHERE {where}', params).fetchone()[0]
        entries = con.execute(
            f"""SELECT source,target,title,artist,collection,status,reason,source_id,target_id,position
                FROM direct_sync_items WHERE {where} ORDER BY source,position LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    return {'job_id': job_id, 'items': [dict(row) for row in entries], 'total': total}


def history():
    with connect() as con:
        runs = con.execute(
            'SELECT * FROM direct_sync_runs ORDER BY finished_at DESC, rowid DESC LIMIT 30',
        ).fetchall()
    history = [dict(run) for run in runs]
    for run in history:
        run['result'] = json.loads(run.pop('result_json'))
    return history


def options(source, target, source_id):
    with connect() as con:
        row = con.execute(
            """SELECT candidates_json FROM direct_catalog_cache WHERE source=? AND target=?
               AND source_id=? AND expires_at>unixepoch()""", (source, target, source_id),
        ).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def seed(job_id, source, target, collections):
    with connect() as con:
        position = 0
        for playlist, items in collections:
            collection = playlist.title if playlist else None
            key = f'playlist:{playlist.remote_id}' if playlist else 'liked'
            con.executemany(
                """INSERT OR IGNORE INTO direct_sync_items
                   (job_id,source,target,collection,collection_key,position,title,artist,status,reason,source_id)
                   VALUES (?,?,?,?,?,?,?,?,'deferred',?,?)""",
                [(job_id, source, target, collection, key, index, item.title, item.artist,
                  'Chờ tra cứu bản ghi.', item.raw.get('track_id') or item.remote_id)
                 for index, item in enumerate(items, position)],
            )
            position += len(items)
