import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app import db, main
from app.connectors import spotify
from app.connectors.base import Item
from app.sync import direct, direct_results, undo
from app.sync.destinations import SpotifyDestination


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'music.db')
    monkeypatch.setattr(db, 'settings', replace(db.settings, data_dir=tmp_path))
    db.init_db()
    monkeypatch.setattr(direct, 'ENABLED', True)
    monkeypatch.setattr(direct, '_task', None)


def test_missing_write_permission_prevents_work_and_pauses_matching_rule(database, monkeypatch):
    async def denied(*, required_scopes):
        assert required_scopes == spotify.DIRECT_WRITE_SCOPES
        raise spotify.MissingWriteScope('Spotify cần cấp quyền ghi.')

    fetch = AsyncMock()
    monkeypatch.setattr(spotify, 'access_token', denied)
    monkeypatch.setitem(direct.FETCHERS, 'ytmusic', fetch)

    async def scenario():
        direct.save_rule('both', 'both', True)
        direct.submit('both', 'both')
        await direct._task
        job = direct.status()['job']
        assert job['state'] == 'failed' and job['stage'] == 'check_permissions'
        assert job['result']['schedule_paused'] and not direct.rule()['scheduled']
        fetch.assert_not_awaited()

    asyncio.run(scenario())


def test_token_scope_preflight_reads_the_granted_scope(monkeypatch):
    expiry = datetime.now(timezone.utc).timestamp() + 500
    saved = {'scope': 'user-library-read', 'access_token': 'test', 'expires_at': expiry}
    monkeypatch.setattr(spotify, '_load_tokens', lambda: saved)
    with pytest.raises(spotify.MissingWriteScope, match='user-library-modify'):
        asyncio.run(spotify.access_token(required_scopes=spotify.DIRECT_WRITE_SCOPES))
    assert asyncio.run(spotify.access_token()) == 'test'
    saved['scope'] += ' playlist-modify-private user-library-modify'
    assert asyncio.run(spotify.access_token(required_scopes=spotify.DIRECT_WRITE_SCOPES)) == 'test'


def test_spotify_write_403_records_track_failure_without_phantom_undo(database, monkeypatch):
    async def send(request):
        if request.method == 'PUT':
            return httpx.Response(403)
        if request.url.path == '/v1/me':
            return httpx.Response(200, json={'id': 'spotify-user'})
        return httpx.Response(200, json=[False])

    monkeypatch.setattr(spotify, 'access_token', AsyncMock(return_value='test'))
    monkeypatch.setattr(direct, 'persist', lambda *_: None)
    monkeypatch.setattr(direct, '_local_match', lambda *_: ('target-id', 'reviewed', None))
    monkeypatch.setitem(direct.FETCHERS, 'spotify', AsyncMock(return_value={'liked': [], 'owned_playlist': []}))
    monkeypatch.setitem(direct.FETCHERS, 'ytmusic', AsyncMock(return_value={
        'liked': [Item('liked', 'source-id', 'Example song', artist='Example artist')], 'owned_playlist': [],
    }))

    @asynccontextmanager
    async def fake_destination(target):
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
            yield SpotifyDestination(http)

    monkeypatch.setattr(direct, 'destination_client', fake_destination)

    async def scenario():
        direct.submit('ytmusic', 'spotify')
        await direct._task
        job = direct.status()['job']
        assert 'PUT /v1/me/library' in job['result']['error']
        assert job['state'] == 'failed'
        assert undo.history() == []
        rows = direct_results.list_items('failed', 0, 25)
        assert rows['total'] == 1
        assert rows['items'][0]['title'] == 'Example song'
        assert 'PUT /v1/me/library' in rows['items'][0]['reason']

    asyncio.run(scenario())


def test_two_way_uses_one_snapshot_per_provider_and_separate_undo_ids(database, monkeypatch):
    snapshots = {provider: {'liked': [], 'owned_playlist': []} for provider in direct.SERVICES}
    fetchers = {provider: AsyncMock(return_value=snapshot) for provider, snapshot in snapshots.items()}
    monkeypatch.setattr(spotify, 'access_token', AsyncMock(return_value='test'))
    monkeypatch.setattr(direct, 'persist', lambda *_: None)
    monkeypatch.setattr(direct, 'FETCHERS', fetchers)

    @asynccontextmanager
    async def fake_destination(target):
        yield target

    monkeypatch.setattr(direct, 'destination_client', fake_destination)
    apply = AsyncMock()
    monkeypatch.setattr(direct, '_apply', apply)

    async def scenario():
        reply = direct.submit('both', 'both')
        await direct._task
        assert direct.status()['job']['state'] == 'completed'
        for fetch in fetchers.values():
            fetch.assert_awaited_once()
        calls = [call.args for call in apply.await_args_list]
        assert [(call[1], call[2]) for call in calls] == [('ytmusic', 'spotify'), ('spotify', 'ytmusic')]
        for call in calls:
            assert call[3] is snapshots[call[1]] and call[4] is snapshots[call[2]]
        assert [call[-1] for call in calls] == [reply['job_id'] + '-spotify', reply['job_id'] + '-ytmusic']

    asyncio.run(scenario())


def test_search_budget_distinguishes_waiting_from_no_match(database, monkeypatch):
    monkeypatch.setattr(direct, 'MAX_SEARCHES', 1)
    monkeypatch.setattr(direct, '_local_match', lambda *_: (None, 'unmatched', None))
    monkeypatch.setattr(direct, '_catalog_match', AsyncMock(return_value=None))
    source = {'liked': [Item('liked', 'first', 'No match'),
                        Item('liked', 'second', 'Not yet searched')], 'owned_playlist': []}
    result = dict(unmatched=0, search_deferred=0)

    asyncio.run(direct._plan('job', 'ytmusic', 'spotify', source, None, result))
    assert result == dict(unmatched=1, search_deferred=1)
    assert direct_results.list_items('unmatched', 0, 25)['items'][0]['title'] == 'No match'
    assert direct_results.list_items('deferred', 0, 25)['items'][0]['title'] == 'Not yet searched'


def test_sync_all_includes_apple_when_ready(monkeypatch):
    configured = {'spotify', 'ytmusic'}
    monkeypatch.setattr(main, 'provider_ready', lambda provider: provider in configured)
    monkeypatch.setattr(main, 'submit', lambda provider: provider)
    assert set(asyncio.run(main.sync_all())['jobs']) == configured
    configured.add('apple')
    assert set(asyncio.run(main.sync_all())['jobs']) == configured


def test_old_spotify_pending_likes_are_cleared_only_if_absent(database):
    stats = dict(liked_added=0)
    journal = undo.Journal('old-rejected', 'ytmusic', 'spotify', 'account', stats)
    journal.record('likes', ['missing'])
    journal = undo.Journal('old-uncertain', 'ytmusic', 'spotify', 'account', stats)
    journal.record('likes', ['present'])
    journal = undo.Journal('old-confirmed', 'ytmusic', 'spotify', 'account', stats)
    journal.confirm(journal.record('likes', ['unliked-after-sync']))
    undo.resolve_absent_spotify_likes('account', {'present'})
    assert {row['job_id'] for row in undo.history()} == {'old-uncertain', 'old-confirmed'}


def test_result_groups_keep_same_name_playlists_and_directions_separate(database, monkeypatch):
    monkeypatch.setattr(direct, '_local_match', lambda *_: ('matched-id', 'reviewed', None))
    playlist_a = Item('owned_playlist', 'first', 'Road trip', kind='playlist')
    playlist_b = Item('owned_playlist', 'second', 'Road trip', kind='playlist')
    source = {'liked': [Item('liked', 'favorite', 'Favorite')],
              'owned_playlist': [playlist_a, playlist_b],
              'playlist:first': [Item('playlist:first', 'a', 'First song'),
                                 Item('playlist:first', 'b', 'Second song')],
              'playlist:second': [Item('playlist:second', 'c', 'Third song')]}
    asyncio.run(direct._plan('job', 'ytmusic', 'spotify', source, None, dict(unmatched=0, search_deferred=0)))
    direct_results.mark('job', 'ytmusic', [('matched-id', 2)], 'failed', 'Spotify HTTP 403')
    direct_results.record('job', 'spotify', 'ytmusic', 'Road trip',
                          [(0, 'Reverse song', None, 'pending', None)], collection_key='playlist:first')

    groups = direct_results.list_groups(None, 0, 20)['groups']
    assert [(group['source'], group['key']) for group in groups] == [
        ('spotify', 'playlist:first'), ('ytmusic', 'liked'),
        ('ytmusic', 'playlist:first'), ('ytmusic', 'playlist:second'),
    ]
    failed = direct_results.list_groups('failed', 0, 20)
    assert failed['total'] == 1
    assert failed['groups'][0]['visible'] == 1 and failed['groups'][0]['total'] == 2
    tracks = direct_results.list_items('failed', 0, 25, source='ytmusic', collection_key='playlist:first')
    assert [(track['title'], track['reason']) for track in tracks['items']] == [
        ('Second song', 'Spotify HTTP 403'),
    ]


def test_result_groups_preserve_existing_database_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'music.db')
    monkeypatch.setattr(db, 'settings', replace(db.settings, data_dir=tmp_path))
    with db.connect() as con:
        con.execute(
            """CREATE TABLE direct_sync_items (
               job_id TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
               position INTEGER NOT NULL, title TEXT NOT NULL, artist TEXT,
               collection TEXT, status TEXT NOT NULL, reason TEXT,
               PRIMARY KEY(job_id,source,position))"""
        )
        con.execute(
            """INSERT INTO direct_sync_items
               VALUES ('job','spotify','ytmusic',0,'Existing song',NULL,'Old playlist','synced',NULL)"""
        )
    db.init_db()
    db.init_db()

    group = direct_results.list_groups(None, 0, 20)['groups'][0]
    assert group['key'] == 'name:Old playlist'
    items = direct_results.list_items(None, 0, 25, source='spotify', collection_key=group['key'])
    assert items['items'][0]['title'] == 'Existing song'
