import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from app import db
from app.connectors.base import Item
from app.sync import direct, undo
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


def test_direct_transfer_then_undo_keeps_prior_library(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'music.db')
    monkeypatch.setattr(db, 'settings', type('Settings', (), {'data_dir': tmp_path})())
    db.init_db()
    monkeypatch.setattr(direct, 'ENABLED', True)
    monkeypatch.setattr(direct, 'persist', lambda *_: None)
    monkeypatch.setattr(direct, '_local_match', lambda source, target, track: (track.source_id, 'reviewed', None))
    playlist = Item('owned_playlist', 'source-list', 'Road trip', kind='playlist',
                    raw={'owned_by_me': True})
    existing = Item('owned_playlist', 'existing-list', 'Road trip', kind='playlist')
    liked = {'prior'}
    tracks = ['prior']
    monkeypatch.setitem(direct.FETCHERS, 'spotify', AsyncMock(return_value={
        'liked': [Item('liked', 'prior', 'Prior'), Item('liked', 'new', 'New')],
        'owned_playlist': [playlist],
        'playlist:source-list': [Item('playlist:source-list', 'prior', 'Prior'),
                                 Item('playlist:source-list', 'new', 'New')],
    }))

    async def fetch_target():
        return {'liked': [Item('liked', value, value) for value in liked],
                'owned_playlist': [existing]}

    monkeypatch.setitem(direct.FETCHERS, 'ytmusic', fetch_target)

    class Destination:
        async def account(self):
            return '@channel'

        async def apply_likes(self, ids, journal):
            for value in ids:
                assert value not in liked
                operation = journal.record('likes', [value])
                liked.add(value)
                journal.confirm(operation)

        async def playlist_tracks(self, playlist_id):
            assert playlist_id == 'existing-list'
            return tracks.copy()

        async def append(self, playlist_id, ids):
            tracks.extend(ids)
            return ['entry-' + value for value in ids]

        async def undo_likes(self, ids):
            liked.difference_update(ids)

        async def undo_playlist(self, playlist_id, ids, guard):
            assert guard['item_ids'] == ['entry-new']
            for value in ids:
                tracks.remove(value)
            return True

    @asynccontextmanager
    async def fake_destination(target):
        assert target == 'ytmusic'
        yield Destination()

    monkeypatch.setattr(direct, 'destination_client', fake_destination)
    monkeypatch.setattr(direct, '_task', None)

    async def scenario():
        direct.save_rule('spotify', 'ytmusic', True)
        first = direct.submit('spotify', 'ytmusic')
        await direct._task
        assert direct.status()['job']['state'] == 'completed'
        assert liked == {'prior', 'new'} and tracks == ['prior', 'new']
        direct.submit('spotify', 'ytmusic')
        await direct._task
        assert direct.status()['job']['result']['liked_added'] == 0
        assert tracks == ['prior', 'new']
        direct.submit_undo(first['job_id'])
        await direct._task
        assert direct.status()['job']['state'] == 'completed'
        assert not direct.rule()['scheduled']
        assert liked == {'prior'} and tracks == ['prior']

    asyncio.run(scenario())
