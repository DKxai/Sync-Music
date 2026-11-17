import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app import db, main
from app.connectors import spotify
from app.connectors.base import Item
from app.sync import direct, direct_results, engine, undo
from app.sync.destinations import SpotifyDestination, YouTubeDestination
from app.sync.matcher import catalog_match
from app.sync.transfer import ExportTrack


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


def test_spotify_oauth_import_waits_and_retries_without_marking_account_disconnected(database, monkeypatch):
    response = httpx.Response(429, headers={'Retry-After': '1'},
                             request=httpx.Request('GET', f'{spotify.API}/me/tracks'))

    async def scenario():
        requested = asyncio.Event()

        async def fetch():
            if not requested.is_set():
                requested.set()
                response.raise_for_status()
            return {'liked': [], 'owned_playlist': []}

        fetch_spotify = AsyncMock(side_effect=fetch)
        monkeypatch.setitem(engine.FETCHERS, 'spotify', fetch_spotify)
        monkeypatch.setattr(spotify, 'redeem', AsyncMock())
        monkeypatch.setattr(main, '_jobs', {})
        monkeypatch.setattr(main, '_active', {})
        monkeypatch.setattr(main, '_tasks', set())
        with db.connect() as con:
            con.execute("UPDATE accounts SET status='connected' WHERE provider='spotify'")

        callback = await main.spotify_callback(code='accepted', state='pending')
        assert 'Đã cấp quyền Spotify' in callback.body.decode()
        await asyncio.wait_for(requested.wait(), timeout=3)

        job = main.sync_status()['spotify']
        assert job['trigger'] == 'oauth' and job['state'] == 'running'
        assert job['retry_at'] == spotify.api_retry_at()
        assert not engine.POLL_LOCK.locked()
        assert (await engine.poll('spotify'))['retry_at'] == job['retry_at']
        fetch_spotify.assert_awaited_once()
        account = next(row for row in main.accounts() if row['provider'] == 'spotify')
        assert account['status'] == 'connected' and account['last_error'] is None

        await asyncio.wait_for(asyncio.gather(*main._tasks), timeout=4)
        assert main.sync_status()['spotify']['state'] == 'completed'
        assert fetch_spotify.await_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize('target', ['spotify', 'ytmusic'])
def test_incomplete_search_response_does_not_stop_other_tracks(database, monkeypatch, target):
    class YouTubeAPI:
        def search(self, query, *, filter, limit):
            if query.startswith('First'):
                raise KeyError('tracking data')
            return [{'videoId': 'y2', 'title': 'Second', 'artists': [{'name': 'Artist'}], 'duration_seconds': 200}]

    def send(request):
        if request.url.params['q'].startswith('First'):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={'tracks': {'items': [
            {'id': 's2', 'name': 'Second', 'artists': [{'name': 'Artist'}], 'duration_ms': 200000},
        ]}})

    async def scenario():
        source = 'spotify' if target == 'ytmusic' else 'ytmusic'
        snapshot = {'liked': [Item('liked', key, title, artist='Artist', duration_ms=200000)
                              for key, title in [('first', 'First'), ('second', 'Second')]], 'owned_playlist': []}
        result = dict(unmatched=0, tracks_failed=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
            adapter = SpotifyDestination(http) if target == 'spotify' else YouTubeDestination(YouTubeAPI())
            await direct._plan('job', source, target, snapshot, adapter, result)

        assert result == dict(unmatched=0, tracks_failed=1)
        failed = direct_results.list_items('failed', 0, 25)['items'][0]
        assert failed['title'] == 'First' and 'tra cứu bài hát' in failed['reason']
        assert direct_results.list_items('pending', 0, 25)['items'][0]['title'] == 'Second'

    asyncio.run(scenario())


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


def test_catalog_errors_are_reported_separately_from_unmatched_tracks(database, monkeypatch):
    monkeypatch.setattr(direct, '_local_match', lambda *_: (None, 'unmatched', None))
    monkeypatch.setattr(direct, '_catalog_match', AsyncMock(side_effect=[
        (None, 'not_found'), httpx.ReadTimeout('request timed out'), ('match', None),
    ]))
    source = {'liked': [Item('liked', 'first', 'No match'),
                        Item('liked', 'second', 'Temporary failure'),
                        Item('liked', 'third', 'Matched after failure')], 'owned_playlist': []}
    result = dict(unmatched=0, tracks_failed=0)

    asyncio.run(direct._plan('job', 'ytmusic', 'spotify', source, None, result))
    assert result == dict(unmatched=1, tracks_failed=1)
    assert direct_results.list_items('unmatched', 0, 25)['items'][0]['title'] == 'No match'
    assert direct_results.list_items('failed', 0, 25)['items'][0]['title'] == 'Temporary failure'
    assert direct_results.list_items('pending', 0, 25)['items'][0]['title'] == 'Matched after failure'


def test_catalog_matches_video_metadata_and_equivalent_editions():
    track = ExportTrack(source_id='yt1', title='Đường Về (feat. Mai) [Official Music Video]',
                        artist='Linh - Topic', duration_ms=200000)
    candidates = [('first', 'Duong Ve', 'Mai, Linh', 200500, 'VN123'),
                  ('same-recording', 'Duong Ve', 'Linh, Mai', 201000, 'VN123'),
                  ('remix', 'Duong Ve - Remix', 'Linh, Mai', 200000, 'VN456'),
                  ('cover', 'Duong Ve', 'Other Artist', 200000, 'VN789')]
    assert catalog_match(track, candidates) == ('first', None)
    assert catalog_match(track, candidates[2:]) == (None, 'insufficient')
    candidates.append(('another-recording', 'Duong Ve', 'Linh, Mai', 200000, 'VN987'))
    assert catalog_match(track, candidates) == (None, 'ambiguous')


def test_spotify_fallback_results_are_cached(database):
    track = ExportTrack(source_id='yt1', title='Đường Về', artist='Linh', duration_ms=200000)
    adapter = SpotifyDestination(None)
    adapter.search = AsyncMock(return_value=[])
    adapter.search_fallback = AsyncMock(return_value=[('sp1', 'Duong Ve', 'Linh', 201000, 'VN123')])
    assert asyncio.run(direct._catalog_match(adapter, 'ytmusic', 'spotify', track)) == ('sp1', None)
    assert direct._local_match('ytmusic', 'spotify', track)[2]['target_id'] == 'sp1'


@pytest.mark.parametrize('restart_during_wait', [None, 'likes', 'playlist'])
def test_sync_passes_old_limits_and_continues_after_spotify_wait(database, monkeypatch, restart_during_wait):
    liked = [Item('liked', f'yt{index}', f'Song {index}', artist='Artist', duration_ms=200000)
             for index in range(521)]
    source = {'liked': liked, 'owned_playlist': [Item('owned_playlist', 'source-list', 'Road trip', kind='playlist')],
              'playlist:source-list': [Item('playlist:source-list', item.remote_id, item.title,
                                            raw={'track_id': item.remote_id}) for item in liked[:205]]}
    target = {'liked': [], 'owned_playlist': []}
    monkeypatch.setattr(spotify, 'access_token', AsyncMock(return_value='test'))
    monkeypatch.setattr(direct, 'persist', lambda *_: None)
    read_source = AsyncMock(return_value=source)
    read_target = AsyncMock(return_value=target)
    monkeypatch.setattr(direct, 'FETCHERS', {'ytmusic': read_source, 'spotify': read_target})
    searches, remote_likes, playlist, playlist_batches = [], set(), [], []
    playlist_version = 0
    limit_search, limit_write = True, True
    limit_verify = restart_during_wait == 'playlist'

    def send(request):
        nonlocal limit_search, limit_write, limit_verify, playlist_version
        path = request.url.path
        if path == '/v1/search':
            _, index, _ = request.url.params['q'].split()
            if index == '3' and limit_search:
                limit_search = False
                return httpx.Response(429, headers={'Retry-After': '12'})
            searches.append(index)
            if index == '15':
                return httpx.Response(400)
            return httpx.Response(200, json={'tracks': {'items': [
                {'id': f'sp{index}', 'name': f'Song {index}', 'artists': [{'name': 'Artist'}], 'duration_ms': 200000},
            ]}})
        if path == '/v1/me':
            return httpx.Response(200, json={'id': 'owner'})
        if path.startswith('/v1/me/library'):
            ids = [uri.split(':')[-1] for uri in request.url.params['uris'].split(',')]
            assert len(ids) <= 40
            if path.endswith('/contains'):
                return httpx.Response(200, json=[value in remote_likes for value in ids])
            if request.method == 'DELETE':
                remote_likes.difference_update(ids)
                return httpx.Response(200)
            assert request.method == 'PUT'
            if len(remote_likes) == 40 and limit_write:
                limit_write = False
                return httpx.Response(429, headers={'Retry-After': '90'})
            assert remote_likes.isdisjoint(ids)
            remote_likes.update(ids)
            return httpx.Response(200)
        if path == '/v1/me/playlists':
            assert request.method == 'POST'
            assert playlist_version == 0
            playlist_version += 1
            return httpx.Response(201, json={'id': 'target-list'})
        if path.endswith('/target-list'):
            if limit_verify and len(playlist) == 100:
                limit_verify = False
                return httpx.Response(429, headers={'Retry-After': '45'})
            return httpx.Response(200, json={'owner': {'id': 'owner'}, 'public': False,
                                             'snapshot_id': str(playlist_version)})
        assert path.endswith('/target-list/items')
        if request.method == 'POST':
            ids = [uri.split(':')[-1] for uri in json.loads(request.content)['uris']]
            playlist_batches.append(len(ids))
            playlist.extend(ids)
            playlist_version += 1
            return httpx.Response(201, json={'snapshot_id': str(playlist_version)})
        if request.method == 'DELETE':
            data = json.loads(request.content)
            assert data['snapshot_id'] == str(playlist_version)
            ids = {item['uri'].split(':')[-1] for item in data['items']}
            playlist[:] = [value for value in playlist if value not in ids]
            playlist_version += 1
            return httpx.Response(200, json={'snapshot_id': str(playlist_version)})
        offset = int(request.url.params['offset'])
        return httpx.Response(200, json={'items': [{'item': {'id': value}} for value in playlist[offset:offset + 50]],
                                         'next': 'next' if len(playlist) > offset + 50 else None})

    @asynccontextmanager
    async def fake_destination(_):
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
            yield SpotifyDestination(http)

    monkeypatch.setattr(direct, 'destination_client', fake_destination)

    async def scenario():
        waiting = asyncio.Event()
        deadlines = []

        async def inspect_wait(job_id, retry_at):
            job = direct.status()['job']
            assert job['job_id'] == job_id and job['retry_at'] == retry_at
            deadlines.append(retry_at - direct.time.time())
            if (restart_during_wait == 'likes' and len(deadlines) == 2
                    or restart_during_wait == 'playlist' and len(deadlines) == 3):
                waiting.set()
                await asyncio.Future()
            with db.connect() as con:
                con.execute('UPDATE direct_sync_job SET retry_at=NULL WHERE job_id=?', (job_id,))

        monkeypatch.setattr(direct, '_wait_until', inspect_wait)
        started = direct.submit('ytmusic', 'spotify')
        if restart_during_wait:
            await asyncio.wait_for(waiting.wait(), timeout=10)
            await direct.shutdown()
            assert direct.status()['job']['result']['liked_added'] == (40 if restart_during_wait == 'likes' else 520)
            direct.recover()
        await direct._task
        job = direct.status()['job']

        assert job['job_id'] == started['job_id'] and job['retry_at'] is None
        assert job['state'] == 'partial' and job['stage'] == 'finished'
        assert job['result']['liked_added'] == len(remote_likes) == 520
        assert job['result']['playlist_tracks_added'] == 204
        assert job['result']['tracks_failed'] == 2
        assert 11 <= deadlines[0] <= 13 and 89 <= deadlines[1] <= 91
        assert len(deadlines) == {None: 2, 'likes': 3, 'playlist': 4}[restart_during_wait]
        assert playlist_batches == [100, 100, 4]
        assert playlist == [f'sp{index}' for index in range(205) if index != 15]
        failed = direct_results.list_items('failed', 0, 25)['items']
        assert {row['title'] for row in failed} == {'Song 15'}
        assert all('HTTP 400' in row['reason'] for row in failed)
        assert searches.count('3') == 1  # successful matches survive a restart
        assert undo.history()[0]['uncertain'] == 0  # a rejected 429 never becomes an undo entry
        read_source.assert_awaited_once()
        read_target.assert_awaited_once()
        with db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM direct_sync_snapshots').fetchone()[0] == 0

        direct.submit_undo(started['job_id'])
        await direct._task
        assert direct.status()['job']['state'] == 'completed'
        assert not remote_likes and not playlist

    asyncio.run(scenario())


def test_spotify_playlist_failure_does_not_stop_other_playlists(database, monkeypatch):
    def library(ids, *, spotify_playlist):
        return [Item('owned_playlist', key if spotify_playlist else 'source-' + key, key,
                     kind='playlist', raw={'owned_by_me': True, 'public': None}) for key in ids]

    playlist_ids = ('bad', 'good')
    source = {'liked': [], 'owned_playlist': library(playlist_ids, spotify_playlist=False)}
    for key in playlist_ids:
        source[f'playlist:source-{key}'] = [Item(f'playlist:source-{key}', key + '-song', key)]
    target = {'liked': [], 'owned_playlist': library(playlist_ids, spotify_playlist=True)}
    monkeypatch.setattr(direct, '_local_match', lambda _, __, track: (track.source_id, 'reviewed', None))
    tracks, posted = [], []
    snapshots = iter(['old', 'new', 'new', 'added', 'added'])

    def send(request):
        path = request.url.path
        if path.endswith('/me'):
            return httpx.Response(200, json={'id': 'owner', 'account_id': 'stable-owner'})
        if path.endswith('/bad'):
            return httpx.Response(200, json={'owner': {'id': 'owner'}, 'public': True, 'snapshot_id': 'none'})
        if path.endswith('/good'):
            if 'fields' in request.url.params:
                return httpx.Response(200, json={'snapshot_id': 'partial'})
            return httpx.Response(200, json={'owner': {'id': 'owner'}, 'public': False,
                                             'snapshot_id': next(snapshots)})
        assert path.endswith('/good/items')
        if request.method == 'POST':
            posted.append(path)
            tracks.append('good-song')
            return httpx.Response(201, json={'snapshot_id': 'added'})
        return httpx.Response(200, json={'items': [{'item': {'id': value}} for value in tracks], 'next': None})

    result = dict(unmatched=0, tracks_failed=0, playlist_tracks_added=0, playlists_need_review=0)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
            await direct._apply('job', 'ytmusic', 'spotify', source, target, SpotifyDestination(http), result)

    asyncio.run(go())
    assert posted == ['/v1/playlists/good/items']
    assert result['playlists_need_review'] == result['playlist_tracks_added'] == 1
    assert undo.history()[0]['confirmed'] == 1
    failed = direct_results.list_items('failed', 0, 25)['items']
    assert len(failed) == 1 and 'công khai' in failed[0]['reason']


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
