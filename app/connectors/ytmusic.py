import asyncio
import json
import re
import time
from contextvars import ContextVar
from hashlib import sha256
from threading import Lock

from requests import RequestException, Session
from cryptography.fernet import InvalidToken
from requests.structures import CaseInsensitiveDict

from .ytmusic_transport import MusicSession, RateLimited, api_retry_at
from .ytmusic_likes import LikedPlaylistError, SignedOut, read_liked_tracks
from .base import Item, NotConfigured, ProviderResponseError
from ..config import settings
from ..db import get_setting
from ..provider_tokens import has_token, load_token, replace_token



rate_limit_waiter = ContextVar('ytmusic_rate_limit_waiter', default=None)


_refresh_lock = Lock()


class GoogleSession(Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', 20)
        return super().request(method, url, **kwargs)


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

def auth_mode():
    return get_setting('ytmusic_auth_mode') or 'auto'


def client(*, rejected_access_token=None, mode=None):
    selected = mode is None
    mode = mode or auth_mode()
    if mode == 'browser':
        expected = get_setting('ytmusic_browser_fingerprint') if selected else None
        return browser_client(expected_fingerprint=expected)

    from ytmusicapi import OAuthCredentials, YTMusic

    if settings.ytm_client_id and settings.ytm_client_secret:
        with _refresh_lock:
            try:
                token = load_token('ytmusic')
            except InvalidToken:
                raise NotConfigured('Không đọc được token Google đã lưu. Kiểm tra ENCRYPTION_KEY của bản deploy.') from None
            if token:
                if token.get('client_id') != settings.ytm_client_id:
                    raise NotConfigured('OAuth client đã thay đổi. Hãy cấp lại quyền Google trên dashboard.')
                credentials = OAuthCredentials(settings.ytm_client_id, settings.ytm_client_secret, session=GoogleSession())
                if token['expires_at'] <= time.time() + 90 or token['access_token'] == rejected_access_token:
                    refreshed = credentials.refresh_token(token['refresh_token'])
                    if refreshed.get('error') == 'invalid_grant':
                        raise NotConfigured('Google đã từ chối refresh token. Hãy cấp lại quyền YouTube Music.')
                    if refreshed.get('error') in ('invalid_client', 'unauthorized_client'):
                        raise NotConfigured('Google từ chối OAuth client. Kiểm tra YTM_CLIENT_ID và YTM_CLIENT_SECRET.')
                    if not refreshed.get('access_token'):
                        raise ImportRequestError('Google chưa cấp được access token mới. Kiểm tra cấu hình OAuth rồi thử lại.')
                    renewed = {**token, **refreshed, 'expires_at': int(time.time()) + int(refreshed['expires_in'])}
                    if not replace_token('ytmusic', token, renewed):
                        raise ImportRequestError('Phiên Google đã thay đổi trong lúc gia hạn. Hãy thử lại.')
                    token = renewed
                api = YTMusic(token, oauth_credentials=credentials, requests_session=MusicSession())
                api._sync_access_token = token['access_token']
                return api
    if mode == 'oauth':
        raise NotConfigured('Chưa lưu quyền Google cho hồ sơ này.')
    return browser_client()


def browser_client(*, expected_fingerprint=None):
    from ytmusicapi import YTMusic
    from ytmusicapi.exceptions import YTMusicUserError
    from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

    if not settings.ytm_auth_path.is_file():
        raise NotConfigured(f'Chưa có tệp phiên trình duyệt. Tải lên {settings.ytm_auth_path.name} trong thư mục private của hồ sơ.')
    try:
        raw = settings.ytm_auth_path.read_bytes()
        if expected_fingerprint and sha256(raw).hexdigest() != expected_fingerprint:
            raise NotConfigured('Tệp phiên đã thay đổi. Hãy kiểm tra và chọn Dùng phiên trình duyệt lại.')
        headers = CaseInsensitiveDict(json.loads(raw.decode('utf-8-sig')))
        if not all(isinstance(v, str) for v in headers.values()):
            raise ValueError
        if not headers.get('cookie'):
            raise ValueError
        # Browser auth signs the origin on every request. An absent origin otherwise signs "None".
        if not headers.get('origin') and not headers.get('x-origin'):
            headers['origin'] = 'https://music.youtube.com'
        origin = headers.get('origin') or headers['x-origin']
        headers['authorization'] = get_authorization(sapisid_from_cookie(headers['cookie']) + ' ' + origin)
        return YTMusic(dict(headers), requests_session=MusicSession())
    except (OSError, ValueError, TypeError, AttributeError, KeyError, YTMusicUserError):
        raise NotConfigured('Tệp ytmusic_browser.json không hợp lệ. Tạo lại bằng ytmusicapi browser từ phiên YouTube Music đang đăng nhập.') from None


class ImportRequestError(ProviderResponseError):
    """Safe error metadata for the dashboard and import audit."""


class SessionRejected(ImportRequestError):
    pass


def _read(stage, method, *args, **kwargs):
    from ytmusicapi.exceptions import YTMusicServerError
    from ytmusicapi.auth.oauth.exceptions import BadOAuthClient, UnauthorizedOAuthClient

    try:
        return method(*args, **kwargs)
    except (BadOAuthClient, UnauthorizedOAuthClient):
        raise ImportRequestError('Google từ chối cấu hình OAuth. Kiểm tra client ID và cấp lại quyền YouTube Music.') from None
    except YTMusicServerError as exc:
        match = re.match(r'Server returned HTTP ([1-5][0-9]{2})\b', str(exc))
        if match and match[1] == '401':
            raise SessionRejected('YouTube Music từ chối phiên hiện tại (HTTP 401).') from None
        if match and match[1] == '403':
            raise ImportRequestError(f'YouTube Music từ chối quyền truy cập (HTTP {match[1]}). Kiểm tra tài khoản và cấp lại quyền hoặc cập nhật tệp phiên.') from None
        detail = f'HTTP {match[1]}' if match else 'YTMusicServerError'
        raise ImportRequestError(f'YouTube Music {stage}: {detail}') from None
    except json.JSONDecodeError:
        raise ImportRequestError(f'YouTube Music {stage}: invalid JSON') from None
    except RequestException:
        raise ImportRequestError(f'YouTube Music {stage}: lỗi mạng. Hãy thử lại.') from None
    except ProviderResponseError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError):
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


def liked_tracks(api, limit=None):
    # Parse the shelf directly. The upstream playlist parser may return just the
    # first page as a successful result for responses with legacy pagination.
    try:
        return _read('đọc bài đã thích', read_liked_tracks, api, limit)
    except SignedOut as exc:
        raise SessionRejected(str(exc)) from None
    except LikedPlaylistError as exc:
        raise ImportRequestError(str(exc)) from None


def destination_account(api):
    """Use the same channel handle that existing undo records identify."""
    from ytmusicapi.auth.types import AuthType
    from ytmusicapi.exceptions import YTMusicServerError

    try:
        handle = api.get_account_info().get('channelHandle')
        if handle:
            return handle
    except YTMusicServerError as exc:
        if not str(exc).startswith('Server returned HTTP 400:'):
            raise
    except (KeyError, IndexError):
        pass

    if api.auth_type == AuthType.OAUTH_CUSTOM_CLIENT:
        # Only send the OAuth bearer to Google's fixed API. Browser cookies and
        # other web-client headers must never accompany this request.
        response = api._session.get(
            'https://www.googleapis.com/youtube/v3/channels',
            headers={'Authorization': api.headers['authorization']},
            params={'mine': 'true', 'part': 'snippet', 'fields': 'items(snippet/customUrl),nextPageToken'},
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise ImportRequestError(f'YouTube Data API từ chối đọc kênh (HTTP {response.status_code}). '
                                     'Kiểm tra YouTube Data API v3 và quyền của tài khoản Google.')
        data = response.json()
        channels = data.get('items', [])
        if not data.get('nextPageToken') and len(channels) == 1:
            handle = channels[0].get('snippet', {}).get('customUrl')
            if isinstance(handle, str) and handle.startswith('@'):
                return handle
    raise ImportRequestError('Chưa xác định được kênh YouTube để ghi và hoàn tác. Kiểm tra kênh đang chọn.')


def read_session(limit=None):
    api = _read('khởi tạo kết nối', client)
    try:
        return api, liked_tracks(api, limit)
    except SessionRejected:
        rejected = getattr(api, '_sync_access_token', None)
        if not isinstance(rejected, str):
            raise
        renewed = _read('gia hạn phiên Google', client, rejected_access_token=rejected, mode='oauth')
        return renewed, liked_tracks(renewed, limit)


def validate_mode(mode):
    before = sha256(settings.ytm_auth_path.read_bytes()).hexdigest() if (
        mode == 'browser' and settings.ytm_auth_path.is_file()) else None
    candidate = _read('khởi tạo kết nối', client, mode=mode)
    liked_tracks(candidate, limit=0)
    if mode == 'browser' and has_token('ytmusic'):
        # Connection methods share transfer history only when both identify the same channel.
        oauth = _read('khởi tạo kết nối Google', client, mode='oauth')
        expected = _read('xác định kênh Google', destination_account, oauth)
        actual = _read('xác định kênh trình duyệt', destination_account, candidate)
        if expected.casefold() != actual.casefold():
            raise NotConfigured('Tệp phiên trình duyệt thuộc kênh khác với kết nối Google của hồ sơ này.')
    if mode == 'browser':
        try:
            after = sha256(settings.ytm_auth_path.read_bytes()).hexdigest()
        except OSError:
            after = None
        if not before or before != after:
            raise NotConfigured('Tệp phiên đã thay đổi trong lúc kiểm tra. Vui lòng thử lại.')
    return before


def check_connection():
    # Verify the private library used by imports. A zero limit validates the
    # first response, including an empty library, without following a cursor.
    read_session(limit=0)
    return {'authenticated': True}


def _fetch_sync():
    api, likes = read_session()
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
