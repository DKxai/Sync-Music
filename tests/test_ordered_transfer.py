import sqlite3

from app import db
from app.connectors.base import Item
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
