from datetime import datetime, timezone, timedelta
import re

import httpx
import jwt
from . import requests_log
from .base import Item, NotConfigured, ProviderResponseError
from ..config import settings
from ..provider_tokens import load_token

API='https://api.music.apple.com/v1'

def developer_configured() -> bool:
    return bool(settings.apple_key_path.is_file() and settings.apple_key_id and settings.apple_team_id)


def developer_token(origin: str | None = None):
    if not developer_configured():
        raise NotConfigured('Apple Music developer credentials missing')
    now=datetime.now(timezone.utc)
    lifetime = timedelta(minutes=15) if origin else timedelta(hours=12)
    claims = {'iss': settings.apple_team_id, 'iat': int(now.timestamp()),
              'exp': int((now + lifetime).timestamp())}
    if origin:
        claims['origin'] = [origin]
    return jwt.encode(claims, settings.apple_key_path.read_text(), algorithm='ES256',
                      headers={'kid': settings.apple_key_id})

def user_token() -> str:
    stored = load_token('apple')
    if stored:
        return stored['user_token']
    if settings.apple_user_token_path.is_file():
        value = settings.apple_user_token_path.read_text().strip()
        if value:
            return value
    raise NotConfigured('Connect Apple Music or provide the Music User Token file')


def headers_for(token: str) -> dict:
    return {'Authorization': f'Bearer {developer_token()}', 'Music-User-Token': token}


async def verify_token(token: str) -> None:
    async with httpx.AsyncClient(timeout=15, headers=headers_for(token)) as client:
        response = await client.get(f'{API}/me/library/songs', params={'limit': 1})
        response.raise_for_status()
        if not isinstance(response.json().get('data'), list):
            raise ValueError('Unexpected response from Apple Music')


async def fetch():
    headers = headers_for(user_token())
    async with httpx.AsyncClient(timeout=35, headers=headers) as client:
        async def paged(path):
            result, seen = [], set()
            url = f'{API}/{path}'
            while url:
                if url in seen:
                    raise ProviderResponseError('Apple Music trả về trang trùng lặp.')
                seen.add(url)
                response = await client.get(url)
                requests_log.record('apple', 'read', response.status_code)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data.get('data'), list):
                    raise ProviderResponseError('Apple Music thiếu dữ liệu phân trang.')
                result.extend(data['data'])
                next_url = data.get('next')
                if next_url and next_url.startswith('/v1/'):
                    url = 'https://api.music.apple.com' + next_url
                elif next_url and next_url.startswith(f'{API}/'):
                    url = next_url
                elif next_url:
                    raise ProviderResponseError('Apple Music trả về đường dẫn phân trang không hợp lệ.')
                else:
                    url = None
            return result

        songs = await paged('me/library/songs')
        playlists = await paged('me/library/playlists')
        out = {'library': [], 'liked': [], 'owned_playlist': []}
        for position, song in enumerate(songs):
            out['library'].append(track_item('library', song, position))
        favorites = settings.apple_favorites_playlist_id
        if favorites:
            if not re.fullmatch(r'[A-Za-z0-9._-]{1,160}', favorites):
                raise NotConfigured('Mã playlist Favorites của Apple Music không hợp lệ.')
            items = await paged(f'me/library/playlists/{favorites}/tracks')
            added = set()
            for position, song in enumerate(items):
                item = track_item('liked', song, position)
                if item.remote_id not in added:
                    out['liked'].append(item)
                    added.add(item.remote_id)
        for playlist in playlists:
            attributes = playlist.get('attributes') or {}
            playlist_id = playlist.get('id')
            if not playlist_id or not attributes.get('canEdit') or playlist_id == favorites:
                continue
            items = await paged(f'me/library/playlists/{playlist_id}/tracks')
            scope = f'playlist:{playlist_id}'
            out['owned_playlist'].append(Item('owned_playlist', playlist_id, attributes.get('name') or '', kind='playlist'))
            out[scope] = [track_item(scope, item, position) for position, item in enumerate(items)]
        return out


def track_item(scope, song, position):
    attrs = song.get('attributes') or {}
    track_id = song.get('id')
    title = attrs.get('name') or ''
    if not track_id or song.get('type') not in (None, 'songs', 'library-songs'):
        return Item(scope, f'unavailable:{position}', title or 'Bài không khả dụng',
                    raw={'position': position, 'unavailable': True})
    remote_id = f'{track_id}#{position}' if scope.startswith('playlist:') else track_id
    return Item(scope, remote_id, title, artist=attrs.get('artistName'), album=attrs.get('albumName'),
                duration_ms=attrs.get('durationInMillis'), isrc=attrs.get('isrc'),
                raw={'track_id': track_id, 'position': position, 'added_at': attrs.get('dateAdded')})
