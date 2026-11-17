"""One durable direct transfer job with bounded catalog lookups."""
import asyncio
import json
import os
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx

from ..connectors import spotify, ytmusic
from ..connectors.base import Item
from ..db import connect
from . import direct_results, undo
from .destinations import SpotifyDestination, YouTubeDestination, batches
from .engine import FETCHERS, POLL_LOCK, persist
from .matcher import preview_score
from .transfer import ExportTrack, _match

ENABLED = os.getenv('AUTO_TRANSFER_ENABLED', '').lower() == 'true'
SERVICES = ('spotify', 'ytmusic')
BOTH = 'both'
REMOTE_ID = re.compile(r'[A-Za-z0-9_-]{1,160}')
MAX_SEARCHES = 40
MAX_MUTATIONS = 200
_task = None


def status():
    with connect() as con:
        job = con.execute('SELECT * FROM direct_sync_job WHERE id=1').fetchone()
    job = dict(job) if job else None
    if job:
        job['result'] = json.loads(job.pop('result_json') or '{}')
    return {'enabled': ENABLED, 'job': job, 'rule': rule(), 'history': undo.history()}


def rule():
    with connect() as con:
        row = con.execute('SELECT source,target,scheduled FROM direct_sync_rule WHERE id=1').fetchone()
    return dict(row) if row else None


def save_rule(source, target, scheduled):
    with connect() as con:
        con.execute(
            """INSERT INTO direct_sync_rule VALUES (1,?,?,?)
               ON CONFLICT(id) DO UPDATE SET source=excluded.source,
               target=excluded.target,scheduled=excluded.scheduled""",
            (source, target, int(scheduled)),
        )


def progress(job_id, state, stage, done=0, total=0, result=None):
    with connect() as con:
        con.execute(
            """UPDATE direct_sync_job SET state=?,stage=?,done=?,total=?,result_json=?,
               updated_at=datetime('now') WHERE id=1 AND job_id=?""",
            (state, stage, done, total, json.dumps(result or {}), job_id),
        )


def recover():
    with connect() as con:
        con.execute(
            """UPDATE direct_sync_job SET state='interrupted', stage='interrupted', updated_at=datetime('now')
               WHERE state IN ('queued','running')"""
        )


def directions(source, target):
    if source == target == BOTH:
        return (('ytmusic', 'spotify'), ('spotify', 'ytmusic'))
    if source in SERVICES and target in SERVICES and source != target:
        return ((source, target),)
    return ()


def submit(source, target):
    global _task
    if not ENABLED or not directions(source, target):
        raise ValueError('Choose a valid direction and enable AUTO_TRANSFER_ENABLED.')
    job_id = uuid4().hex
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT job_id,state FROM direct_sync_job WHERE id=1').fetchone()
        if existing and existing['state'] in ('queued', 'running'):
            return {'job_id': existing['job_id'], 'already_running': True}
        con.execute('DELETE FROM direct_sync_job WHERE id=1')
        con.execute('DELETE FROM direct_sync_items')
        con.execute(
            """INSERT INTO direct_sync_job(id,job_id,source,target,state,stage)
               VALUES (1,?,?,?,'queued','waiting')""", (job_id, source, target),
        )
    _task = asyncio.create_task(run(job_id, source, target))
    return {'job_id': job_id, 'already_running': False}


async def shutdown():
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)


def _local_match(source, target, track):
    with connect() as con:
        match, method = _match(con, source, target, track)
        cached = con.execute(
            """SELECT target_id FROM direct_catalog_cache WHERE source=? AND source_id=?
               AND target=? AND expires_at>?""", (source, track.source_id, target, int(time.time())),
        ).fetchone()
    return match, method, cached


async def _catalog_match(adapter, source, target, track):
    results = await adapter.search(track)
    results = [row for row in results if REMOTE_ID.fullmatch(row[0])]
    exact_codes = {candidate_id for candidate_id, _, _, _, isrc in results
                   if track.isrc and isrc == track.isrc}
    strong = exact_codes or {
        candidate_id for candidate_id, title, artist, duration, isrc in results
        if not (track.isrc and isrc and track.isrc != isrc)
        and preview_score(track.title, track.artist, track.duration_ms, title, artist, duration) == 1
    }
    match = next(iter(strong)) if len(strong) == 1 else None
    with connect() as con:
        con.execute(
            """INSERT INTO direct_catalog_cache VALUES (?,?,?,?,?)
               ON CONFLICT(source,source_id,target) DO UPDATE
               SET target_id=excluded.target_id,expires_at=excluded.expires_at""",
            (source, track.source_id, target, match, int(time.time()) + (30 if match else 1)*86400),
        )
    return match


def _owned_playlist(item, target):
    return target == 'ytmusic' or (
        item.raw.get('owned_by_me') is True and item.raw.get('public') is False
        and not item.raw.get('collaborative')
    )


def _bind(source, target, playlist, target_id):
    with connect() as con:
        in_use = con.execute(
            """SELECT 1 FROM direct_playlist_links WHERE target=? AND target_id=?
               AND (source<>? OR source_id<>?)""", (target, target_id, source, playlist.remote_id),
        ).fetchone()
        if in_use:
            return False
        con.execute(
            """INSERT INTO direct_playlist_links VALUES (?,?,?,?)
               ON CONFLICT(source,source_id,target) DO UPDATE SET target_id=excluded.target_id""",
            (source, playlist.remote_id, target, target_id),
        )
    return True


async def _playlist_id(adapter, source, target, playlist, destination):
    with connect() as con:
        previous = con.execute(
            'SELECT target_id FROM direct_playlist_links WHERE source=? AND source_id=? AND target=?',
            (source, playlist.remote_id, target),
        ).fetchone()
    if previous and previous['target_id']:
        return previous['target_id'] if any(
            item.remote_id == previous['target_id'] and _owned_playlist(item, target)
            and REMOTE_ID.fullmatch(item.remote_id)
            for item in destination
        ) else None
    named = [item for item in destination if item.title.strip() == playlist.title.strip()]
    if named:
        if (len(named) != 1 or not _owned_playlist(named[0], target)
                or not REMOTE_ID.fullmatch(named[0].remote_id)):
            return None
        return named[0].remote_id if _bind(source, target, playlist, named[0].remote_id) else None
    if previous:
        # A timed-out creation may still exist. Never blindly create a second playlist.
        return None
    _bind(source, target, playlist, None)
    try:
        playlist_id = await adapter.create_playlist(playlist.title.strip())
    except httpx.HTTPStatusError as exc:
        if undo.rejected_write(exc):
            with connect() as con:
                con.execute(
                    """DELETE FROM direct_playlist_links
                       WHERE source=? AND source_id=? AND target=? AND target_id IS NULL""",
                    (source, playlist.remote_id, target),
                )
        raise
    if not isinstance(playlist_id, str) or not REMOTE_ID.fullmatch(playlist_id) or not _bind(source, target, playlist, playlist_id):
        raise ValueError('The destination did not confirm a unique playlist.')
    return playlist_id


async def _plan(job_id, source, target, source_data, adapter, result):
    playlists = [playlist for playlist in source_data['owned_playlist']
                 if source != 'spotify' or playlist.raw.get('owned_by_me') is True]
    collections = [
        (None, source_data['liked']),
        *((playlist, source_data.get(f'playlist:{playlist.remote_id}', [])) for playlist in playlists),
    ]
    total = sum(len(items) for _, items in collections)
    progress(job_id, 'running', 'match_tracks', 0, total, result)
    matches, plans = {}, []
    done = searches = 0
    with connect() as con:
        con.execute('DELETE FROM direct_catalog_cache WHERE expires_at<=?', (int(time.time()),))
    for playlist, items in collections:
        tracks, rows = [], []
        for item in items:
            source_id = item.raw.get('track_id') or item.remote_id
            if source_id not in matches:
                track = ExportTrack(source_id=source_id, title=item.title,
                                    artist=item.artist, duration_ms=item.duration_ms, isrc=item.isrc)
                match, method, cached = _local_match(source, target, track)
                state, reason = 'unmatched', 'Không tìm thấy bản ghi đủ tin cậy.'
                if method == 'ambiguous':
                    reason = 'Có nhiều bản ghi phù hợp, cần duyệt bản ghép.'
                elif not match and cached is not None:
                    match = cached['target_id']
                elif not match and track.title:
                    if searches < MAX_SEARCHES:
                        searches += 1
                        try:
                            match = await _catalog_match(adapter, source, target, track)
                        except Exception as exc:
                            rows.append((done, item.title, item.artist, 'failed', _error(exc)))
                            direct_results.record(job_id, source, target, playlist.title if playlist else None, rows)
                            raise
                    else:
                        state, reason = 'deferred', 'Chờ tra cứu danh mục ở lượt sau.'
                if match and REMOTE_ID.fullmatch(match):
                    state, reason = 'pending', 'Đã ghép, đang chờ ghi vào dịch vụ đích.'
                else:
                    match = None
                matches[source_id] = match, state, reason
            match, state, reason = matches[source_id]
            if state == 'deferred':
                result['search_deferred'] += 1
            elif match:
                tracks.append((match, done))
            else:
                result['unmatched'] += 1
            rows.append((done, item.title, item.artist, state, reason))
            done += 1
            if len(rows) >= 10:
                direct_results.record(job_id, source, target, playlist.title if playlist else None, rows)
                rows.clear()
                progress(job_id, 'running', 'match_tracks', done, total, result)
        if rows:
            direct_results.record(job_id, source, target, playlist.title if playlist else None, rows)
        plans.append((playlist, tracks))
    progress(job_id, 'running', 'match_tracks', done, total, result)
    return plans


def _work(tracks, present, *, unique=False):
    available = Counter(present)
    existing, missing = [], []
    for track in tracks:
        track_id, _ = track
        if available[track_id]:
            existing.append(track)
            if not unique:
                available[track_id] -= 1
        else:
            missing.append(track)
            if unique:
                # Two source tracks can resolve to the same destination favorite.
                available[track_id] = 1
    return existing, missing


async def _apply(job_id, source, target, source_data, target_data, adapter, result, transfer_id=None):
    plans = await _plan(job_id, source, target, source_data, adapter, result)
    account = await adapter.account()
    if not account:
        raise ValueError('Cannot identify the destination account for safe undo.')
    journal = undo.Journal(transfer_id or job_id, source, target, account, result)
    target_likes = {item.remote_id for item in target_data['liked']}
    if target == 'spotify':
        undo.resolve_absent_spotify_likes(account, target_likes)
    destination = list(target_data['owned_playlist'])
    remaining = MAX_MUTATIONS
    for index, (playlist, tracks) in enumerate(plans):
        progress(job_id, 'running', 'write_collections', index, len(plans), result)
        if not tracks:
            continue
        if playlist is None:
            remaining -= await _write_likes(job_id, source, adapter, journal, tracks, target_likes,
                                            remaining, result)
        else:
            remaining -= await _write_playlist(job_id, source, target, adapter, journal, playlist,
                                               tracks, destination, remaining, result)
    progress(job_id, 'running', 'write_collections', len(plans), len(plans), result)


async def _write_likes(job_id, source, adapter, journal, tracks, existing, remaining, result):
    covered, missing = _work(tracks, existing, unique=True)
    target_ids = {track_id for track_id, _ in missing}
    duplicates = [track for track in covered if track[0] in target_ids]
    direct_results.mark(job_id, source, [track for track in covered if track[0] not in target_ids], 'synced')
    selected = missing[:remaining]
    deferred = missing[remaining:]
    deferred_ids = {track_id for track_id, _ in deferred}
    _defer_writes(job_id, source, deferred + [track for track in duplicates if track[0] in deferred_ids], result)
    for batch in batches(list(reversed(selected)), 40):
        ids = [value for value, _ in batch]
        followers = [track for track in duplicates if track[0] in ids]
        try:
            await adapter.apply_likes(ids, journal)
        except Exception as exc:
            direct_results.mark(job_id, source, batch + followers, 'failed', _error(exc))
            raise
        direct_results.mark(job_id, source, batch + followers, 'synced')
    return len(selected)


async def _write_playlist(job_id, source, target, adapter, journal, playlist, tracks, destination, remaining, result):
    if not remaining:
        _defer_writes(job_id, source, tracks, result)
        return 0
    try:
        playlist_id = (await _playlist_id(adapter, source, target, playlist, destination)
                       if 0 < len(playlist.title.strip()) <= 100 else None)
        if not playlist_id:
            result['playlists_need_review'] += 1
            direct_results.mark(job_id, source, tracks, 'failed',
                                'Playlist đích trùng tên, thiếu quyền sở hữu hoặc cần kiểm tra.')
            return 0
        if not any(item.remote_id == playlist_id for item in destination):
            destination.append(Item('owned_playlist', playlist_id, playlist.title, kind='playlist',
                                    raw={'owned_by_me': True, 'public': False}))
        if target == 'spotify':
            old, _ = await adapter.playlist_value(playlist_id)
        else:
            old = await adapter.playlist_tracks(playlist_id)
    except Exception as exc:
        direct_results.mark(job_id, source, tracks, 'failed', _error(exc))
        raise
    covered, missing = _work(tracks, old)
    direct_results.mark(job_id, source, covered, 'synced')
    limit = min(100, remaining)
    selected = missing[:limit]
    _defer_writes(job_id, source, missing[limit:], result)
    if selected:
        ids = [value for value, _ in selected]
        try:
            entry = journal.record('playlist', ids, playlist_id)
            with journal.attempt(entry):
                receipt = await adapter.append(playlist_id, ids)
            guard = await _playlist_guard(adapter, target, playlist_id, old, ids, receipt)
            journal.confirm(entry, guard)
        except Exception as exc:
            direct_results.mark(job_id, source, selected, 'failed', _error(exc))
            raise
        direct_results.mark(job_id, source, selected, 'synced')
    return len(selected)


def _defer_writes(job_id, source, tracks, result):
    result['writes_deferred'] += len(tracks)
    direct_results.mark(job_id, source, tracks, 'deferred', 'Chờ lượt sau do giới hạn số mục ghi mỗi lượt.')


async def _playlist_guard(adapter, target, playlist_id, old, added, receipt):
    if target == 'spotify':
        observed, snapshot = await adapter.playlist_value(playlist_id)
        if receipt and snapshot == receipt and observed == old + added:
            return {'before': undo.fingerprint(old), 'after': undo.fingerprint(observed),
                    'snapshot': snapshot, 'unique': not set(added).intersection(old)}
    elif receipt and await adapter.playlist_tracks(playlist_id) == old + added:
        return {'item_ids': receipt}
    raise ValueError('The destination playlist needs review after writing.')


@asynccontextmanager
async def destination_client(target):
    if target == 'spotify':
        token = await spotify.access_token(required_scopes=spotify.DIRECT_WRITE_SCOPES)
        async with httpx.AsyncClient(timeout=25, headers={'Authorization': f'Bearer {token}'}) as http:
            yield SpotifyDestination(http)
    else:
        yield YouTubeDestination(await asyncio.to_thread(ytmusic.client))


def submit_undo(transfer_id):
    global _task
    if not ENABLED:
        raise ValueError('Enable AUTO_TRANSFER_ENABLED to undo a transfer.')
    job_id = uuid4().hex
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        active = con.execute('SELECT state FROM direct_sync_job WHERE id=1').fetchone()
        if active and active['state'] in ('queued', 'running'):
            raise ValueError('Wait for the running direct sync job.')
        transfer = con.execute(
            """SELECT source,target FROM direct_sync_undo WHERE job_id=? AND status!='reverted'
               ORDER BY id DESC LIMIT 1""", (transfer_id,),
        ).fetchone()
        if not transfer:
            raise ValueError('This transfer has no changes left to undo.')
        latest = con.execute(
            """SELECT job_id FROM direct_sync_undo WHERE target=? AND status!='reverted'
               ORDER BY id DESC LIMIT 1""", (transfer['target'],),
        ).fetchone()
        if latest['job_id'] != transfer_id:
            raise ValueError('Undo the newer transfer to this destination first.')
        con.execute('UPDATE direct_sync_rule SET scheduled=0 WHERE id=1')
        con.execute('DELETE FROM direct_sync_job WHERE id=1')
        con.execute(
            """INSERT INTO direct_sync_job(id,job_id,source,target,state,stage)
               VALUES (1,?,?,?,'queued','undo')""", (job_id, transfer['source'], transfer['target']),
        )
    _task = asyncio.create_task(run_undo(job_id, transfer_id, transfer['target']))
    return {'job_id': job_id}


async def run_undo(job_id, transfer_id, target):
    result = dict(likes_removed=0, playlist_tracks_removed=0, conflicts=0, uncertain=0)
    try:
        async with POLL_LOCK:
            progress(job_id, 'running', 'undo', result=result)
            async with destination_client(target) as adapter:
                await undo.reverse(
                    transfer_id, adapter, result,
                    lambda done, total: progress(job_id, 'running', 'undo', done, total, result),
                )
        state = 'partial' if result['conflicts'] or result['uncertain'] else 'completed'
        progress(job_id, state, 'undo_finished', 1, 1, result)
    except asyncio.CancelledError:
        progress(job_id, 'interrupted', 'undo', result=result)
        raise
    except Exception as exc:
        result['error'] = _error(exc)
        progress(job_id, 'failed', 'undo', result=result)


def _error(exc):
    if isinstance(exc, spotify.MissingWriteScope):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        request = exc.request
        status_code = exc.response.status_code
        if request.url.host not in ('api.spotify.com', 'accounts.spotify.com'):
            return f'Dịch vụ âm nhạc trả về HTTP {status_code}.'
        path = re.sub(r'/playlists/[A-Za-z0-9_-]+', '/playlists/{id}', request.url.path)
        if path not in ('/v1/me', '/v1/me/tracks', '/v1/me/following', '/v1/me/playlists',
                        '/v1/me/library', '/v1/me/library/contains', '/v1/search',
                        '/v1/playlists/{id}', '/v1/playlists/{id}/items', '/api/token'):
            path = 'API'
        operation = f'Spotify HTTP {status_code} · {request.method} {path}'
        if status_code == 403:
            return operation + '. Cấp lại quyền Spotify. Nếu vẫn lỗi, kiểm tra quyền ứng dụng trong Spotify Developer Dashboard.'
        return operation
    if type(exc) is ValueError:
        return str(exc)
    return f'Direct sync stopped ({type(exc).__name__}).'


async def run(job_id, source, target):
    result = dict(unmatched=0, search_deferred=0, writes_deferred=0,
                  liked_added=0, playlist_tracks_added=0, playlists_need_review=0)
    pairs = directions(source, target)
    try:
        async with POLL_LOCK:
            progress(job_id, 'running', 'check_permissions')
            if any(destination == 'spotify' for _, destination in pairs):
                await spotify.access_token(required_scopes=spotify.DIRECT_WRITE_SCOPES)
            snapshots = {}
            for index, provider in enumerate(dict.fromkeys(value for pair in pairs for value in pair)):
                progress(job_id, 'running', 'read_source' if index == 0 else 'read_destination')
                snapshot = await FETCHERS[provider]()
                if 'liked' not in snapshot or 'owned_playlist' not in snapshot:
                    raise ValueError('The snapshot lacks favorites or playlists.')
                persist(provider, snapshot)
                snapshots[provider] = snapshot
            for origin, destination in pairs:
                transfer_id = f'{job_id}-{destination}' if len(pairs) > 1 else job_id
                result['active_source'], result['active_target'] = origin, destination
                async with destination_client(destination) as adapter:
                    await _apply(job_id, origin, destination, snapshots[origin],
                                 snapshots[destination], adapter, result, transfer_id)
        incomplete = any(result[key] for key in ('unmatched', 'search_deferred', 'writes_deferred', 'playlists_need_review'))
        progress(job_id, 'partial' if incomplete else 'completed', 'finished', 1, 1, result)
    except asyncio.CancelledError:
        progress(job_id, 'interrupted', 'interrupted', result=result)
        raise
    except Exception as exc:
        with connect() as con:
            current = con.execute('SELECT stage,done,total FROM direct_sync_job WHERE id=1').fetchone()
        if isinstance(exc, spotify.MissingWriteScope) or (
            isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403
            and exc.request.url.host == 'api.spotify.com'
        ):
            with connect() as con:
                paused = con.execute(
                    """UPDATE direct_sync_rule SET scheduled=0 WHERE id=1 AND scheduled=1
                       AND source=? AND target=?""", (source, target),
                )
                result['schedule_paused'] = bool(paused.rowcount)
        result['error'] = _error(exc)
        progress(job_id, 'failed', current['stage'], current['done'], current['total'], result)
