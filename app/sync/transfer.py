"""Export library order and copy reviewed matches into a new private playlist."""
import asyncio
import hashlib
import json
import os
from datetime import datetime

import httpx
from pydantic import BaseModel, Field

from ..connectors import spotify, ytmusic
from ..db import connect
from .matcher import preview_score

MAX_TRACKS = 1000
PLAYLIST_TRANSFER_ENABLED = os.getenv('PLAYLIST_TRANSFER_ENABLED', '').lower() == 'true'


class TransferError(ValueError):
    pass


class ExportTrack(BaseModel):
    source_id: str = Field(min_length=1, max_length=160)
    title: str = Field(max_length=1000)
    artist: str | None = Field(default=None, max_length=1000)
    duration_ms: int | None = Field(default=None, ge=0)
    isrc: str | None = Field(default=None, max_length=32)
    added_at: str | None = Field(default=None, max_length=64)


class LibraryExport(BaseModel):
    version: int = Field(ge=1, le=1)
    provider: str
    order: str = Field(max_length=40)
    tracks: list[ExportTrack] = Field(max_length=MAX_TRACKS)


def _date(value):
    try:
        date = datetime.fromisoformat(value)
        return date.timestamp() if date.tzinfo else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def export_library(provider):
    scope = 'library' if provider == 'apple' else 'liked'
    with connect() as con:
        account = con.execute('SELECT last_success_at FROM accounts WHERE provider=?', (provider,)).fetchone()
        if not account or not account['last_success_at']:
            raise TransferError('Run a successful source import before exporting.')
        rows = con.execute(
            """SELECT remote_id,payload_json FROM source_states
               WHERE provider=? AND scope=? AND present=1 AND missing_streak=0
               ORDER BY rowid LIMIT ?""", (provider, scope, MAX_TRACKS + 1),
        ).fetchall()
    if len(rows) > MAX_TRACKS:
        raise TransferError(f'This transfer supports up to {MAX_TRACKS} tracks at a time.')
    items = [(row['remote_id'], json.loads(row['payload_json'])) for row in rows]
    if provider == 'ytmusic':
        if any(not isinstance(data.get('position'), int) for _, data in items):
            raise TransferError('Run Sync Now for YouTube Music again to capture source order.')
        items.sort(key=lambda item: item[1]['position'])
        order = 'source_order'
    elif provider == 'spotify':
        items.sort(key=lambda item: _date(item[1].get('added_at')), reverse=True)
        order = 'added_at_desc'
    else:
        order = 'snapshot_order'
    return LibraryExport(version=1, provider=provider, order=order, tracks=[
        ExportTrack(source_id=source_id, **{key: data[key] for key in
                    ('title', 'artist', 'duration_ms', 'isrc', 'added_at') if key in data})
        for source_id, data in items
    ])


def _match(con, source, target, track):
    if target == source:
        row = con.execute(
            """SELECT remote_id FROM provider_mappings WHERE provider=?
               AND entity_kind='track' AND remote_id=?""", (target, track.source_id),
        ).fetchone()
        return (row[0], 'native_id') if row else (None, 'unmatched')
    approved = con.execute(
        """SELECT target_id FROM reviewed_links WHERE source_provider=? AND target_provider=?
           AND entity_kind='track' AND source_id=?""", (source, target, track.source_id),
    ).fetchone()
    if approved:
        return approved[0], 'reviewed'
    if track.isrc:
        codes = con.execute(
            """SELECT m.remote_id FROM provider_mappings m JOIN music_items c
               ON m.music_item_id=c.id WHERE m.provider=? AND m.entity_kind='track'
               AND c.isrc=? LIMIT 2""", (target, track.isrc),
        ).fetchall()
        if codes:
            return (codes[0][0], 'isrc') if len(codes) == 1 else (None, 'ambiguous')
    candidates = con.execute(
        """SELECT m.remote_id,c.title,c.artist,c.duration_ms,c.isrc
           FROM music_items c JOIN provider_mappings m ON m.music_item_id=c.id
           WHERE m.provider=? AND m.entity_kind='track' AND c.title=? COLLATE NOCASE
           LIMIT 51""", (target, track.title),
    ).fetchall()
    if len(candidates) == 51:
        return None, 'ambiguous'
    exact = [
        row['remote_id'] for row in candidates
        if not (track.isrc and row['isrc'] and track.isrc != row['isrc'])
        and preview_score(track.title, track.artist, track.duration_ms,
                          row['title'], row['artist'], row['duration_ms']) == 1.0
    ]
    if len(exact) == 1:
        return exact[0], 'title_artist_duration'
    return None, 'ambiguous' if exact else 'unmatched'


def preview(library, target):
    if library.provider not in ('spotify', 'ytmusic', 'apple'):
        raise TransferError('Unknown source provider.')
    with connect() as con:
        rows = []
        for position, track in enumerate(library.tracks):
            match, method = _match(con, library.provider, target, track)
            rows.append({'position': position + 1, **track.model_dump(),
                         'target_id': match, 'match': method})
    plan = hashlib.sha256(json.dumps([target, rows], sort_keys=True).encode()).hexdigest()
    return {'plan': plan, 'tracks': rows, 'matched': sum(bool(row['target_id']) for row in rows),
            'total': len(rows)}


def _record(key, status, playlist_id=None, added=0):
    with connect() as con:
        con.execute(
            'UPDATE playlist_transfers SET status=?, playlist_id=?, added=? WHERE transfer_key=?',
            (status, playlist_id, added, key),
        )


async def execute(library, target, title, expected_plan):
    if not PLAYLIST_TRANSFER_ENABLED:
        raise TransferError('Playlist transfers require PLAYLIST_TRANSFER_ENABLED=true.')
    plan = preview(library, target)
    if plan['plan'] != expected_plan:
        raise TransferError('The match list changed. Preview again before copying.')
    ids = [row['target_id'] for row in plan['tracks'] if row['target_id']]
    if not ids:
        raise TransferError('No tracks matched the destination catalog.')
    key = hashlib.sha256(json.dumps([target, title, plan['plan']]).encode()).hexdigest()
    with connect() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO playlist_transfers(transfer_key,target,title,status)
               VALUES (?,?,?,'running')""", (key, target, title),
        )
        if not cur.rowcount:
            row = con.execute(
                'SELECT status,playlist_id,added FROM playlist_transfers WHERE transfer_key=?', (key,)
            ).fetchone()
            return dict(row)
    playlist_id, added = None, 0
    try:
        if target == 'spotify':
            token = await spotify.access_token()
            async with httpx.AsyncClient(timeout=20, headers={'Authorization': f'Bearer {token}'}) as api:
                reply = await api.post(f'{spotify.API}/me/playlists', json={'name': title, 'public': False})
                reply.raise_for_status()
                playlist_id = reply.json()['id']
                _record(key, 'running', playlist_id, added)
                for offset in range(0, len(ids), 100):
                    batch = ids[offset:offset + 100]
                    reply = await api.post(f'{spotify.API}/playlists/{playlist_id}/items',
                                           json={'uris': [f'spotify:track:{value}' for value in batch]})
                    reply.raise_for_status()
                    added += len(batch)
                    _record(key, 'running', playlist_id, added)
        else:
            api = await asyncio.to_thread(ytmusic.client)
            result = await asyncio.to_thread(api.create_playlist, title, 'Imported by Sync-Music',
                                             privacy_status='PRIVATE')
            if not isinstance(result, str) or not result:
                raise TransferError('YouTube Music did not confirm playlist creation.')
            playlist_id = result
            _record(key, 'running', playlist_id, added)
            for offset in range(0, len(ids), 100):
                batch = ids[offset:offset + 100]
                result = await asyncio.to_thread(api.add_playlist_items, playlist_id, batch, duplicates=True)
                if not isinstance(result, dict) or result.get('status') != 'STATUS_SUCCEEDED':
                    raise TransferError('YouTube Music did not confirm the track batch.')
                added += len(batch)
                _record(key, 'running', playlist_id, added)
    except Exception:
        _record(key, 'needs_review', playlist_id, added)
        return {'status': 'needs_review', 'playlist_id': playlist_id, 'added': added}
    _record(key, 'completed', playlist_id, added)
    return {'status': 'completed', 'playlist_id': playlist_id, 'added': added}
