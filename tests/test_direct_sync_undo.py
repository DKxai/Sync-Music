import asyncio
from unittest.mock import AsyncMock

from app import db
from app.sync import undo
from app.sync.destinations import SpotifyDestination, YouTubeDestination


def test_reverse_skips_unconfirmed_additions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'music.db')
    monkeypatch.setattr(db, 'settings', type('Settings', (), {'data_dir': tmp_path})())
    db.init_db()
    counts = {'liked_added': 0, 'playlist_tracks_added': 0}
    journal = undo.Journal('transfer', 'spotify', 'ytmusic', '@channel', counts)
    journal.confirm(journal.record('likes', ['new-like']))
    journal.record('likes', ['unconfirmed'])
    journal.confirm(journal.record('playlist', ['new-track'], 'playlist'), {'item_ids': ['new-item']})
    adapter = type('Adapter', (), {})()
    adapter.account = AsyncMock(return_value='@channel')
    adapter.undo_likes = AsyncMock()
    adapter.undo_playlist = AsyncMock(return_value=True)
    result = dict(likes_removed=0, playlist_tracks_removed=0, conflicts=0, uncertain=0)

    asyncio.run(undo.reverse('transfer', adapter, result, lambda *_: None))
    adapter.undo_likes.assert_awaited_once_with(['new-like'])
    adapter.undo_playlist.assert_awaited_once_with('playlist', ['new-track'], {'item_ids': ['new-item']})
    assert result == dict(likes_removed=1, playlist_tracks_removed=1, conflicts=0, uncertain=1)
    assert undo.history()[0]['confirmed'] == 0


def test_spotify_cannot_remove_an_older_occurrence():
    adapter = SpotifyDestination(None)
    adapter.playlist_value = AsyncMock(return_value=(['same', 'same'], 'current'))
    adapter.request = AsyncMock()
    guard = {'before': undo.fingerprint(['same']), 'after': undo.fingerprint(['same', 'same']),
             'snapshot': 'current', 'unique': False}
    assert not asyncio.run(adapter.undo_playlist('playlist', ['same'], guard))
    adapter.request.assert_not_awaited()


def test_youtube_removes_only_the_added_occurrence():
    class Playlist:
        tracks = [{'videoId': 'same', 'setVideoId': 'original'},
                  {'videoId': 'same', 'setVideoId': 'added'},
                  {'videoId': 'new', 'setVideoId': 'manually-added'}]

        def get_playlist(self, playlist_id, limit=None):
            return {'tracks': self.tracks}

        def remove_playlist_items(self, playlist_id, items):
            self.tracks = [track for track in self.tracks if track not in items]

    playlist = Playlist()
    adapter = YouTubeDestination(playlist)
    assert asyncio.run(adapter.undo_playlist('playlist', ['same'], {'item_ids': ['added']}))
    assert [track['setVideoId'] for track in playlist.tracks] == ['original', 'manually-added']
