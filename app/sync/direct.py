"""One direct transfer job with durable progress and batched writes."""
import asyncio
import json
import os
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from traceback import extract_tb
from uuid import uuid4

import httpx
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
from ytmusicapi.exceptions import YTMusicServerError

from ..connectors import spotify, ytmusic
from ..connectors.base import Item, NotConfigured, ProviderResponseError
from ..config import settings
from ..db import connect, get_setting, set_setting
from . import direct_results, schedule, undo
from .destinations import PlaylistConflict, SpotifyDestination, WriteVerificationError, YouTubeDestination, batches
from .engine import FETCHERS, POLL_LOCK, persist
from .matcher import catalog_match
from .transfer import ExportTrack, _match, catalog_match_key

ENABLED = os.getenv('AUTO_TRANSFER_ENABLED', '').lower() == 'true'
SERVICES = ('spotify', 'ytmusic')
BOTH = 'both'
ALL = 'all'
REMOTE_ID = re.compile(r'[A-Za-z0-9_-]{1,160}')
NO_MATCH_REASONS = {
    'ambiguous': 'Có nhiều bản ghi phù hợp, cần duyệt bản ghép.',
    'insufficient': 'Có kết quả, nhưng tên bài, nghệ sĩ, thời lượng hoặc mã bản ghi chưa đủ khớp.',
    'not_found': 'Dịch vụ đích không trả kết quả cho bài này.',
    'unavailable': 'Nguồn không cung cấp ID để chuyển bài này.',
    'missing_title': 'Nguồn thiếu tên bài để tra cứu.',
}
_task = None


def status():
    with connect() as con:
        job = con.execute('SELECT * FROM direct_sync_job WHERE id=1').fetchone()
    job = dict(job) if job else None
    if job:
        job['result'] = json.loads(job.pop('result_json') or '{}')
    return {'enabled': ENABLED, 'job': job, 'rule': rule(), 'history': undo.history(),
            'next_check_at': schedule.next_check('direct')}


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
    schedule.reset('direct')


def progress(job_id, state, stage, done=0, total=0, result=None):
    with connect() as con:
        con.execute(
            """UPDATE direct_sync_job SET state=?,stage=?,done=?,total=?,result_json=?,
               updated_at=datetime('now') WHERE id=1 AND job_id=?""",
            (state, stage, done, total, json.dumps(result or {}), job_id),
        )


def recover():
    global _task
    with connect() as con:
        job = con.execute(
            "SELECT * FROM direct_sync_job WHERE id=1 AND state IN ('queued','running')",
        ).fetchone()
        if not job:
            con.execute('DELETE FROM direct_sync_snapshots')
            return
        details = json.loads(job['result_json'] or '{}')
        if ENABLED and job['stage'] == 'undo' and details.get('transfer_id'):
            _task = asyncio.create_task(run_undo(
                job['job_id'], details['transfer_id'], job['target'], retry_at=job['retry_at'],
            ))
            return
        resume = ENABLED and directions(job['source'], job['target']) and job['stage'] != 'undo'
        if not resume:
            con.execute("UPDATE direct_sync_job SET state='interrupted', retry_at=NULL WHERE id=1")
            con.execute('DELETE FROM direct_sync_snapshots')
            return
    _task = asyncio.create_task(
        run(job['job_id'], job['source'], job['target'], resumed=True, retry_at=job['retry_at'],
            scheduled=bool(details.get('scheduled'))),
    )


async def _wait_until(job_id, retry_at):
    await asyncio.sleep(max(0, retry_at - time.time()))
    with connect() as con:
        con.execute('UPDATE direct_sync_job SET retry_at=NULL,retry_provider=NULL WHERE job_id=?', (job_id,))


async def _wait_for_provider(job_id, retry_at, auth_versions, provider):
    with connect() as con:
        con.execute('UPDATE direct_sync_job SET retry_at=?,retry_provider=? WHERE job_id=?',
                    (retry_at, provider, job_id))
    POLL_LOCK.release()
    try:
        await _wait_until(job_id, retry_at)
    finally:
        await POLL_LOCK.acquire()
    if auth_versions != _auth_versions(auth_versions):
        raise NotConfigured('Kết nối đã thay đổi khi chờ. Hãy bắt đầu lượt đồng bộ mới.')


def _auth_versions(providers):
    files = {'ytmusic': settings.ytm_auth_path, 'apple': settings.apple_user_token_path}
    versions = {}
    for provider in providers:
        revision = [get_setting(provider + '_auth_version')]
        if provider in files:
            try:
                file = files[provider].stat()
                revision.extend((file.st_mtime_ns, file.st_size))
            except FileNotFoundError:
                pass
        elif provider == 'spotify':
            revision.append(settings.spotify_client_id)
        versions[provider] = revision
    return versions


def _job_auth_versions(job_id, providers):
    key = 'direct_auth_versions'
    saved = get_setting(key)
    versions = _auth_versions(providers)
    if saved:
        previous = json.loads(saved)
        if previous['job_id'] == job_id and previous['versions'] != versions:
            raise NotConfigured('Kết nối đã thay đổi. Hãy bắt đầu lượt đồng bộ mới.')
    set_setting(key, json.dumps({'job_id': job_id, 'versions': versions}))
    return versions


async def _snapshot(provider):
    with connect() as con:
        saved = con.execute('SELECT payload_json FROM direct_sync_snapshots WHERE provider=?', (provider,)).fetchone()
    if saved:
        return {scope: [Item(**item) for item in items] for scope, items in json.loads(saved[0]).items()}
    snapshot = await FETCHERS[provider]()
    if 'liked' not in snapshot or 'owned_playlist' not in snapshot:
        raise ValueError('The snapshot lacks favorites or playlists.')
    persist(provider, snapshot)
    relevant = {scope: items for scope, items in snapshot.items()
                if scope in ('liked', 'owned_playlist') or scope.startswith('playlist:')}
    payload = json.dumps({scope: [asdict(item) for item in items] for scope, items in relevant.items()},
                         ensure_ascii=False, separators=(',', ':'))
    with connect() as con:
        con.execute('INSERT INTO direct_sync_snapshots VALUES (?,?)', (provider, payload))
    return snapshot


def directions(source, target):
    if source == target == BOTH:
        return (('ytmusic', 'spotify'), ('spotify', 'ytmusic'))
    if source == target == ALL:
        return (*directions(BOTH, BOTH), ('apple', 'spotify'), ('apple', 'ytmusic'))
    if source == 'apple' and target in SERVICES:
        return ((source, target),)
    if source in SERVICES and target in SERVICES and source != target:
        return ((source, target),)
    return ()


def submit(source, target, *, scheduled=False):
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
        con.execute('DELETE FROM direct_sync_snapshots')
        con.execute(
            """INSERT INTO direct_sync_job(id,job_id,source,target,state,stage)
               VALUES (1,?,?,?,'queued','waiting')""", (job_id, source, target),
        )
        con.execute('UPDATE direct_sync_job SET result_json=? WHERE job_id=?',
                    (json.dumps({'scheduled': scheduled}), job_id))
    _task = asyncio.create_task(run(job_id, source, target, scheduled=scheduled))
    return {'job_id': job_id, 'already_running': False}


def running():
    return _task is not None and not _task.done()


async def shutdown():
    if running():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)


def _local_match(source, target, track):
    with connect() as con:
        match, method = _match(con, source, target, track)
        cached = con.execute(
            """SELECT target_id,reason FROM direct_catalog_cache WHERE source=? AND source_id=?
               AND target=? AND expires_at>? AND match_key=?""",
            (source, track.source_id, target, int(time.time()), _match_key(track, target)),
        ).fetchone()
    return match, method, cached


def _match_key(track, target):
    return catalog_match_key(track, get_setting(target + '_auth_version'))


async def _catalog_match(adapter, source, target, track):
    candidates = [row for row in await adapter.search(track) if REMOTE_ID.fullmatch(row[0])]
    match, reason = catalog_match(track, candidates)
    if reason in ('not_found', 'insufficient') and hasattr(adapter, 'search_fallback'):
        fallback = await adapter.search_fallback(track)
        candidates.extend(row for row in fallback if REMOTE_ID.fullmatch(row[0]))
        match, reason = catalog_match(track, candidates)
    with connect() as con:
        con.execute(
            """INSERT INTO direct_catalog_cache
               (source,source_id,target,target_id,expires_at,reason,match_key,candidates_json)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(source,source_id,target) DO UPDATE SET target_id=excluded.target_id,
               expires_at=excluded.expires_at,reason=excluded.reason,match_key=excluded.match_key,
               candidates_json=excluded.candidates_json""",
            (source, track.source_id, target, match, int(time.time()) + (30 if match else 1)*86400,
             reason, _match_key(track, target),
             json.dumps(list({row[0]: row for row in candidates}.values())) if not match else None),
        )
    return match, reason


def _owned_playlist(item, target):
    return target == 'ytmusic' or item.raw.get('owned_by_me') is True


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
    if previous is None:
        with connect() as con:
            reverse = con.execute(
                '''SELECT source_id FROM direct_playlist_links
                   WHERE source=? AND target=? AND target_id=?''',
                (target, source, playlist.remote_id),
            ).fetchone()
        if reverse:
            previous = {'target_id': reverse['source_id']}
    if previous and previous['target_id']:
        target_id = previous['target_id']
        if not REMOTE_ID.fullmatch(target_id):
            return None
        if target == 'spotify' or any(item.remote_id == target_id for item in destination):
            return target_id  # Spotify checks live ownership and privacy before writing.
        return target_id if await adapter.owns_playlist(target_id) else None
    named = [item for item in destination if item.title.strip() == playlist.title.strip()]
    if named:
        if (len(named) != 1 or not _owned_playlist(named[0], target)
                or not REMOTE_ID.fullmatch(named[0].remote_id)):
            return None
        return named[0].remote_id
    if previous:
        # A timed-out creation may still exist. Never blindly create a second playlist.
        return None
    while True:
        _bind(source, target, playlist, None)
        try:
            playlist_id = await adapter.create_playlist(playlist.title.strip())
            break
        except httpx.HTTPStatusError as exc:
            if undo.rejected_write(exc):
                with connect() as con:
                    con.execute(
                        """DELETE FROM direct_playlist_links
                           WHERE source=? AND source_id=? AND target=? AND target_id IS NULL""",
                        (source, playlist.remote_id, target),
                    )
            if not await spotify.wait_after_rate_limit(exc.response):
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
    direct_results.seed(job_id, source, target, collections)
    total = sum(len(items) for _, items in collections)
    progress(job_id, 'running', 'match_tracks', 0, total, result)
    matches, plans = {}, []
    done = 0
    with connect() as con:
        con.execute('DELETE FROM direct_catalog_cache WHERE expires_at<=?', (int(time.time()),))
    for playlist, items in collections:
        tracks, rows = [], []
        collection = playlist.title if playlist else None
        collection_key = f'playlist:{playlist.remote_id}' if playlist else 'liked'
        for item in items:
            source_id = item.raw.get('track_id') or item.remote_id
            if source_id not in matches:
                track = ExportTrack(source_id=source_id, title=item.title,
                                    artist=item.artist, duration_ms=item.duration_ms, isrc=item.isrc)
                if item.raw.get('unavailable'):
                    match, method, cached = None, 'unavailable', None
                else:
                    match, method, cached = _local_match(source, target, track)
                state = 'unmatched'
                reason = None
                if method == 'unavailable':
                    reason = method
                elif not match and cached is not None:
                    match, reason = cached['target_id'], cached['reason']
                elif not match:
                    if not (track.title.strip() or track.isrc):
                        reason = 'missing_title'
                    else:
                        try:
                            match, reason = await _catalog_match(adapter, source, target, track)
                        except Exception as exc:
                            state, reason = 'failed', _error(exc)
                            if not _is_item_error(exc):
                                rows.append((done, item.title, item.artist, state, reason, source_id, None))
                                direct_results.record(job_id, source, target, collection, rows, collection_key=collection_key)
                                raise
                if match and REMOTE_ID.fullmatch(match):
                    state, reason = 'pending', 'Đã ghép, đang chờ ghi vào dịch vụ đích.'
                else:
                    match = None
                    if state == 'unmatched':
                        reason = NO_MATCH_REASONS.get(reason, NO_MATCH_REASONS['insufficient'])
                matches[source_id] = match, state, reason
            match, state, reason = matches[source_id]
            if state == 'failed':
                result['tracks_failed'] += 1
            elif match:
                tracks.append((match, done))
            else:
                result['unmatched'] += 1
            rows.append((done, item.title, item.artist, state, reason, source_id, match))
            done += 1
            if len(rows) >= 25:
                direct_results.record(job_id, source, target, collection, rows, collection_key=collection_key)
                rows.clear()
                progress(job_id, 'running', 'match_tracks', done, total, result)
        if rows:
            direct_results.record(job_id, source, target, collection, rows, collection_key=collection_key)
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


async def _apply(job_id, source, target, source_data, target_data, adapter, result, transfer_id=None, *,
                 resumed=False, destination_playlists=None):
    plans = await _plan(job_id, source, target, source_data, adapter, result)
    account = await adapter.account()
    if not account:
        raise ValueError('Cannot identify the destination account for safe undo.')
    journal = undo.Journal(transfer_id or job_id, source, target, account, result)
    if resumed:
        journal.restore_counts()
        if hasattr(adapter, 'recover_likes'):
            await adapter.recover_likes(journal)
    target_likes = {item.remote_id for item in target_data['liked']}
    if target == 'spotify' and not resumed:
        undo.resolve_absent_spotify_likes(account, target_likes)
    destination = list(target_data['owned_playlist']) if destination_playlists is None else destination_playlists
    done, total = 0, sum(len(tracks) for _, tracks in plans)

    def report(count=0):
        nonlocal done
        done += count
        progress(job_id, 'running', 'write_collections', done, total, result)

    report()
    for playlist, tracks in plans:
        if not tracks:
            continue
        if playlist is None:
            await _write_likes(job_id, source, adapter, journal, tracks, target_likes, result, report)
        else:
            await _write_playlist(job_id, source, target, adapter, journal, playlist,
                                  tracks, destination, result, report)


async def _write_likes(job_id, source, adapter, journal, tracks, existing, result, report):
    covered, missing = _work(tracks, existing, unique=True)
    duplicates = {track_id: [] for track_id, _ in missing}
    present = []
    for track in covered:
        if track[0] in duplicates:
            duplicates[track[0]].append(track)
        else:
            present.append(track)
    direct_results.mark(job_id, source, present, 'synced', target=journal.target)
    report(len(present))
    async def apply(batch):
        ids = [value for value, _ in batch]
        affected = batch + [track for value in ids for track in duplicates[value]]
        try:
            await adapter.apply_likes(ids, journal)
        except Exception as exc:
            if _provider_status(exc) == 400 and len(batch) > 1:
                middle = len(batch) // 2
                await apply(batch[:middle])
                await apply(batch[middle:])
                return
            direct_results.mark(job_id, source, affected, 'failed', _error(exc), target=journal.target)
            if not _is_item_error(exc):
                raise
            result['tracks_failed'] += len(affected)
        else:
            direct_results.mark(job_id, source, affected, 'synced', target=journal.target)
        report(len(affected))

    for batch in batches(missing[::-1], 40):
        await apply(batch)


def _ordered_batches(tracks, destination):
    """Insert into an existing subsequence without moving any existing entry."""
    groups = []
    cursor = 0
    for position, track in enumerate(tracks):
        if cursor < len(destination) and track[0] == destination[cursor]:
            cursor += 1
            continue
        if groups and groups[-1][0] + len(groups[-1][1]) == position and len(groups[-1][1]) < 100:
            groups[-1][1].append(track)
        else:
            groups.append((position, [track]))
    return groups if cursor == len(destination) else None


async def _write_playlist(job_id, source, target, adapter, journal, playlist, tracks, destination, result, report):
    try:
        playlist_id = (await _playlist_id(adapter, source, target, playlist, destination)
                       if 0 < len(playlist.title.strip()) <= 100 else None)
        if not playlist_id:
            raise PlaylistConflict('Playlist đích trùng tên, thiếu quyền sở hữu hoặc cần kiểm tra.')
        if not any(item.remote_id == playlist_id for item in destination):
            destination.append(Item('owned_playlist', playlist_id, playlist.title, kind='playlist',
                                    raw={'owned_by_me': True, 'public': False}))
        if target == 'spotify':
            old, snapshot = await adapter.playlist_value(playlist_id)
        else:
            old = await adapter.playlist_tracks(playlist_id)
            snapshot = None
        for entry, _, guard in journal.pending_playlist(playlist_id):
            verified = guard.get('after') == undo.fingerprint(old) and (
                (snapshot is not None and guard.get('snapshot') == snapshot)
                or (guard.get('item_ids') and await adapter.verify_playlist(playlist_id, old, guard['item_ids']))
            )
            if verified:
                journal.confirm(entry, guard)
            else:
                raise PlaylistConflict('Lượt ghi trước chưa xác minh được. Kiểm tra playlist trước khi thêm tiếp.')
        if not _bind(source, target, playlist, playlist_id):
            raise PlaylistConflict('Playlist đích đã liên kết với một playlist nguồn khác.')
    except Exception as exc:
        _playlist_failure(job_id, source, target, tracks, result, exc)
        report(len(tracks))
        return
    ordered = _ordered_batches(tracks, old) if target == 'spotify' and hasattr(adapter, 'insert') else None
    if ordered is not None:
        missing_positions = {position for _, batch in ordered for _, position in batch}
        covered = [track for track in tracks if track[1] not in missing_positions]
        writes = ordered
    else:
        covered, missing = _work(tracks, old)
        writes = [(None, batch) for batch in batches(missing, 100)]
    direct_results.mark(job_id, source, covered, 'synced', target=target)
    report(len(covered))

    async def before_retry():
        if target == 'spotify' and await adapter.playlist_value(playlist_id) != (old, snapshot):
            raise PlaylistConflict('Playlist Spotify thay đổi trong lúc chờ giới hạn yêu cầu.')

    for index, (position, batch) in enumerate(writes):
        ids = [value for value, _ in batch]
        expected = old + ids if position is None else old[:position] + ids + old[position:]

        async def write(playlist_id, values):
            if position is not None and position < len(old):
                return await adapter.insert(playlist_id, values, position)
            return await adapter.append(playlist_id, values)

        try:
            entry, receipt = await journal.write_playlist(playlist_id, ids, write, before_retry)
            if target == 'spotify' and receipt:
                guard = {'before': undo.fingerprint(old), 'after': undo.fingerprint(expected),
                         'snapshot': receipt, 'unique': not set(ids).intersection(old)}
                journal.checkpoint(entry, guard)
            elif target == 'ytmusic' and receipt:
                journal.checkpoint(entry, {'item_ids': receipt, 'after': undo.fingerprint(old + ids)})
            guard = await _playlist_guard(adapter, target, playlist_id, old, ids, receipt, expected=expected)
            journal.confirm(entry, guard)
        except Exception as exc:
            remaining = [item for _, work in writes[index:] for item in work]
            _playlist_failure(job_id, source, target, remaining, result, exc)
            report(len(remaining))
            return
        old = expected
        snapshot = receipt
        direct_results.mark(job_id, source, batch, 'synced', target=target)
        report(len(batch))


def _playlist_failure(job_id, source, target, tracks, result, exc):
    direct_results.mark(job_id, source, tracks, 'failed', _error(exc), target=target)
    if not (isinstance(exc, PlaylistConflict) or _is_item_error(exc) or (
        target == 'spotify' and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 404)
    )):
        raise exc
    result['playlists_need_review'] += 1
    result['tracks_failed'] += len(tracks)


def _is_item_error(exc):
    if isinstance(exc, (httpx.TransportError, RequestsConnectionError, RequestsTimeout,
                        WriteVerificationError, ProviderResponseError)):
        return True
    status = _provider_status(exc)
    return status in (400, 404) or (status is not None and status >= 500)


def _provider_status(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    if isinstance(exc, YTMusicServerError):
        match = re.match(r'Server returned HTTP ([1-5][0-9]{2})\b', str(exc))
        if match:
            return int(match[1])
    return None


async def _playlist_guard(adapter, target, playlist_id, old, added, receipt, *, expected):
    if target == 'spotify':
        observed, snapshot = await adapter.playlist_value(playlist_id)
        if receipt and snapshot == receipt and observed == expected:
            return {'before': undo.fingerprint(old), 'after': undo.fingerprint(observed),
                    'snapshot': snapshot, 'unique': not set(added).intersection(old)}
    elif receipt and (await adapter.verify_playlist(playlist_id, old + added, receipt)
                      if hasattr(adapter, 'verify_playlist') else
                      await adapter.playlist_tracks(playlist_id) == old + added):
        return {'item_ids': receipt, 'after': undo.fingerprint(old + added)}
    raise PlaylistConflict('Playlist đích đã nhận yêu cầu ghi, nhưng thứ tự hoặc phiên bản sau ghi chưa được xác minh.')


@asynccontextmanager
async def destination_client(target):
    if target == 'spotify':
        token = await spotify.access_token(required_scopes=spotify.DIRECT_WRITE_SCOPES)
        async with httpx.AsyncClient(timeout=25, headers={'Authorization': f'Bearer {token}'}) as http:
            yield SpotifyDestination(http)
    else:
        yield YouTubeDestination(await ytmusic.call('khởi tạo kết nối', ytmusic.client))


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
            """INSERT INTO direct_sync_job(id,job_id,source,target,state,stage,result_json)
               VALUES (1,?,?,?,'queued','undo',?)""",
            (job_id, transfer['source'], transfer['target'], json.dumps({'transfer_id': transfer_id})),
        )
    _task = asyncio.create_task(run_undo(job_id, transfer_id, transfer['target']))
    return {'job_id': job_id}


async def run_undo(job_id, transfer_id, target, *, retry_at=None):
    token = None
    ytm_token = None
    result = dict(transfer_id=transfer_id, likes_removed=0, playlist_tracks_removed=0, conflicts=0, uncertain=0)
    try:
        async with POLL_LOCK:
            versions = _job_auth_versions(job_id, (target,))
            token = spotify.rate_limit_waiter.set(lambda deadline: _wait_for_provider(job_id, deadline, versions, 'spotify'))
            ytm_token = ytmusic.rate_limit_waiter.set(
                lambda deadline: _wait_for_provider(job_id, deadline, versions, 'ytmusic'),
            )
            current_deadline = (spotify.api_retry_at if target == 'spotify' else ytmusic.api_retry_at)()
            deadline = max(retry_at or 0, current_deadline or 0)
            if deadline:
                await _wait_for_provider(job_id, deadline, versions, target)
            progress(job_id, 'running', 'undo', result=result)
            async with destination_client(target) as adapter:
                await undo.reverse(
                    transfer_id, adapter, result,
                    lambda done, total: progress(job_id, 'running', 'undo', done, total, result),
                )
        state = 'partial' if result['conflicts'] or result['uncertain'] else 'completed'
        progress(job_id, state, 'undo_finished', 1, 1, result)
    except Exception as exc:
        result['error'] = _error(exc)
        progress(job_id, 'failed', 'undo', result=result)
    finally:
        if ytm_token is not None:
            ytmusic.rate_limit_waiter.reset(ytm_token)
        if token is not None:
            spotify.rate_limit_waiter.reset(token)


def _error(exc):
    if isinstance(exc, (spotify.MissingWriteScope, NotConfigured, ProviderResponseError)):
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
    if type(exc) is ValueError or isinstance(exc, (PlaylistConflict, WriteVerificationError)):
        return str(exc)
    if isinstance(exc, YTMusicServerError):
        status = _provider_status(exc)
        return f'YouTube Music HTTP {status}.' if status else 'YouTube Music từ chối yêu cầu.'
    if isinstance(exc, (httpx.TimeoutException, RequestsTimeout)):
        return 'Yêu cầu tới dịch vụ âm nhạc vượt thời gian chờ.'
    if isinstance(exc, (httpx.TransportError, RequestsConnectionError)):
        return 'Kết nối tới dịch vụ âm nhạc bị gián đoạn.'
    if isinstance(exc, KeyError):
        frames = extract_tb(exc.__traceback__, limit=-1)
        if frames:
            frame = frames[0]
            return f'Thiếu trường dữ liệu khi đồng bộ ({Path(frame.filename).name}:{frame.lineno}).'
        return 'Thiếu trường dữ liệu khi đồng bộ.'
    return f'Direct sync stopped ({type(exc).__name__}).'


async def run(job_id, source, target, *, resumed=False, retry_at=None, scheduled=False):
    result = dict(scheduled=scheduled, unmatched=0, tracks_failed=0,
                  liked_added=0, playlist_tracks_added=0, playlists_need_review=0)
    pairs = directions(source, target)
    token = None
    unchanged = False
    ytm_token = None
    try:
        async with POLL_LOCK:
            providers = dict.fromkeys(value for pair in pairs for value in pair)
            versions = _job_auth_versions(job_id, providers)
            token = spotify.rate_limit_waiter.set(lambda deadline: _wait_for_provider(job_id, deadline, versions, 'spotify'))
            ytm_token = ytmusic.rate_limit_waiter.set(
                lambda deadline: _wait_for_provider(job_id, deadline, versions, 'ytmusic'),
            )
            deadlines = {'spotify': spotify.api_retry_at() or 0, 'ytmusic': ytmusic.api_retry_at() or 0}
            waiting = max(deadlines, key=deadlines.get)
            deadline = max(retry_at or 0, deadlines[waiting])
            if deadline:
                await _wait_for_provider(job_id, deadline, versions, waiting)
            progress(job_id, 'running', 'check_permissions', result=result)
            if any(destination == 'spotify' for _, destination in pairs):
                await spotify.access_token(required_scopes=spotify.DIRECT_WRITE_SCOPES)
            snapshots = {}
            for index, provider in enumerate(providers):
                progress(job_id, 'running', 'read_source' if index == 0 else 'read_destination', result=result)
                try:
                    snapshots[provider] = await _snapshot(provider)
                except Exception as exc:
                    if provider != 'apple' or source != ALL:
                        raise
                    result['apple_error'] = _error(exc)
            key = f'direct_observation:{source}:{target}'
            with connect() as con:
                fingerprints = con.execute('SELECT provider,payload_json FROM direct_sync_snapshots ORDER BY provider')
                observation = sha256(json.dumps([tuple(row) for row in fingerprints]).encode()).hexdigest()
            previous = json.loads(get_setting(key) or '{}')
            unchanged = (scheduled and not resumed and previous.get('fingerprint') == observation
                         and previous.get('until', 0) > time.time() and not result.get('apple_error'))
            if unchanged:
                result['no_changes'] = True
                result['unmatched'] = previous.get('unmatched', 0)
                progress(job_id, 'partial' if result['unmatched'] else 'completed', 'no_changes', 1, 1, result)
                return
            destinations = {provider: list(snapshot['owned_playlist'])
                            for provider, snapshot in snapshots.items()} if source == ALL else {}
            for origin, destination in pairs:
                if origin not in snapshots or destination not in snapshots:
                    continue
                if source == ALL and origin == 'apple':
                    transfer_id = f'{job_id}-apple-{destination}'
                else:
                    transfer_id = f'{job_id}-{destination}' if len(pairs) > 1 else job_id
                result['active_source'], result['active_target'] = origin, destination
                async with destination_client(destination) as adapter:
                    await _apply(job_id, origin, destination, snapshots[origin],
                                 snapshots[destination], adapter, result, transfer_id, resumed=resumed)
        incomplete = any(result[key] for key in ('unmatched', 'tracks_failed', 'playlists_need_review')) or bool(result.get('apple_error'))
        if not any(result.get(field) for field in ('tracks_failed', 'playlists_need_review',
                                                  'apple_error', 'liked_added', 'playlist_tracks_added')):
            until = int(time.time()) + (3600 if result['unmatched'] else 86400)
            set_setting(key, json.dumps({'fingerprint': observation, 'until': until,
                                         'unmatched': result['unmatched']}))
        else:
            set_setting(key, '')
        progress(job_id, 'partial' if incomplete else 'completed', 'finished', 1, 1, result)
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
    finally:
        if ytm_token is not None:
            ytmusic.rate_limit_waiter.reset(ytm_token)
        if token is not None:
            spotify.rate_limit_waiter.reset(token)
        with connect() as con:
            finished = con.execute(
                "SELECT * FROM direct_sync_job WHERE job_id=? AND state IN ('completed','partial','failed')",
                (job_id,),
            ).fetchone()
            if finished and not unchanged:
                con.execute(
                    'INSERT OR REPLACE INTO direct_sync_runs VALUES (?,?,?,?,?,?)',
                    (job_id, source, target, finished['state'], finished['result_json'], int(time.time())),
                )
            con.execute(
                """DELETE FROM direct_sync_snapshots WHERE EXISTS (
                   SELECT 1 FROM direct_sync_job WHERE job_id=? AND state IN ('completed','partial','failed'))""",
                (job_id,),
            )
        if finished and (scheduled or (rule() or {}).get('scheduled')):
            schedule.record('direct', 10, unchanged=unchanged, retry_at=spotify.api_retry_at())
