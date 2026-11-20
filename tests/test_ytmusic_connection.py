import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.utils import formatdate
from unittest.mock import Mock

import httpx
import pytest
import requests
from cryptography.fernet import Fernet
from ytmusicapi.auth.types import AuthType
from ytmusicapi.exceptions import YTMusicServerError

from app import db, provider_tokens
from app.connectors import google_oauth, ytmusic, ytmusic_transport as transport


@pytest.fixture
def database(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'db')
    monkeypatch.setattr(db, 'settings', replace(db.settings, data_dir=tmp_path))
    db.init_db()


@pytest.mark.parametrize('endpoint,status,calls', [('browse', 503, 2), ('search', 502, 2),
                                                   ('like/like', 503, 1), ('browse', 400, 1)])
def test_only_transient_reads_retry(database, monkeypatch, endpoint, status, calls):
    sent = []
    def send(_session, request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = status if len(sent) == 1 else 200
        response._content = b'{}'
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    monkeypatch.setattr(transport.time, 'sleep', lambda _: None)
    transport.MusicSession().post('https://music.youtube.com/youtubei/v1/' + endpoint,
                                  json={'context': {}}, headers={'content-encoding': 'gzip'})
    assert len(sent) == calls
    assert all('Content-Encoding' not in req.headers and json.loads(req.body) == {'context': {}} for req in sent)


def test_429_http_date_persists_and_blocks_next_request(database, monkeypatch):
    deadline = int(time.time()) + 900
    reply = requests.Response()
    reply.status_code = 429
    reply.headers['Retry-After'] = formatdate(deadline, usegmt=True)
    reply._content_consumed = True
    send = Mock(return_value=reply)
    monkeypatch.setattr(requests.Session, 'send', send)
    for _ in range(2):
        with pytest.raises(ytmusic.RateLimited) as err:
            transport.MusicSession().get('https://music.youtube.com/')
        assert abs(err.value.retry_at - deadline) <= 1
    assert send.call_count == 1


def test_concurrent_refresh_and_reauthorization(database, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(provider_tokens, 'settings', replace(db.settings, encryption_key=key))
    monkeypatch.setattr(ytmusic, 'settings', replace(db.settings, ytm_client_id='client', ytm_client_secret='secret'))
    old = dict(client_id='client', access_token='old', refresh_token='fixture', expires_at=1,
               expires_in=3600, scope=google_oauth.SCOPE, token_type='Bearer')
    provider_tokens.save_token('ytmusic', old)
    refresh = Mock(return_value={'access_token': 'new', 'expires_in': 3600})
    monkeypatch.setattr('ytmusicapi.OAuthCredentials.refresh_token', refresh)
    with ThreadPoolExecutor(2) as pool:
        clients = list(pool.map(lambda _: ytmusic.client(), range(2)))
    assert all(api.auth_type == AuthType.OAUTH_CUSTOM_CLIENT for api in clients)
    refresh.assert_called_once()
    assert provider_tokens.load_token('ytmusic')['access_token'] == 'new'
    assert not provider_tokens.replace_token('ytmusic', old, old)


def test_device_code_survives_temporary_google_failure(monkeypatch):
    grant = google_oauth.Grant('handle', 'code', 'user', 'https://google.com/device', time.monotonic()+300, 0, 5)
    monkeypatch.setattr(google_oauth, '_grant', grant)
    async def post(*args, **kwargs):
        return httpx.Response(503, headers={'Retry-After': '22'}, text='unavailable')
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    result = asyncio.run(google_oauth.poll('handle'))
    assert result == {'status': 'pending', 'interval': 22}
    assert google_oauth._grant is grant


def test_channel_fallback_requires_a_single_handle():
    api = Mock(auth_type=AuthType.OAUTH_CUSTOM_CLIENT, headers={'authorization': 'Bearer example', 'cookie': 'private'})
    api.get_account_info.side_effect = YTMusicServerError('Server returned HTTP 400: Bad Request.')
    reply = api._session.get.return_value
    reply.status_code = 200
    reply.json.return_value = {'items': [{'snippet': {'customUrl': '@fixture'}}]}
    assert ytmusic.destination_account(api) == '@fixture'
    assert api._session.get.call_args.kwargs['headers'] == {'Authorization': 'Bearer example'}
    reply.json.return_value = {'items': []}
    with pytest.raises(ytmusic.ImportRequestError):
        ytmusic.destination_account(api)
    api.auth_type = AuthType.BROWSER
    api._session.get.reset_mock()
    with pytest.raises(ytmusic.ImportRequestError):
        ytmusic.destination_account(api)
    api._session.get.assert_not_called()


def test_device_consent_to_private_library_check(database, monkeypatch):
    settings = replace(db.settings, ytm_client_id='fixture-client', ytm_client_secret='fixture-secret',
                       encryption_key=Fernet.generate_key().decode())
    for module in (ytmusic, google_oauth, provider_tokens):
        monkeypatch.setattr(module, 'settings', settings)
    monkeypatch.setattr(google_oauth, '_grant', None)
    async def post(_client, url, data):
        if url == google_oauth.DEVICE_URL:
            result = dict(device_code='device', user_code='code', interval=5, expires_in=300,
                          verification_url='https://google.com/device')
        else:
            assert data['device_code'] == 'device'
            result = dict(access_token='access', refresh_token='refresh', token_type='Bearer',
                          expires_in=3600, scope=google_oauth.SCOPE)
        return httpx.Response(200, request=httpx.Request('POST', url), json=result)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    async def consent():
        grant = await google_oauth.start()
        assert (await google_oauth.poll(grant['handle']))['status'] == 'pending'
        google_oauth._grant.next_poll = 0
        assert (await google_oauth.poll(grant['handle']))['status'] == 'connected'
    asyncio.run(consent())

    monkeypatch.setattr('ytmusicapi.ytmusic.get_visitor_id', lambda *_: {'X-Goog-Visitor-Id': 'visitor'})
    def library(_session, req, **kwargs):
        assert req.headers['Authorization'] == 'Bearer access'
        assert req.headers.get('Content-Encoding') is None
        assert json.loads(req.body)['browseId'] == 'VLLM'
        reply = requests.Response()
        reply.status_code = 200
        reply._content = json.dumps({'contents': {'twoColumnBrowseResultsRenderer': {'secondaryContents': {
            'sectionListRenderer': {'contents': [{'musicPlaylistShelfRenderer': {'playlistId': 'LM', 'contents': []}}]}}}}}).encode()
        return reply
    monkeypatch.setattr(requests.Session, 'send', library)
    assert ytmusic.check_connection() == {'authenticated': True}


@pytest.mark.parametrize('path,count', [('browse', 2), ('playlist/create', 1)])
def test_network_timeout_retries_only_a_read(database, monkeypatch, path, count):
    send = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr(requests.Session, 'send', send)
    monkeypatch.setattr(transport.time, 'sleep', lambda _: None)
    with pytest.raises(requests.Timeout):
        transport.MusicSession().post('https://music.youtube.com/youtubei/v1/' + path, json={})
    assert send.call_count == count


def test_rejected_unexpired_session_renews_once(monkeypatch):
    old = Mock(_sync_access_token='rejected')
    new = Mock()
    clients = Mock(side_effect=[old, new])
    monkeypatch.setattr(ytmusic, 'client', clients)
    reads = Mock(side_effect=[ytmusic.SessionRejected('signed out'), ['song']])
    monkeypatch.setattr(ytmusic, 'liked_tracks', reads)
    assert ytmusic.read_session() == (new, ['song'])
    clients.assert_called_with(rejected_access_token='rejected')
    assert clients.call_count == 2
