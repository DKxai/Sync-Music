import asyncio
from .base import Item, NotConfigured
from ..config import settings

def _fetch_sync():
    if not settings.ytm_auth_path.is_file():
        raise NotConfigured('YTM browser auth missing: private/ytmusic_browser.json')
    from ytmusicapi import YTMusic
    api = YTMusic(str(settings.ytm_auth_path))
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
