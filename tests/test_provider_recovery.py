from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock

import httpx
import pytest

from app.connectors import spotify, ytmusic


def liked_response(rows):
    return {'contents': {'twoColumnBrowseResultsRenderer': {'secondaryContents': {
        'sectionListRenderer': {'contents': [{'musicPlaylistShelfRenderer': {'contents': rows}}]}}}}}


def test_missing_liked_header_reads_all_pages(monkeypatch):
    from ytmusicapi.parsers import playlists
    api = Mock()
    row = lambda key: {'musicResponsiveListItemRenderer': {'videoId': key}}
    token = {'continuationItemRenderer': {'continuationEndpoint': {'continuationCommand': {'token': 'next'}}}}
    api._send_request.side_effect = [liked_response([row('a'), token]), {'onResponseReceivedActions': [
        {'appendContinuationItemsAction': {'continuationItems': [row('b')]}}]}]
    monkeypatch.setattr(playlists, 'parse_playlist_items', lambda rows: [r['musicResponsiveListItemRenderer'] for r in rows])
    assert ytmusic.liked_tracks(api) == [{'videoId': 'a'}, {'videoId': 'b'}]
    assert api._send_request.call_args.args == ('browse', {'continuation': 'next'})


@pytest.mark.parametrize('response', [{}, liked_response([{'unexpected': {}}])])
def test_unknown_layout_never_becomes_empty_library(response):
    api = Mock()
    api._send_request.return_value = response
    with pytest.raises(ytmusic.ImportRequestError):
        ytmusic.liked_tracks(api)


def test_spotify_http_date_cooldown():
    expected = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=20)
    response = httpx.Response(429, headers={'Retry-After': format_datetime(expected, usegmt=True)},
                              request=httpx.Request('POST', spotify.AUTH + '/api/token'))
    assert abs(spotify.record_rate_limit(response) - expected.timestamp()) <= 1


def test_optional_apple_requires_a_nonempty_token(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace
    from fastapi import HTTPException
    from app import main

    path = tmp_path / 'apple-token'
    monkeypatch.setattr(main, 'settings', replace(main.settings, apple_user_token_path=path))
    monkeypatch.setattr(main.apple, 'developer_configured', lambda: True)
    monkeypatch.setattr(main, 'has_token', lambda provider: False)
    path.write_text('  ')
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.sync_now('apple'))
    assert exc.value.status_code == 409
    path.write_text('test-token')
    assert main.provider_ready('apple')


def track_entry(key):
    run = {'text': 'Song ' + key, 'navigationEndpoint': {'watchEndpoint': {'videoId': key}}}
    return {'musicResponsiveListItemRenderer': {
        'playlistItemData': {'videoId': key},
        'flexColumns': [{'musicResponsiveListItemFlexColumnRenderer': {'text': {'runs': [run]}}}],
    }}


def legacy_first():
    first = liked_response([track_entry('one')])
    shelf = first['contents']['twoColumnBrowseResultsRenderer']['secondaryContents']['sectionListRenderer']['contents'][0]
    shelf['musicPlaylistShelfRenderer']['continuations'] = [{'nextContinuationData': {'continuation': 'a+b'}}]
    return first


def test_legacy_pages_preserve_tracks_with_missing_metadata():
    api = Mock()
    api._send_request.side_effect = [legacy_first(), {'continuationContents': {
        'musicPlaylistShelfContinuation': {'contents': [track_entry('two')]}}}]
    assert [row['videoId'] for row in ytmusic.liked_tracks(api)] == ['one', 'two']
    assert api._send_request.call_args.args == ('browse', {'browseId': 'VLLM'}, '&ctoken=a%2Bb&continuation=a%2Bb')


@pytest.mark.parametrize('page,reason', [
    ({}, 'LM_NEXT_PAGE'),
    ({'continuationContents': {'musicPlaylistShelfContinuation': {'contents': [], 'continuations': [
        {'nextContinuationData': {'continuation': 'a+b'}}]}}}, 'LM_CURSOR'),
    ({'responseContext': {'mainAppWebResponseContext': {'loggedOut': True}}}, 'đăng nhập lại'),
])
def test_incomplete_second_page_rejects_entire_read(page, reason):
    api = Mock()
    api._send_request.side_effect = [legacy_first(), page]
    with pytest.raises(ytmusic.ImportRequestError, match=reason):
        ytmusic.liked_tracks(api)


def test_selected_tab_and_explicit_empty_library():
    section = {'sectionListRenderer': {'contents': [{'musicPlaylistShelfRenderer': {
        'playlistId': 'LM', 'contents': [{'messageRenderer': {'style': {'value': 'RENDER_STYLE_EMPTY_STATE'}}}]}}]}}
    api = Mock()
    api._send_request.return_value = {'contents': {'singleColumnBrowseResultsRenderer': {'tabs': [
        {'tabRenderer': {}}, {'tabRenderer': {'selected': True, 'content': section}}]}}}
    assert ytmusic.liked_tracks(api) == []


def test_connection_probe_reports_only_authentication(monkeypatch):
    api = Mock()
    monkeypatch.setattr(ytmusic, 'client', lambda: api)
    api._send_request.return_value = {'actions': [{'openPopupAction': {'popup': {'multiPageMenuRenderer': {
        'header': {'activeAccountHeaderRenderer': {'accountName': 'private name'}}}}}}]}
    assert ytmusic.check_connection() == {'authenticated': True}
    api._send_request.assert_called_once_with('account/account_menu', {})
    api._send_request.return_value = {'responseContext': {'serviceTrackingParams': [
        {'params': [{'key': 'logged_in', 'value': '0'}]}]}}
    with pytest.raises(ytmusic.ProviderResponseError, match='đăng nhập lại'):
        ytmusic.check_connection()


def test_browser_file_with_missing_origin_and_authorization(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from ytmusicapi.auth.types import AuthType

    path = tmp_path / 'browser.json'
    monkeypatch.setattr(ytmusic, 'settings', replace(ytmusic.settings, ytm_client_id='', ytm_auth_path=path))
    path.write_text(json.dumps({'Cookie': '__Secure-3PAPISID=fixture',
                                'X-Goog-Visitor-Id': 'fixture', 'X-Goog-AuthUser': '0'}))
    api = ytmusic.client()
    assert api.auth_type == AuthType.BROWSER
    assert api.origin == 'https://music.youtube.com'
    path.write_text('{"Cookie":"invalid=private-value"}')
    with pytest.raises(ytmusic.NotConfigured, match='không hợp lệ') as error:
        ytmusic.client()
    assert 'private-value' not in str(error.value)
