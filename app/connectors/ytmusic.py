import asyncio
import time
from .base import Item, NotConfigured
from ..config import settings
from ..provider_tokens import load_token, save_token

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
            return YTMusic(token, oauth_credentials=credentials)
    if not settings.ytm_auth_path.is_file():
        raise NotConfigured('Connect YouTube Music via OAuth or set private/ytmusic_browser.json')
    return YTMusic(str(settings.ytm_auth_path))


def _fetch_sync():
    api = client()
    likes = api.get_liked_songs(limit=100000)
    if not isinstance(likes, dict) or 'tracks' not in likes:
        raise ValueError('YTMusic liked songs returned incomplete response')
    out={'liked':[], 'followed_artist':[], 'owned_playlist':[]}
    for t in likes['tracks']:
        if not t.get('videoId'):continue
        out['liked'].append(Item('liked',t['videoId'],t.get('title') or '',artist=', '.join(a.get('name','') for a in t.get('artists') or []),album=(t.get('album') or {}).get('name'),duration_ms=(t.get('duration_seconds') or 0)*1000 or None))
    for a in api.get_library_subscriptions(limit=100000):
        if a.get('browseId'):out['followed_artist'].append(Item('followed_artist',a['browseId'],a.get('artist') or '',kind='artist'))
    for p in api.get_library_playlists(limit=None):
        if not p.get('owned') or not p.get('playlistId'):continue
        pid=p['playlistId']
        out['owned_playlist'].append(Item('owned_playlist',pid,p.get('title') or '',kind='playlist'))
        full=api.get_playlist(pid,limit=None)
        if not isinstance(full,dict) or 'tracks' not in full:
            raise ValueError('YTMusic playlist data incomplete')
        scope=f'playlist:{pid}'
        out[scope]=[]
        for pos,t in enumerate(full['tracks']):
            if not t.get('videoId'):continue
            # setVideoId is required for precise removal of duplicate occurrences.
            occurrence = t.get('setVideoId') or f"{pos}:{t['videoId']}"
            out[scope].append(Item(scope,occurrence,t.get('title') or '',artist=', '.join(a.get('name','') for a in t.get('artists') or []),album=(t.get('album') or {}).get('name'),duration_ms=(t.get('duration_seconds') or 0)*1000 or None,raw={'track_id':t['videoId'],'position':pos,'setVideoId':t.get('setVideoId')}))
    return out

async def fetch():
    return await asyncio.to_thread(_fetch_sync)


def _contains_sync(scope: str, remote_id: str) -> bool:
    api = client()
    limit = 100000
    if scope == 'liked':
        result = api.get_liked_songs(limit=limit)
        if not isinstance(result, dict) or not isinstance(result.get('tracks'), list):
            raise ValueError('YouTube Music library check returned an unexpected response')
        rows = result['tracks']
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
