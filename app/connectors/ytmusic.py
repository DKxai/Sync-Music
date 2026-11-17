import asyncio
import json
import re
import time
from contextvars import ContextVar
from math import ceil

from requests import Session

from . import requests_log
from .base import Item, NotConfigured, ProviderResponseError
from ..config import settings
from ..provider_tokens import load_token, save_token
from ..db import get_setting, set_setting


rate_limit_waiter = ContextVar('ytmusic_rate_limit_waiter', default=None)


class RateLimited(Exception):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__('YouTube Music đang giới hạn yêu cầu.')


def api_retry_at():
    deadline = int(get_setting('ytmusic_api_retry_at') or 0)
    return deadline if deadline > time.time() else None


class MusicSession(Session):
    def request(self, method, url, **kwargs):
        if deadline := api_retry_at():
            raise RateLimited(deadline)
        kwargs.setdefault('timeout', 30)
        endpoint = url.partition('?')[0]
        operation = ('search' if endpoint.endswith('/search') else 'write'
                     if any(part in endpoint for part in ('/like/', '/edit_playlist', '/playlist/', '/subscription/'))
                     else 'read')
        response = super().request(method, url, **kwargs)
        requests_log.record('ytmusic', operation, response.status_code)
        if response.status_code == 429:
            try:
                delay = max(1, int(response.headers.get('Retry-After', '60')))
            except ValueError:
                delay = 60
            deadline = ceil(time.time() + delay)
            set_setting('ytmusic_api_retry_at', str(deadline))
            raise RateLimited(deadline)
        return response


def duration_ms(track):
    seconds = track.get('duration_seconds')
    if type(seconds) in (int, float) and 0 < seconds < 86400:
        return round(seconds * 1000)
    value = track.get('duration')
    if isinstance(value, str) and re.fullmatch(r'\d{1,3}(?::[0-5]\d){1,2}', value):
        total = 0
        for part in value.split(':'):
            total = total * 60 + int(part)
        return total * 1000 or None
    return None

def client():
    from ytmusicapi import OAuthCredentials, YTMusic

    if settings.ytm_client_id and settings.ytm_client_secret:
        token = load_token('ytmusic')
        if token and token.get('client_id') == settings.ytm_client_id:
            credentials = OAuthCredentials(settings.ytm_client_id, settings.ytm_client_secret)
            # Persist refreshes encrypted without creating a plaintext credentials file.
            if token['expires_at'] <= time.time() + 90:
                refreshed = credentials.refresh_token(token['refresh_token'])
                if not refreshed.get('access_token'):
                    raise NotConfigured('Google token refresh failed. Reconnect YouTube Music.')
                token.update(refreshed)
                token['expires_at'] = int(time.time()) + int(token['expires_in'])
                save_token('ytmusic', token)
            return YTMusic(token, oauth_credentials=credentials, requests_session=MusicSession())
    if not settings.ytm_auth_path.is_file():
        raise NotConfigured('Connect YouTube Music via OAuth or set private/ytmusic_browser.json')
    return YTMusic(str(settings.ytm_auth_path), requests_session=MusicSession())


class ImportRequestError(ProviderResponseError):
    """Safe error metadata for the dashboard and import audit."""


def _read(stage, method, *args, **kwargs):
    from ytmusicapi.exceptions import YTMusicServerError

    try:
        return method(*args, **kwargs)
    except YTMusicServerError as exc:
        match = re.match(r'Server returned HTTP ([1-5][0-9]{2})\b', str(exc))
        detail = f'HTTP {match[1]}' if match else 'YTMusicServerError'
        raise ImportRequestError(f'YouTube Music {stage}: {detail}') from None
    except json.JSONDecodeError:
        raise ImportRequestError(f'YouTube Music {stage}: invalid JSON') from None
    except ImportRequestError:
        raise
    except (KeyError, IndexError):
        raise ImportRequestError(f'YouTube Music {stage}: response missing expected data') from None
    except ValueError:
        raise ImportRequestError(f'YouTube Music {stage}: invalid data') from None


async def call(stage, method, *args, **kwargs):
    while True:
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except RateLimited as exc:
            waiter = rate_limit_waiter.get()
            if waiter is None:
                raise
            await waiter(exc.retry_at)
        except KeyError:
            raise ProviderResponseError(f'YouTube Music không trả đủ dữ liệu khi {stage}.') from None


def _liked_shelf(api, limit):
    """Read the LM track shelf independently of optional playlist header metadata."""
    from ytmusicapi.continuations import CONTINUATION_ITEMS, get_continuation_token
    from ytmusicapi.navigation import SINGLE_COLUMN_TAB, TWO_COLUMN_RENDERER, nav
    from ytmusicapi.parsers.playlists import parse_playlist_items

    invalid = 'YouTube Music trả về danh sách bài đã thích chưa đầy đủ. Hãy kiểm tra kết nối rồi nhập lại.'
    response = api._send_request('browse', {'browseId': 'VLLM'})
    shelves = []
    for path in (TWO_COLUMN_RENDERER + ['secondaryContents'], SINGLE_COLUMN_TAB):
        sections = nav(response, path + ['sectionListRenderer', 'contents'], True) or []
        shelves.extend(section['musicPlaylistShelfRenderer'] for section in sections
                       if 'musicPlaylistShelfRenderer' in section)
    if len(shelves) != 1 or shelves[0].get('continuations'):
        raise ImportRequestError(invalid)
    rows = shelves[0].get('contents')
    tracks = []
    seen = set()
    while isinstance(rows, list):
        more = bool(rows and isinstance(rows[-1], dict) and 'continuationItemRenderer' in rows[-1])
        entries = rows[:-1] if more else rows
        if any(not isinstance(row, dict) or 'musicResponsiveListItemRenderer' not in row for row in entries):
            break
        page = parse_playlist_items(entries)
        if len(page) != len(entries):
            break
        tracks.extend(page)
        if not more or (limit is not None and len(tracks) >= limit):
            return tracks
        token = get_continuation_token(rows)
        if not token or token in seen:
            break
        seen.add(token)
        response = api._send_request('browse', {'continuation': token})
        rows = nav(response, CONTINUATION_ITEMS, True)
    raise ImportRequestError(invalid)


def liked_tracks(api, limit=None):
    try:
        result = api.get_liked_songs(limit=limit)
    except (KeyError, IndexError):
        return _read('read liked songs', _liked_shelf, api, limit)
    if not isinstance(result, dict) or not isinstance(result.get('tracks'), list):
        raise ImportRequestError('YouTube Music read liked songs: missing or invalid tracks list')
    return result['tracks']


def _fetch_sync():
    api = _read('initialize client', client)
    likes = _read('read liked songs', liked_tracks, api)
    out={'liked':[], 'followed_artist':[], 'owned_playlist':[]}
    liked_ids = set()
    for pos, t in enumerate(likes):
        video_id = t.get('videoId')
        if not video_id:
            out['liked'].append(unavailable('liked', pos, t))
            continue
        if video_id in liked_ids:
            continue
        liked_ids.add(video_id)
        out['liked'].append(Item('liked',video_id,t.get('title') or '',artist=', '.join(a.get('name','') for a in t.get('artists') or []),album=(t.get('album') or {}).get('name'),duration_ms=duration_ms(t)))
    for a in _read('read artist subscriptions', api.get_library_subscriptions, limit=100000):
        if a.get('browseId'):out['followed_artist'].append(Item('followed_artist',a['browseId'],a.get('artist') or '',kind='artist'))
    for p in _read('list library playlists', api.get_library_playlists, limit=None):
        if not p.get('owned') or not p.get('playlistId'):continue
        pid=p['playlistId']
        out['owned_playlist'].append(Item('owned_playlist',pid,p.get('title') or '',kind='playlist'))
        full=_read('read playlist tracks', api.get_playlist, pid, limit=None)
        if not isinstance(full, dict) or not isinstance(full.get('tracks'), list):
            raise ImportRequestError('YouTube Music read playlist tracks: missing or invalid tracks list')
        scope=f'playlist:{pid}'
        out[scope]=[]
        for pos,t in enumerate(full['tracks']):
            if not t.get('videoId'):
                out[scope].append(unavailable(scope, pos, t))
                continue
            # setVideoId is required for precise removal of duplicate occurrences.
            occurrence = t.get('setVideoId') or f"{pos}:{t['videoId']}"
            out[scope].append(Item(scope,occurrence,t.get('title') or '',artist=', '.join(a.get('name','') for a in t.get('artists') or []),album=(t.get('album') or {}).get('name'),duration_ms=duration_ms(t),raw={'track_id':t['videoId'],'position':pos,'setVideoId':t.get('setVideoId')}))
    return out

def unavailable(scope, position, track):
    return Item(scope, f'unavailable:{position}', track.get('title') or 'Bài không khả dụng',
                raw={'position': position, 'unavailable': True})


async def fetch():
    return await call('đọc thư viện', _fetch_sync)


def _contains_sync(scope: str, remote_id: str) -> bool:
    api = client()
    limit = 100000
    if scope == 'liked':
        rows = liked_tracks(api, limit)
        key = 'videoId'
    else:
        rows = api.get_library_subscriptions(limit=limit)
        if not isinstance(rows, list):
            raise ValueError('YouTube Music subscriptions check returned an unexpected response')
        key = 'browseId'
    if len(rows) >= limit:
        raise ValueError('YouTube Music verification limit exceeded')
    return any(row.get(key) == remote_id for row in rows)


async def contains(scope: str, remote_id: str) -> bool:
    return await asyncio.to_thread(_contains_sync, scope, remote_id)


def _add_sync(scope: str, remote_id: str) -> None:
    api = client()
    if scope == 'liked':
        from ytmusicapi import LikeStatus
        api.rate_song(remote_id, LikeStatus.LIKE)
    else:
        api.subscribe_artists([remote_id])


async def add_to_library(scope: str, remote_id: str) -> None:
    await asyncio.to_thread(_add_sync, scope, remote_id)
