"""Paginated track results for the latest library transfer."""

from ..db import connect

# Old results only recorded playlist names. Keep them visible after upgrading.
COLLECTION_KEY = "COALESCE(collection_key, CASE WHEN collection IS NULL THEN 'liked' ELSE 'name:' || collection END)"


def record(job_id, source, target, collection, rows, *, collection_key=None):
    with connect() as con:
        con.executemany(
            """INSERT INTO direct_sync_items
               (job_id,source,target,collection,collection_key,position,title,artist,status,reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(job_id, source, target, collection, collection_key, *row) for row in rows],
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


def _latest_job(con):
    row = con.execute('SELECT job_id FROM direct_sync_items LIMIT 1').fetchone()
    return row['job_id'] if row else None


def list_groups(status, offset, limit):
    with connect() as con:
        con.execute('BEGIN')
        job_id = _latest_job(con)
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


def list_items(status, offset, limit, *, source=None, collection_key=None):
    with connect() as con:
        con.execute('BEGIN')
        job_id = _latest_job(con)
        if job_id is None:
            return {'items': [], 'total': 0, 'job_id': None}
        conditions, params = ['job_id=?'], [job_id]
        if status:
            conditions.append('status=?')
            params.append(status)
        if source is not None:
            conditions.append('source=?')
            params.append(source)
        if collection_key is not None:
            conditions.append(f'{COLLECTION_KEY}=?')
            params.append(collection_key)
        where = ' AND '.join(conditions)
        total = con.execute(f'SELECT COUNT(*) FROM direct_sync_items WHERE {where}', params).fetchone()[0]
        entries = con.execute(
            f"""SELECT source,target,title,artist,collection,status,reason
                FROM direct_sync_items WHERE {where} ORDER BY source,position LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    return {'job_id': job_id, 'items': [dict(row) for row in entries], 'total': total}
