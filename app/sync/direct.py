"""One durable direct transfer job with bounded catalog lookups."""
import asyncio
import json
import os
import re
import time
from collections import Counter
from uuid import uuid4

import httpx

from ..connectors import spotify, ytmusic
from ..db import connect
from .destinations import SpotifyDestination, YouTubeDestination, missing_occurrences
from .engine import FETCHERS, POLL_LOCK, persist
from .matcher import preview_score
from .transfer import ExportTrack, _match

ENABLED = os.getenv('AUTO_TRANSFER_ENABLED', '').lower() == 'true'
SERVICES = ('spotify', 'ytmusic')
REMOTE_ID = re.compile(r'[A-Za-z0-9_-]{1,160}')
MAX_SEARCHES = 40
MAX_MUTATIONS = 200
_task = None


def status():
    with connect() as con:
        job = con.execute('SELECT * FROM direct_sync_job WHERE id=1').fetchone()
        rule = con.execute('SELECT source,target,scheduled FROM direct_sync_rule WHERE id=1').fetchone()
    job = dict(job) if job else None
    if job:
        job['result'] = json.loads(job.pop('result_json') or '{}')
    return {'enabled': ENABLED, 'job': job, 'rule': dict(rule) if rule else None}


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


def submit(source, target):
    global _task
    if not ENABLED or source not in SERVICES or target not in SERVICES or source == target:
        raise ValueError('Choose two different services and enable AUTO_TRANSFER_ENABLED.')
    job_id = uuid4().hex
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT job_id,state FROM direct_sync_job WHERE id=1').fetchone()
        if existing and existing['state'] in ('queued', 'running'):
            return {'job_id': existing['job_id'], 'already_running': True}
        con.execute('DELETE FROM direct_sync_job WHERE id=1')
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
    strong = {
        candidate_id for candidate_id, title, artist, duration, isrc in results
        if REMOTE_ID.fullmatch(candidate_id) and (
            (track.isrc and isrc == track.isrc)
            or (not (track.isrc and isrc and track.isrc != isrc)
                and preview_score(track.title, track.artist, track.duration_ms, title, artist, duration) == 1)
        )
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
            for item in destination
        ) else None
    named = [item for item in destination if item.title.strip() == playlist.title.strip()]
    if named:
        if len(named) != 1 or not _owned_playlist(named[0], target):
            return None
        return named[0].remote_id if _bind(source, target, playlist, named[0].remote_id) else None
    if previous:
        # A timed-out creation may still exist. Never blindly create a second playlist.
        return None
    _bind(source, target, playlist, None)
    playlist_id = await adapter.create_playlist(playlist.title.strip()[:100])
    if not REMOTE_ID.fullmatch(playlist_id) or not _bind(source, target, playlist, playlist_id):
        raise ValueError('The destination did not confirm a unique playlist.')
    return playlist_id


async def _apply(job_id, source, target, source_data, target_data, adapter, result):
    playlists = source_data.get('owned_playlist', [])
    collections = [
        (None, source_data.get('liked', [])),
        *((playlist, source_data.get(f'playlist:{playlist.remote_id}', [])) for playlist in playlists),
    ]
    total = sum(len(items) for _, items in collections)
    progress(job_id, 'running', 'match_tracks', 0, total, result)
    matches, plans = {}, []
    done = searches = 0
    now = int(time.time())
    with connect() as con:
        con.execute('DELETE FROM direct_catalog_cache WHERE expires_at<=?', (now,))
    for playlist, items in collections:
        ids = []
        for item in items:
            source_id = item.raw.get('track_id') or item.remote_id
            if source_id not in matches:
                track = ExportTrack(source_id=source_id, title=item.title,
                                    artist=item.artist, duration_ms=item.duration_ms, isrc=item.isrc)
                match, method, cached = _local_match(source, target, track)
                if not match and method != 'ambiguous':
                    if cached is not None:
                        match = cached['target_id']
                    elif searches < MAX_SEARCHES and track.title:
                        searches += 1
                        match = await _catalog_match(adapter, source, target, track)
                    elif track.title:
                        # Leave deferred items uncached so the next run has a chance to match them.
                        result['search_deferred'] += 1
                matches[source_id] = match if match and REMOTE_ID.fullmatch(match) else None
            match = matches[source_id]
            if match:
                ids.append(match)
            else:
                result['unmatched'] += 1
            done += 1
            if done % 10 == 0 or done == total:
                progress(job_id, 'running', 'match_tracks', done, total, result)
        plans.append((playlist, ids))

    remaining = MAX_MUTATIONS
    target_likes = {item.remote_id for item in target_data.get('liked', [])}
    destination = target_data.get('owned_playlist', [])
    # Reuse this list for playlists created earlier in the same job.
    destination = list(destination)
    for index, (playlist, ids) in enumerate(plans):
        progress(job_id, 'running', 'write_collections', index, len(plans), result)
        if playlist is None:
            missing = [value for value in dict.fromkeys(ids) if value not in target_likes]
            batch = missing[:remaining]
            if batch:
                # Favorites usually show the most recently added entries first.
                await adapter.like(list(reversed(batch)))
            result['liked_added'] += len(batch)
        elif ids and remaining:
            if not playlist.title.strip() or len(playlist.title.strip()) > 100:
                result['playlists_need_review'] += 1
                continue
            playlist_id = await _playlist_id(adapter, source, target, playlist, destination)
            if not playlist_id:
                result['playlists_need_review'] += 1
                continue
            if not any(item.remote_id == playlist_id for item in destination):
                from ..connectors.base import Item
                destination.append(Item('owned_playlist', playlist_id, playlist.title, kind='playlist',
                                        raw={'owned_by_me': True, 'public': False}))
            old = await adapter.playlist_tracks(playlist_id)
            missing = missing_occurrences(ids, old)
            batch = missing[:remaining]
            if batch:
                await adapter.append(playlist_id, batch)
                # Verify we added exactly that suffix without changing existing order.
                observed = await adapter.playlist_tracks(playlist_id)
                if observed != old + batch:
                    raise ValueError('The destination playlist needs review after writing.')
            result['playlist_tracks_added'] += len(batch)
        elif ids:
            result['writes_deferred'] += len(ids)
            continue
        else:
            continue
        remaining -= len(batch)
        result['writes_deferred'] += len(missing) - len(batch)
    progress(job_id, 'running', 'write_collections', len(plans), len(plans), result)


async def run(job_id, source, target):
    result = dict(unmatched=0, search_deferred=0, writes_deferred=0,
                  liked_added=0, playlist_tracks_added=0, playlists_need_review=0)
    try:
        async with POLL_LOCK:
            progress(job_id, 'running', 'read_source')
            source_data = await FETCHERS[source]()
            if 'liked' not in source_data or 'owned_playlist' not in source_data:
                raise ValueError('The source snapshot lacks favorites or playlists.')
            persist(source, source_data)
            progress(job_id, 'running', 'read_destination')
            target_data = await FETCHERS[target]()
            if 'liked' not in target_data or 'owned_playlist' not in target_data:
                raise ValueError('The destination snapshot lacks favorites or playlists.')
            persist(target, target_data)
            if target == 'spotify':
                token = await spotify.access_token()
                async with httpx.AsyncClient(timeout=25, headers={'Authorization': f'Bearer {token}'}) as http:
                    await _apply(job_id, source, target, source_data, target_data, SpotifyDestination(http), result)
            else:
                api = await asyncio.to_thread(ytmusic.client)
                await _apply(job_id, source, target, source_data, target_data, YouTubeDestination(api), result)
        incomplete = any(result[key] for key in ('unmatched', 'writes_deferred', 'playlists_need_review'))
        progress(job_id, 'partial' if incomplete else 'completed', 'finished', 1, 1, result)
    except asyncio.CancelledError:
        progress(job_id, 'interrupted', 'interrupted', result=result)
        raise
    except Exception as exc:
        current = status()['job']
        result['error'] = (
            f'Provider HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError)
            else f'Direct sync stopped ({type(exc).__name__}).'
        )
        progress(job_id, 'failed', current['stage'], current['done'], current['total'], result)
