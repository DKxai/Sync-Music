"""Paginated track results for the latest library transfer."""

from ..db import connect


def record(job_id, source, target, collection, rows):
    with connect() as con:
        con.executemany(
            """INSERT INTO direct_sync_items
               (job_id,source,target,collection,position,title,artist,status,reason)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(job_id, source, target, collection, *row) for row in rows],
        )


def mark(job_id, source, tracks, status, reason=None):
    if not tracks:
        return
    with connect() as con:
        con.executemany(
            """UPDATE direct_sync_items SET status=?,reason=?
               WHERE job_id=? AND source=? AND position=?""",
            [(status, reason, job_id, source, position) for _, position in tracks],
        )


def list_items(status, offset, limit):
    with connect() as con:
        con.execute('BEGIN')
        job = con.execute('SELECT job_id FROM direct_sync_items LIMIT 1').fetchone()
        if not job:
            return {'items': [], 'total': 0, 'job_id': None}
        job_id = job['job_id']
        where = 'WHERE job_id=? AND status=?' if status else 'WHERE job_id=?'
        params = (job_id, status) if status else (job_id,)
        total = con.execute(f'SELECT COUNT(*) FROM direct_sync_items {where}', params).fetchone()[0]
        entries = con.execute(
            f"""SELECT source,target,title,artist,collection,status,reason
                FROM direct_sync_items {where} ORDER BY source,position LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    return {'job_id': job_id, 'items': [dict(row) for row in entries], 'total': total}
