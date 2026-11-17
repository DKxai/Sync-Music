import asyncio
import base64
from contextvars import ContextVar
from dataclasses import asdict
import os
from datetime import datetime, timezone, timedelta
import hashlib
import json
from email.utils import parsedate_to_datetime
from math import ceil
import secrets
import time
from urllib.parse import urlencode, urlsplit

from cryptography.fernet import Fernet
import httpx
from . import requests_log
from .base import Item, NotConfigured, ProviderResponseError
from ..config import settings
from ..db import connect, get_setting, set_setting
from ..provider_tokens import invalidate_account

API = 'https://api.spotify.com/v1'
AUTH = 'https://accounts.spotify.com'
SCOPES = 'user-library-read user-follow-read playlist-read-private playlist-read-collaborative'
DIRECT_WRITE_SCOPES = frozenset({'user-library-modify', 'playlist-modify-private'})
rate_limit_waiter = ContextVar('spotify_rate_limit_waiter', default=None)
COOLDOWN_SETTING = 'spotify_api_retry_at'
_pacing = False
_spacing = 0.2
_next_request = 0.0


def enable_pacing(enabled):
    global _pacing, _spacing, _next_request
    _pacing = enabled
    _spacing, _next_request = 0.2, 0.0


async def pace():
    global _next_request
    now = time.monotonic()
    earliest = max(now, _next_request)
    _next_request = earliest + _spacing
    if earliest > now:
        await asyncio.sleep(earliest - now)


def adjust_pace(status):
    global _spacing
    if status == 429:
        _spacing = min(2.0, _spacing * 2)
    elif status < 400:
        _spacing = max(0.2, _spacing * 0.995)


class MissingWriteScope(ValueError):
    pass


def api_retry_at():
    deadline = int(get_setting(COOLDOWN_SETTING) or 0)
    return deadline if deadline > time.time() else None


def record_rate_limit(response):
    if response.status_code != 429:
        return None
    host = response.request.url.host
    if host not in ('api.spotify.com', 'accounts.spotify.com'):
        return None
    reason = 'rate_limit'
    try:
        if response.json().get('error', {}).get('reason') == 'QUOTA_EXCEEDED':
            reason = 'quota'
    except (ValueError, AttributeError):
        pass
    fallback = 3600 if reason == 'quota' else 30
    retry_after = response.headers.get('Retry-After')
    delay = fallback
    if retry_after:
        try:
            delay = max(1, int(retry_after))
        except ValueError:
            try:
                delay = max(1, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    deadline = ceil(time.time() + delay)
    if host == 'api.spotify.com':
        set_setting('spotify_limit_reason', reason)
        with connect() as con:
            row = con.execute(
                """INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE
                   SET value=MAX(CAST(settings.value AS INTEGER), CAST(excluded.value AS INTEGER))
                   RETURNING value""", (COOLDOWN_SETTING, str(deadline)),
            ).fetchone()
            deadline = int(row[0])
    return deadline


async def wait_after_rate_limit(response):
    waiter = rate_limit_waiter.get()
    if waiter is None or (deadline := record_rate_limit(response)) is None:
        return False
    await waiter(deadline)
    return True


async def request(client, method, url, *, retry_rate_limit=False, **kwargs):
    refreshed = False
    retry_read = method == 'GET' and rate_limit_waiter.get() is not None
    path = urlsplit(str(url)).path
    operation = ('authorize' if path == '/api/token' else 'search' if path == '/v1/search'
                 else 'verify' if path.endswith('/contains') else 'read' if method == 'GET' else 'write')
    while True:
        paced = _pacing and urlsplit(str(url)).netloc == 'api.spotify.com'
        if paced:
            await pace()
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            requests_log.record('spotify', operation, 0)
            if not retry_read:
                raise
            retry_read = False
            await asyncio.sleep(1)
            continue
        requests_log.record('spotify', operation, response.status_code)
        if paced:
            adjust_pace(response.status_code)
        if (method == 'GET' or retry_rate_limit) and await wait_after_rate_limit(response):
            continue
        if retry_read and response.status_code in (500, 502, 503, 504):
            retry_read = False
            await asyncio.sleep(1)
            continue
        if (response.status_code == 401 and not refreshed and rate_limit_waiter.get() is not None
                and response.request.url.host == 'api.spotify.com'):
            refreshed = True
            authorization = f'Bearer {await access_token()}'
            if client.headers.get('Authorization') != authorization:
                client.headers['Authorization'] = authorization
                continue
        return response


def _require_scopes(tokens, required):
    missing = required.difference((tokens.get('scope') or '').split())
    if missing:
        raise MissingWriteScope('Spotify thiếu quyền ' + ', '.join(sorted(missing))
                                + '. Bấm Kết nối Spotify và cấp lại quyền.')


def _cipher():
    return Fernet(settings.encryption_key.encode())


def response_object(response, operation):
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ProviderResponseError(f'Spotify {operation}: phản hồi không đúng định dạng.')
    return data


def _token_response(response):
    response.raise_for_status()
    data = response_object(response, 'cấp token')
    try:
        expires_in = int(data.get('expires_in'))
    except (TypeError, ValueError):
        expires_in = 0
    if not isinstance(data.get('access_token'), str) or not data['access_token'] or expires_in <= 0:
        raise ProviderResponseError('Spotify cấp token: phản hồi thiếu token hoặc thời hạn hợp lệ.')
    data['expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)).timestamp()
    data['client_id'] = settings.spotify_client_id
    return data


def _load_tokens():
    with connect() as con:
        row = con.execute('SELECT payload_ciphertext FROM encrypted_tokens WHERE provider="spotify"').fetchone()
    if not row:
        raise NotConfigured('Spotify OAuth not connected')
    return json.loads(_cipher().decrypt(row['payload_ciphertext']))


def _save_tokens(tokens: dict, *, reconnect: bool = False):
    existing = None
    try:
        existing = _load_tokens()
    except NotConfigured:
        pass
    if existing and not reconnect:
        if not tokens.get('refresh_token'):
            tokens['refresh_token'] = existing.get('refresh_token')
        if 'scope' not in tokens:
            tokens['scope'] = existing.get('scope')
    token = _cipher().encrypt(json.dumps(tokens).encode())
    with connect() as con:
        con.execute('INSERT INTO encrypted_tokens(provider,payload_ciphertext) VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET payload_ciphertext=excluded.payload_ciphertext,updated_at=datetime("now")', ('spotify',token))
        if reconnect:
            invalidate_account(con, 'spotify')


def auth_url():
    if not settings.spotify_client_id or not settings.spotify_redirect_uri:
        raise NotConfigured('Spotify client ID / redirect URI missing')
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
    with connect() as con:
        con.execute('DELETE FROM oauth_states WHERE expires_at < datetime("now")')
        con.execute('INSERT INTO oauth_states(state,verifier_ciphertext,expires_at) VALUES(?,?,?)', (state,_cipher().encrypt(verifier.encode()),expires_at))
    scope = SCOPES
    direct_transfer = os.getenv('AUTO_TRANSFER_ENABLED', '').lower() == 'true'
    if settings.write_enabled or direct_transfer:
        scope += ' user-library-modify'
    if settings.write_enabled:
        scope += ' user-follow-modify'
    if direct_transfer or os.getenv('PLAYLIST_TRANSFER_ENABLED', '').lower() == 'true':
        scope += ' playlist-modify-private'
    params = dict(response_type='code',client_id=settings.spotify_client_id,redirect_uri=settings.spotify_redirect_uri,code_challenge_method='S256',code_challenge=challenge,scope=scope,state=state)
    if direct_transfer:
        params['show_dialog'] = 'true'
    return f'{AUTH}/authorize?{urlencode(params)}'


async def redeem(code: str, state: str):
    with connect() as con:
        # Atomic consume: the state may not be reused.
        row = con.execute('SELECT verifier_ciphertext FROM oauth_states WHERE state=? AND used_at IS NULL AND expires_at > datetime("now")', (state,)).fetchone()
        if not row:
            raise ValueError('Invalid or expired OAuth state')
        consumed = con.execute('UPDATE oauth_states SET used_at=datetime("now") WHERE state=? AND used_at IS NULL', (state,))
        if not consumed.rowcount:
            raise ValueError('Invalid or expired OAuth state')
    verifier = _cipher().decrypt(row['verifier_ciphertext']).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await request(client, 'POST', f'{AUTH}/api/token', data={'grant_type':'authorization_code','code':code,'redirect_uri':settings.spotify_redirect_uri,'client_id':settings.spotify_client_id,'code_verifier':verifier})
        data = _token_response(r)
    if not data.get('refresh_token'):
        raise ProviderResponseError('Spotify cấp token: phản hồi thiếu refresh token.')
    _save_tokens(data, reconnect=True)


async def access_token(*, required_scopes=frozenset()):
    tokens = _load_tokens()
    if tokens.get('client_id', settings.spotify_client_id) != settings.spotify_client_id:
        raise NotConfigured('Spotify Client ID changed. Reconnect Spotify.')
    if tokens.get('access_token') and tokens.get('expires_at',0) > datetime.now(timezone.utc).timestamp():
        _require_scopes(tokens, required_scopes)
        return tokens['access_token']
    if not tokens.get('refresh_token'):
        raise NotConfigured('Spotify refresh_token missing: reconnect Spotify')
    async with httpx.AsyncClient(timeout=30) as client:
        r = await request(client, 'POST', f'{AUTH}/api/token', retry_rate_limit=True,
                          data={'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],'client_id':settings.spotify_client_id})
        data = _token_response(r)
    _save_tokens(data)
    _require_scopes(data, required_scopes)
    return data['access_token']


async def fetch():
    token = await access_token()
    headers = {'Authorization':f'Bearer {token}'}
    async with httpx.AsyncClient(timeout=35, headers=headers) as client:
        async def paged(url, operation, params=None):
            results, seen = [], set()
            while url:
                parsed = urlsplit(url)
                if parsed.netloc != 'api.spotify.com' or parsed.scheme != 'https' or url in seen:
                    raise ProviderResponseError(f'Spotify {operation}: đường dẫn phân trang không hợp lệ.')
                seen.add(url)
                r=await request(client, 'GET', url, params=params)
                r.raise_for_status()  # incomplete pagination aborts the entire provider poll
                data=response_object(r, operation)
                if not isinstance(data.get('items'), list):
                    raise ProviderResponseError(f'Spotify {operation}: phản hồi thiếu danh sách kết quả.')
                results.extend(data['items'])
                url=data.get('next')
                params=None
            return results
        likes = await paged(f'{API}/me/tracks', 'đọc bài đã thích', {'limit':50})
        follow=[]
        after=None
        seen_cursors = set()
        while True:
            params={'type':'artist','limit':50}
            if after: params['after']=after
            r=await request(client, 'GET', f'{API}/me/following', params=params)
            r.raise_for_status()
            d=response_object(r, 'đọc nghệ sĩ đã theo dõi').get('artists')
            if not isinstance(d, dict) or not isinstance(d.get('items'), list):
                raise ProviderResponseError('Spotify đọc nghệ sĩ đã theo dõi: phản hồi thiếu danh sách kết quả.')
            follow.extend(d['items'])
            after=(d.get('cursors') or {}).get('after')
            if not d.get('next'):
                break
            if not isinstance(after, str) or not after or after in seen_cursors:
                raise ProviderResponseError('Spotify trả về trang nghệ sĩ chưa đầy đủ hoặc trùng lặp.')
            seen_cursors.add(after)
        playlists = await paged(f'{API}/me/playlists', 'đọc danh sách playlist', {'limit':50})
        me_resp = await request(client, 'GET', f'{API}/me')
        me_resp.raise_for_status()
        profile = response_object(me_resp, 'đọc tài khoản')
        my_id = profile.get('id') or profile.get('account_id')
        if not my_id:
            raise ProviderResponseError('Spotify đọc tài khoản: phản hồi thiếu mã tài khoản.')
        out={'liked':[], 'followed_artist':[], 'owned_playlist':[]}
        for position, entry in enumerate(likes):
            track=entry.get('track') or {}
            if not track.get('id'):
                out['liked'].append(unavailable('liked', position, track))
                continue
            out['liked'].append(Item('liked',track['id'],track.get('name') or '',artist=', '.join(x['name'] for x in track.get('artists',[]) if x.get('name')),album=(track.get('album') or {}).get('name'),duration_ms=track.get('duration_ms'),isrc=(track.get('external_ids') or {}).get('isrc'),raw={'added_at':entry.get('added_at')}))
        for a in follow:
            if a.get('id'):out['followed_artist'].append(Item('followed_artist',a['id'],a.get('name') or '',kind='artist'))
        for p in playlists:
            owner=(p.get('owner') or {}).get('id')
            if owner != my_id and not p.get('collaborative'): continue
            if not p.get('id'): continue
            out['owned_playlist'].append(Item('owned_playlist',p['id'],p.get('name') or '',kind='playlist',raw={'snapshot_id':p.get('snapshot_id'),'collaborative':p.get('collaborative'),'owned_by_me':bool(my_id and owner == my_id),'public':p.get('public')}))
            scope = f"playlist:{p['id']}"
            version = p.get('snapshot_id')
            summary = p.get('items') or p.get('tracks')
            if version and isinstance(summary, dict) and type(summary.get('total')) is int and summary['total'] == 0:
                out[scope] = []
                continue
            cached = playlist_cache(my_id, p['id'], version)
            if cached is not None:
                out[scope] = cached
                continue
            items = await paged(f"{API}/playlists/{p['id']}/items", 'đọc bài trong playlist', {'limit':50})
            if version:
                check = await request(client, 'GET', f"{API}/playlists/{p['id']}", params={'fields': 'snapshot_id'})
                check.raise_for_status()
                if response_object(check, 'xác minh phiên bản playlist').get('snapshot_id') != version:
                    raise ProviderResponseError('Playlist Spotify thay đổi khi đọc. Hệ thống sẽ thử lại.')
            out[scope] = playlist_items(scope, items)
            if version:
                save_playlist_cache(my_id, p['id'], version, out[scope])
        return out



def unavailable(scope, position, track):
    return Item(scope, f'unavailable:{position}', track.get('name') or 'Bài không khả dụng',
                raw={'position': position, 'unavailable': True})


def playlist_items(scope, entries):
    tracks, occurrences = [], {}
    for pos, entry in enumerate(entries):
        track = entry.get('item') or entry.get('track') or {}
        track_id = track.get('id')
        if not track_id or track.get('type') not in (None, 'track') or track.get('is_local'):
            tracks.append(unavailable(scope, pos, track))
            continue
        occurrences[track_id] = occurrences.get(track_id, 0) + 1
        tracks.append(Item(
            scope, f'{track_id}#{occurrences[track_id]}', track.get('name') or '',
            artist=', '.join(x['name'] for x in track.get('artists') or [] if x.get('name')),
            album=(track.get('album') or {}).get('name'), duration_ms=track.get('duration_ms'),
            isrc=(track.get('external_ids') or {}).get('isrc'),
            raw={'track_id': track_id, 'position': pos, 'added_at': entry.get('added_at')},
        ))
    return tracks


def playlist_cache(account, playlist_id, snapshot_id):
    if not snapshot_id:
        return None
    with connect() as con:
        row = con.execute(
            """SELECT payload FROM spotify_playlist_cache
               WHERE account=? AND playlist_id=? AND snapshot_id=? AND fetched_at>?""",
            (account, playlist_id, snapshot_id, int(time.time()) - 86400),
        ).fetchone()
    return [Item(**item) for item in json.loads(row[0])] if row else None


def save_playlist_cache(account, playlist_id, snapshot_id, items):
    with connect() as con:
        con.execute(
            """INSERT INTO spotify_playlist_cache VALUES (?,?,?,?,?)
               ON CONFLICT(account,playlist_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,
               fetched_at=excluded.fetched_at,payload=excluded.payload""",
            (account, playlist_id, snapshot_id, int(time.time()), json.dumps([asdict(item) for item in items])),
        )


async def contains(scope: str, remote_id: str) -> bool:
    kind = {'liked': 'track', 'followed_artist': 'artist'}[scope]
    headers = {'Authorization': f'Bearer {await access_token()}'}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        response = await request(client, 'GET', f'{API}/me/library/contains',
                                    params={'uris': f'spotify:{kind}:{remote_id}'})
        response.raise_for_status()
        found = response.json()
    if not isinstance(found, list) or len(found) != 1 or not isinstance(found[0], bool):
        raise ValueError('Spotify library check returned an unexpected response')
    return found[0]


async def add_to_library(scope: str, remote_id: str) -> None:
    kind = {'liked': 'track', 'followed_artist': 'artist'}[scope]
    headers = {'Authorization': f'Bearer {await access_token()}'}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        response = await request(client, 'PUT', f'{API}/me/library',
                                    params={'uris': f'spotify:{kind}:{remote_id}'})
        response.raise_for_status()
