import asyncio
import base64
from contextvars import ContextVar
import os
from datetime import datetime, timezone, timedelta
import hashlib
import json
import secrets
from urllib.parse import urlencode

from cryptography.fernet import Fernet
import httpx
from .base import Item, NotConfigured
from ..config import settings
from ..db import connect
from ..provider_tokens import invalidate_account

API = 'https://api.spotify.com/v1'
AUTH = 'https://accounts.spotify.com'
SCOPES = 'user-library-read user-follow-read playlist-read-private playlist-read-collaborative'
DIRECT_WRITE_SCOPES = frozenset({'user-library-modify', 'playlist-modify-private'})
rate_limit_waiter = ContextVar('spotify_rate_limit_waiter', default=None)


class MissingWriteScope(ValueError):
    pass


async def wait_after_rate_limit(response):
    waiter = rate_limit_waiter.get()
    if (waiter is None or response.status_code != 429
            or response.request.url.host not in ('api.spotify.com', 'accounts.spotify.com')):
        return False
    try:
        delay = max(1, int(response.headers.get('Retry-After', '30')))
    except ValueError:
        delay = 30
    await waiter(delay)
    return True


async def request(client, method, url, *, retry_rate_limit=False, **kwargs):
    refreshed = False
    retry_read = method == 'GET' and rate_limit_waiter.get() is not None
    while True:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if not retry_read:
                raise
            retry_read = False
            await asyncio.sleep(1)
            continue
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
        r = await client.post(f'{AUTH}/api/token', data={'grant_type':'authorization_code','code':code,'redirect_uri':settings.spotify_redirect_uri,'client_id':settings.spotify_client_id,'code_verifier':verifier})
        r.raise_for_status()
        data = r.json()
    data['expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=int(data['expires_in'])-60)).timestamp()
    if not data.get('refresh_token'):
        raise ValueError('Spotify did not issue a refresh token')
    data['client_id'] = settings.spotify_client_id
    _save_tokens(data, reconnect=True)


async def access_token(*, required_scopes=frozenset()):
    tokens = _load_tokens()
    if tokens.get('client_id', settings.spotify_client_id) != settings.spotify_client_id:
        raise NotConfigured('Spotify Client ID changed. Reconnect Spotify.')
    if tokens.get('expires_at',0) > datetime.now(timezone.utc).timestamp():
        _require_scopes(tokens, required_scopes)
        return tokens['access_token']
    if not tokens.get('refresh_token'):
        raise NotConfigured('Spotify refresh_token missing: reconnect Spotify')
    async with httpx.AsyncClient(timeout=30) as client:
        r = await request(client, 'POST', f'{AUTH}/api/token', retry_rate_limit=True,
                          data={'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],'client_id':settings.spotify_client_id})
        r.raise_for_status()
        data = r.json()
    data['expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=int(data['expires_in'])-60)).timestamp()
    data['client_id'] = settings.spotify_client_id
    _save_tokens(data)
    _require_scopes(data, required_scopes)
    return data['access_token']


async def fetch():
    token = await access_token()
    headers = {'Authorization':f'Bearer {token}'}
    async with httpx.AsyncClient(timeout=35, headers=headers) as client:
        async def paged(url, params=None, root='items'):
            results=[]
            while url:
                r=await request(client, 'GET', url, params=params)
                r.raise_for_status()  # incomplete pagination aborts the entire provider poll
                data=r.json()
                results.extend(data.get(root,[]))
                url=data.get('next')
                params=None
            return results
        likes = await paged(f'{API}/me/tracks', {'limit':50})
        follow=[]
        after=None
        while True:
            params={'type':'artist','limit':50}
            if after: params['after']=after
            r=await request(client, 'GET', f'{API}/me/following', params=params)
            r.raise_for_status()
            d=r.json().get('artists',{})
            follow.extend(d.get('items',[]))
            after=(d.get('cursors') or {}).get('after')
            if not d.get('next') or not after: break
        playlists = await paged(f'{API}/me/playlists', {'limit':50})
        me_resp = await request(client, 'GET', f'{API}/me')
        me_resp.raise_for_status()
        my_id = me_resp.json().get('id')
        out={'liked':[], 'followed_artist':[], 'owned_playlist':[]}
        for entry in likes:
            track=entry.get('track') or {}
            if not track.get('id'): continue
            out['liked'].append(Item('liked',track['id'],track.get('name') or '',artist=', '.join(x['name'] for x in track.get('artists',[]) if x.get('name')),album=(track.get('album') or {}).get('name'),duration_ms=track.get('duration_ms'),isrc=(track.get('external_ids') or {}).get('isrc'),raw={'added_at':entry.get('added_at')}))
        for a in follow:
            if a.get('id'):out['followed_artist'].append(Item('followed_artist',a['id'],a.get('name') or '',kind='artist'))
        for p in playlists:
            owner=(p.get('owner') or {}).get('id')
            if owner != my_id and not p.get('collaborative'): continue
            if not p.get('id'): continue
            out['owned_playlist'].append(Item('owned_playlist',p['id'],p.get('name') or '',kind='playlist',raw={'snapshot_id':p.get('snapshot_id'),'collaborative':p.get('collaborative'),'owned_by_me':bool(my_id and owner == my_id),'public':p.get('public')}))
            # Spotify Development Mode 2026: only owned/collaborative items are supported.
            items = await paged(f"{API}/playlists/{p['id']}/items", {'limit':50})
            scope=f"playlist:{p['id']}"
            out[scope]=[]
            occurrences={}
            for pos,entry in enumerate(items):
                t=entry.get('item') or entry.get('track') or {}
                if not t.get('id'): continue
                occurrences[t["id"]]=occurrences.get(t["id"],0)+1
                out[scope].append(Item(scope, f"{t['id']}#{occurrences[t['id']]}",t.get('name') or '',artist=', '.join(x['name'] for x in t.get('artists',[]) if x.get('name')), album=(t.get('album') or {}).get('name'),duration_ms=t.get('duration_ms'),isrc=(t.get('external_ids') or {}).get('isrc'),raw={'track_id':t['id'],'position':pos}))
        return out



async def contains(scope: str, remote_id: str) -> bool:
    kind = {'liked': 'track', 'followed_artist': 'artist'}[scope]
    headers = {'Authorization': f'Bearer {await access_token()}'}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        response = await client.get(f'{API}/me/library/contains',
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
        response = await client.put(f'{API}/me/library',
                                    params={'uris': f'spotify:{kind}:{remote_id}'})
        response.raise_for_status()
