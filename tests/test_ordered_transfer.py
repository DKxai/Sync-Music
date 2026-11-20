import sqlite3
import time

import pytest

from app import db
from app.connectors.base import Item
from app.provider_tokens import invalidate_account
from app.sync import engine, transfer


def test_export_tracks_follow_latest_order_and_exclude_old_likes(monkeypatch, tmp_path):
    db_path = tmp_path / 'transfer.db'
    monkeypatch.setattr(db, 'DB_PATH', db_path)
    with sqlite3.connect(db_path) as con:
        con.executescript(db.SCHEMA.read_text())

    engine.persist('ytmusic', {'liked': [Item('liked', 'y1', 'First'), Item('liked', 'y2', 'Old')]})
    engine.persist('ytmusic', {'liked': [Item('liked', 'y3', 'Newest'), Item('liked', 'y1', 'First')]})

    exported = transfer.export_library('ytmusic')
    assert exported.order == 'source_order'
    assert [track.source_id for track in exported.tracks] == ['y3', 'y1']


def test_spotify_export_sorts_original_timestamps(monkeypatch, tmp_path):
    db_path = tmp_path / 'transfer.db'
    monkeypatch.setattr(db, 'DB_PATH', db_path)
    with sqlite3.connect(db_path) as con:
        con.executescript(db.SCHEMA.read_text())

    engine.persist('spotify', {'liked': [
        Item('liked', 's1', 'First', raw={'added_at': '2026-01-01T00:00:00Z'}),
        Item('liked', 's2', 'Latest', raw={'added_at': '2026-02-01T00:00:00Z'}),
    ]})

    exported = transfer.export_library('spotify')
    assert exported.order == 'added_at_desc'
    assert [track.source_id for track in exported.tracks] == ['s2', 's1']


def test_playlist_export_keeps_order_duplicates_and_unavailable_tracks(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'playlist.db')
    db.init_db()
    scope = 'playlist:my-playlist'
    engine.persist('ytmusic', {
        'liked': [],
        'owned_playlist': [Item('owned_playlist', 'my-playlist', 'Road Trip', kind='playlist')],
        scope: [
            Item(scope, 'first', 'Repeated', raw={'track_id': 'video', 'position': 0}),
            Item(scope, 'unavailable:1', 'Removed', raw={'unavailable': True, 'position': 1}),
            Item(scope, 'last', 'Repeated', raw={'track_id': 'video', 'position': 2}),
        ],
    })

    collections = transfer.list_collections('ytmusic')['collections']
    assert [(item['id'], item['count']) for item in collections] == [(None, 0), ('my-playlist', 3)]
    library = transfer.export_playlist('ytmusic', 'my-playlist')
    assert library.order == 'playlist_order'
    assert [track.source_id for track in library.tracks] == ['video', 'unavailable:1', 'video']
    assert transfer.preview(library, 'ytmusic')['tracks'][1]['match'] == 'unavailable'

    with db.connect() as con:
        invalidate_account(con, 'ytmusic')
    with pytest.raises(transfer.TransferError, match='nhập thư viện'):
        transfer.list_collections('ytmusic')
    with pytest.raises(transfer.TransferError):
        transfer.export_library('ytmusic')


def test_ambiguous_snapshot_positions_cannot_be_exported(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'incomplete.db')
    db.init_db()
    scope = 'playlist:list'
    engine.persist('spotify', {
        'liked': [],
        'owned_playlist': [Item('owned_playlist', 'list', 'Road Trip', kind='playlist')],
        scope: [Item(scope, 'old', 'Old', raw={'track_id': 'old-track', 'position': 0})],
    })
    with db.connect() as con:
        con.execute(
            'INSERT INTO source_states(provider,scope,remote_id,payload_json) VALUES (?,?,?,?)',
            ('spotify', scope, 'extra', '{"title":"Extra","track_id":"extra-track","position":0}'),
        )
    with pytest.raises(transfer.TransferError, match='thứ tự playlist'):
        transfer.export_playlist('spotify', 'list')


def test_preview_reuses_catalog_matches_for_the_current_destination(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'cached.db')
    db.init_db()
    track = transfer.ExportTrack(source_id='video', title='A Song', artist='An Artist')
    library = transfer.LibraryExport(version=1, provider='ytmusic', order='source_order', tracks=[track])
    db.set_setting('spotify_auth_version', '1')
    with db.connect() as con:
        con.execute(
            """INSERT INTO direct_catalog_cache(source,source_id,target,target_id,expires_at,match_key)
               VALUES ('ytmusic','video','spotify','spotify-track',?,?)""",
            (int(time.time()) + 60, transfer.catalog_match_key(track, '1')),
        )
    assert transfer.preview(library, 'spotify')['tracks'][0]['match'] == 'catalog_cache'
    db.set_setting('spotify_auth_version', '2')
    assert transfer.preview(library, 'spotify')['matched'] == 0
