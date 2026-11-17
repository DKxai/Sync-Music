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
    api.get_liked_songs.side_effect = KeyError('header')
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
    api.get_liked_songs.side_effect = KeyError('header')
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
